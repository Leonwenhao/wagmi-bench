# SPDX-License-Identifier: Apache-2.0
"""Build and prove byte-reproducible OCI archives for WAGMI Bench images."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence, cast

_RECEIPT_SCHEMA = "tradeevolve_image_reproducibility/v1"
_SOURCE_DATE_EPOCH = 0
_UV_VERSION = "0.10.0"
_SETUPTOOLS_VERSION = "83.0.0"
_APACHE_2_LICENSE_SHA256 = (
    "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAMED_DIGEST_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*(?::[a-zA-Z0-9_.-]+)?@sha256:[0-9a-f]{64}$"
)
_LOCAL_TAG_RE = re.compile(
    r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[a-zA-Z0-9_.-]+)?$"
)
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_JSON_LIMIT_BYTES = 1024 * 1024


class ReproducibilityError(RuntimeError):
    """A required reproducibility invariant was not proved."""


@dataclass(frozen=True)
class BuildSpec:
    """One exact Docker context and its build policy."""

    name: str
    context_relative: str
    expected_files: tuple[str, ...]
    dockerignore_text: str
    base_argument: str
    network: str
    runtime_image: bool

    @property
    def dockerfile_relative(self) -> str:
        return f"{self.context_relative}/Dockerfile"


@dataclass(frozen=True)
class ProjectAudit:
    """Pinned project metadata needed by the image receipt."""

    version: str
    license_sha256: str
    lock_sha256: str
    locked_package_count: int


@dataclass(frozen=True)
class BuildInputs:
    """Audited bases and transmitted-context hashes."""

    python_base: str
    iptables_version: str
    context_sha256: Mapping[str, str]


@dataclass(frozen=True)
class OCIArchiveEvidence:
    """Verified content identity from one OCI archive."""

    archive_sha256: str
    archive_bytes: int
    manifest_digest: str
    config_digest: str
    platform: str
    layer_digests: tuple[str, ...]


_AGENT = BuildSpec(
    name="agent",
    context_relative="agents",
    expected_files=(
        ".dockerignore",
        "Dockerfile",
        "__init__.py",
        "common.py",
        "cost.py",
        "evidence.py",
        "evoskill.py",
        "llm.py",
        "manifest.py",
        "prompt.py",
        "reckless.py",
        "server.py",
        "prompts/baseline-system.txt",
    ),
    dockerignore_text=(
        "**\n"
        "!Dockerfile\n"
        "!.dockerignore\n"
        "!__init__.py\n"
        "!common.py\n"
        "!cost.py\n"
        "!evidence.py\n"
        "!evoskill.py\n"
        "!llm.py\n"
        "!manifest.py\n"
        "!prompt.py\n"
        "!reckless.py\n"
        "!server.py\n"
        "!prompts/\n"
        "!prompts/baseline-system.txt\n"
    ),
    base_argument="PYTHON_BASE",
    network="none",
    runtime_image=True,
)
_GATEWAY = BuildSpec(
    name="gateway",
    context_relative="sandbox/docker/gateway",
    expected_files=(".dockerignore", "Dockerfile", "gateway_server.py"),
    dockerignore_text=(
        "**\n"
        "!Dockerfile\n"
        "!.dockerignore\n"
        "!gateway_server.py\n"
    ),
    base_argument="PYTHON_BASE",
    network="none",
    runtime_image=True,
)
_GUARD_BASE = BuildSpec(
    name="guard-base",
    context_relative="sandbox/docker/guard-base",
    expected_files=(".dockerignore", "Dockerfile"),
    dockerignore_text="**\n!Dockerfile\n!.dockerignore\n",
    base_argument="PYTHON_BASE",
    network="default",
    runtime_image=False,
)
_GUARD = BuildSpec(
    name="guard",
    context_relative="sandbox/docker/guard",
    expected_files=(
        ".dockerignore",
        "Dockerfile",
        "__init__.py",
        "block_collector.py",
        "firewall_runtime.py",
        "guard_main.py",
    ),
    dockerignore_text=(
        "**\n"
        "!Dockerfile\n"
        "!.dockerignore\n"
        "!__init__.py\n"
        "!block_collector.py\n"
        "!firewall_runtime.py\n"
        "!guard_main.py\n"
    ),
    base_argument="GUARD_BASE",
    network="none",
    runtime_image=True,
)
BUILD_SPECS = (_AGENT, _GATEWAY, _GUARD_BASE, _GUARD)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ReproducibilityError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ReproducibilityError(f"{label} must be an array")
    return cast(list[object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReproducibilityError(f"{label} must be a non-empty string")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_toml(path: Path) -> Mapping[str, object]:
    try:
        raw = cast(object, tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReproducibilityError(f"could not parse {path.name}") from exc
    return _mapping(raw, path.name)


def audit_project(repo_root: Path) -> ProjectAudit:
    """Validate the lock, canonical license, and SemVer image tag source."""

    root = repo_root.resolve(strict=True)
    pyproject_path = root / "pyproject.toml"
    lock_path = root / "uv.lock"
    license_path = root / "LICENSE"
    for path in (pyproject_path, lock_path, license_path):
        if not path.is_file() or path.is_symlink():
            raise ReproducibilityError(f"{path.name} must be a regular file")

    pyproject = _load_toml(pyproject_path)
    project = _mapping(pyproject.get("project"), "pyproject project")
    version = _string(project.get("version"), "project.version")
    if not _SEMVER_RE.fullmatch(version):
        raise ReproducibilityError("project.version is not strict SemVer")
    if project.get("license") != "Apache-2.0":
        raise ReproducibilityError("project license must be Apache-2.0")
    license_files = _sequence(
        project.get("license-files"), "project.license-files"
    )
    if list(license_files) != ["LICENSE"]:
        raise ReproducibilityError(
            "project.license-files must contain only LICENSE"
        )

    build_system = _mapping(
        pyproject.get("build-system"), "pyproject build-system"
    )
    build_requires = _sequence(
        build_system.get("requires"), "build-system.requires"
    )
    if list(build_requires) != [f"setuptools=={_SETUPTOOLS_VERSION}"]:
        raise ReproducibilityError("setuptools build backend is not pinned")

    tool = _mapping(pyproject.get("tool"), "pyproject tool")
    uv = _mapping(tool.get("uv"), "tool.uv")
    if uv.get("required-version") != f"=={_UV_VERSION}":
        raise ReproducibilityError("uv tool version is not pinned")
    constraints = _sequence(
        uv.get("build-constraint-dependencies"),
        "tool.uv.build-constraint-dependencies",
    )
    if list(constraints) != [f"setuptools=={_SETUPTOOLS_VERSION}"]:
        raise ReproducibilityError("uv build backend constraint is not pinned")

    license_sha256 = _sha256_file(license_path)
    if license_sha256 != _APACHE_2_LICENSE_SHA256:
        raise ReproducibilityError("LICENSE is not the canonical Apache-2.0 text")

    lock = _load_toml(lock_path)
    if lock.get("version") != 1:
        raise ReproducibilityError("unsupported uv.lock schema")
    packages = _sequence(lock.get("package"), "uv.lock package")
    project_found = False
    registry_count = 0
    for index, raw_package in enumerate(packages):
        package = _mapping(raw_package, f"uv.lock package {index}")
        name = _string(package.get("name"), f"uv.lock package {index} name")
        locked_version = _string(
            package.get("version"), f"uv.lock package {name} version"
        )
        source = _mapping(
            package.get("source"), f"uv.lock package {name} source"
        )
        if name == "wagmibench":
            project_found = (
                locked_version == version and source.get("editable") == "."
            )
        if "registry" not in source:
            continue
        registry_count += 1
        artifacts: list[Mapping[str, object]] = []
        raw_sdist = package.get("sdist")
        if raw_sdist is not None:
            artifacts.append(
                _mapping(raw_sdist, f"uv.lock package {name} sdist")
            )
        raw_wheels = package.get("wheels", [])
        for wheel_index, raw_wheel in enumerate(
            _sequence(raw_wheels, f"uv.lock package {name} wheels")
        ):
            artifacts.append(
                _mapping(
                    raw_wheel,
                    f"uv.lock package {name} wheel {wheel_index}",
                )
            )
        if not artifacts:
            raise ReproducibilityError(
                f"uv.lock registry package {name} has no hashed artifact"
            )
        for artifact in artifacts:
            artifact_hash = _string(
                artifact.get("hash"), f"uv.lock package {name} artifact hash"
            )
            if not _SHA256_RE.fullmatch(artifact_hash):
                raise ReproducibilityError(
                    f"uv.lock package {name} has an invalid artifact hash"
                )
    if not project_found:
        raise ReproducibilityError("uv.lock does not pin this project version")
    if registry_count == 0:
        raise ReproducibilityError("uv.lock contains no registry dependencies")

    return ProjectAudit(
        version=version,
        license_sha256=license_sha256,
        lock_sha256=_sha256_file(lock_path),
        locked_package_count=len(packages),
    )


def _dockerfile_arg(dockerfile: Path, name: str) -> str | None:
    text = dockerfile.read_text(encoding="utf-8")
    matches = re.findall(
        rf"^ARG[ \t]+{re.escape(name)}(?:=([^ \t#]+))?[ \t]*$",
        text,
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise ReproducibilityError(
            f"{dockerfile} must declare ARG {name} exactly once"
        )
    if f"FROM ${{{name}}}" not in text:
        raise ReproducibilityError(
            f"{dockerfile} must source FROM through {name}"
        )
    return matches[0] or None


def _audit_context(repo_root: Path, spec: BuildSpec) -> str:
    context = (repo_root / spec.context_relative).resolve(strict=True)
    if not context.is_relative_to(repo_root):
        raise ReproducibilityError(f"{spec.name} context escaped repository")
    if not context.is_dir():
        raise ReproducibilityError(f"{spec.name} context is not a directory")
    for candidate in context.rglob("*"):
        if candidate.is_symlink():
            raise ReproducibilityError(
                f"{spec.name} context contains a symlink"
            )
    ignore_path = context / ".dockerignore"
    if ignore_path.read_text(encoding="utf-8") != spec.dockerignore_text:
        raise ReproducibilityError(
            f"{spec.name} context denylist drifted"
        )

    digest = hashlib.sha256(b"tradeevolve-docker-context/v1\0")
    for relative in spec.expected_files:
        path = context / relative
        if not path.is_file() or path.is_symlink():
            raise ReproducibilityError(
                f"{spec.name} context is missing {relative}"
            )
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    dockerfile_text = (context / "Dockerfile").read_text(encoding="utf-8")
    if re.search(r"^\s*(?:COPY|ADD)\s+\.\s", dockerfile_text, re.MULTILINE):
        raise ReproducibilityError(
            f"{spec.name} Dockerfile transmits an unbounded context"
        )
    _dockerfile_arg(context / "Dockerfile", spec.base_argument)
    return f"sha256:{digest.hexdigest()}"


def audit_build_inputs(repo_root: Path) -> BuildInputs:
    """Validate all image contexts and their digest-pinned ancestry."""

    root = repo_root.resolve(strict=True)
    context_sha256 = {
        spec.name: _audit_context(root, spec) for spec in BUILD_SPECS
    }
    agent_base = _dockerfile_arg(
        root / _AGENT.dockerfile_relative, "PYTHON_BASE"
    )
    guard_base = _dockerfile_arg(
        root / _GUARD_BASE.dockerfile_relative, "PYTHON_BASE"
    )
    if (
        agent_base is None
        or guard_base is None
        or agent_base != guard_base
        or not _NAMED_DIGEST_RE.fullmatch(agent_base)
    ):
        raise ReproducibilityError(
            "agent and guard base must share one named Python digest"
        )
    guard_base_text = (
        root / _GUARD_BASE.dockerfile_relative
    ).read_text(encoding="utf-8")
    iptables_matches = re.findall(
        r"^ARG[ \t]+IPTABLES_VERSION=([^ \t#]+)[ \t]*$",
        guard_base_text,
        flags=re.MULTILINE,
    )
    if len(iptables_matches) != 1:
        raise ReproducibilityError("iptables package version is not pinned")
    iptables_version = iptables_matches[0]
    if '"iptables=${IPTABLES_VERSION}"' not in guard_base_text:
        raise ReproducibilityError(
            "guard base does not install the pinned iptables version"
        )
    return BuildInputs(
        python_base=agent_base,
        iptables_version=iptables_version,
        context_sha256=context_sha256,
    )


def image_tag(spec: BuildSpec, version: str) -> str:
    """Return the SemVer-tagged local image name."""

    if not _SEMVER_RE.fullmatch(version):
        raise ValueError("version must be strict SemVer")
    return f"tradeevolve/{spec.name}:{version}"


def build_command(
    *,
    spec: BuildSpec,
    repo_root: Path,
    archive_path: Path,
    version: str,
    base_image: str,
    base_manifest_digest: str | None = None,
) -> tuple[str, ...]:
    """Construct the shell-free deterministic OCI export command."""

    if _NAMED_DIGEST_RE.fullmatch(base_image):
        if base_manifest_digest is not None:
            raise ValueError(
                "named digest bases do not need a separate manifest digest"
            )
    elif (
        not _LOCAL_TAG_RE.fullmatch(base_image)
        or base_manifest_digest is None
        or not _SHA256_RE.fullmatch(base_manifest_digest)
    ):
        raise ValueError(
            "base image must be a named digest or a verified local tag"
        )
    archive = archive_path.resolve()
    if "," in str(archive) or "\n" in str(archive):
        raise ValueError("archive path is not safe for the OCI exporter")
    context = (repo_root.resolve() / spec.context_relative).resolve()
    tag = image_tag(spec, version)
    return (
        "docker",
        "buildx",
        "build",
        "--pull=false",
        "--no-cache",
        f"--network={spec.network}",
        "--provenance=false",
        "--build-arg",
        f"SOURCE_DATE_EPOCH={_SOURCE_DATE_EPOCH}",
        "--build-arg",
        f"{spec.base_argument}={base_image}",
        "--output",
        (
            f"type=oci,dest={archive},rewrite-timestamp=true,"
            f"name={tag}"
        ),
        str(context),
    )


def _descriptor(
    raw: object, label: str
) -> tuple[str, int, Mapping[str, object]]:
    descriptor = _mapping(raw, label)
    digest = _string(descriptor.get("digest"), f"{label} digest")
    size = descriptor.get("size")
    if (
        not _SHA256_RE.fullmatch(digest)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
    ):
        raise ReproducibilityError(f"{label} has an invalid digest or size")
    return digest, size, descriptor


def _read_json_member(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    name: str,
) -> Mapping[str, object]:
    member = members.get(name)
    if member is None or not member.isfile():
        raise ReproducibilityError(f"OCI archive is missing {name}")
    if member.size > _JSON_LIMIT_BYTES:
        raise ReproducibilityError(f"OCI JSON member {name} is oversized")
    stream = archive.extractfile(member)
    if stream is None:
        raise ReproducibilityError(f"OCI archive could not read {name}")
    try:
        raw = cast(object, json.loads(stream.read()))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ReproducibilityError(f"OCI member {name} is not JSON") from exc
    return _mapping(raw, f"OCI member {name}")


def _verify_blob(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    digest: str,
    size: int,
) -> str:
    name = f"blobs/sha256/{digest.removeprefix('sha256:')}"
    member = members.get(name)
    if member is None or not member.isfile() or member.size != size:
        raise ReproducibilityError(f"OCI blob {digest} size is invalid")
    stream = archive.extractfile(member)
    if stream is None:
        raise ReproducibilityError(f"OCI blob {digest} could not be read")
    actual = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        actual.update(chunk)
    actual_digest = f"sha256:{actual.hexdigest()}"
    if actual_digest != digest:
        raise ReproducibilityError(f"OCI blob {digest} failed hashing")
    return name


def _epoch_timestamp(epoch: int) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalized_image_name(name: str) -> str:
    first_component = name.split("/", 1)[0]
    if "." not in first_component and ":" not in first_component:
        return f"docker.io/{name}"
    return name


def inspect_oci_archive(
    archive_path: Path,
    *,
    expected_image: str,
    source_date_epoch: int = _SOURCE_DATE_EPOCH,
) -> OCIArchiveEvidence:
    """Verify one single-platform OCI archive and return its identities."""

    path = archive_path.resolve(strict=True)
    if not path.is_file() or path.is_symlink():
        raise ReproducibilityError("OCI archive must be a regular file")
    archive_size = path.stat().st_size
    archive_sha256 = _sha256_file(path)
    expected_created = _epoch_timestamp(source_date_epoch)

    try:
        archive = tarfile.open(path, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise ReproducibilityError("OCI archive could not be opened") from exc
    with archive:
        members: dict[str, tarfile.TarInfo] = {}
        for member in archive.getmembers():
            member_path = Path(member.name)
            if (
                member.name in members
                or member_path.is_absolute()
                or ".." in member_path.parts
                or not (member.isfile() or member.isdir())
            ):
                raise ReproducibilityError(
                    "OCI archive has an unsafe or duplicate member"
                )
            members[member.name] = member

        index = _read_json_member(archive, members, "index.json")
        manifests = _sequence(index.get("manifests"), "OCI index manifests")
        if len(manifests) != 1:
            raise ReproducibilityError(
                "OCI archive must contain one platform manifest"
            )
        manifest_digest, manifest_size, descriptor = _descriptor(
            manifests[0], "OCI manifest descriptor"
        )
        annotations = _mapping(
            descriptor.get("annotations"), "OCI manifest annotations"
        )
        if (
            annotations.get("io.containerd.image.name")
            != _normalized_image_name(expected_image)
            or annotations.get("org.opencontainers.image.created")
            != expected_created
            or annotations.get("org.opencontainers.image.ref.name")
            != expected_image.rsplit(":", 1)[1]
        ):
            raise ReproducibilityError(
                "OCI image name, SemVer tag, or timestamp drifted"
            )
        manifest_name = _verify_blob(
            archive, members, manifest_digest, manifest_size
        )
        manifest = _read_json_member(archive, members, manifest_name)
        config_digest, config_size, _ = _descriptor(
            manifest.get("config"), "OCI config descriptor"
        )
        config_name = _verify_blob(
            archive, members, config_digest, config_size
        )
        config = _read_json_member(archive, members, config_name)
        if config.get("created") != expected_created:
            raise ReproducibilityError(
                "OCI config timestamp does not match SOURCE_DATE_EPOCH"
            )
        architecture = _string(config.get("architecture"), "OCI architecture")
        operating_system = _string(config.get("os"), "OCI operating system")

        raw_layers = _sequence(manifest.get("layers"), "OCI layers")
        if not raw_layers:
            raise ReproducibilityError("OCI image has no layers")
        layer_digests: list[str] = []
        for layer_index, raw_layer in enumerate(raw_layers):
            layer_digest, layer_size, _ = _descriptor(
                raw_layer, f"OCI layer {layer_index}"
            )
            _verify_blob(archive, members, layer_digest, layer_size)
            layer_digests.append(layer_digest)

    return OCIArchiveEvidence(
        archive_sha256=archive_sha256,
        archive_bytes=archive_size,
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        platform=f"{operating_system}/{architecture}",
        layer_digests=tuple(layer_digests),
    )


def _run(command: Sequence[str]) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReproducibilityError(
            f"command failed: {' '.join(command[:3])}"
        ) from exc


def _capture(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr, end="")
        raise ReproducibilityError(
            f"command failed: {' '.join(command)}"
        ) from exc
    return completed.stdout.strip()


def _prepare_output_dir(repo_root: Path, output_dir: Path) -> Path:
    parent = output_dir.parent.resolve(strict=True)
    candidate = parent / output_dir.name
    if candidate.exists():
        if candidate.is_symlink() or not candidate.is_dir():
            raise ReproducibilityError(
                "output directory must be a real directory"
            )
        if any(candidate.iterdir()):
            raise ReproducibilityError("output directory must be empty")
    else:
        candidate.mkdir(mode=0o700)
    resolved = candidate.resolve(strict=True)
    if resolved == repo_root or resolved.is_relative_to(repo_root):
        raise ReproducibilityError(
            "image evidence must be written outside the repository"
        )
    return resolved


def _build_pair(
    *,
    spec: BuildSpec,
    repo_root: Path,
    output_dir: Path,
    version: str,
    base_image: str,
    context_sha256: str,
    base_manifest_digest: str | None = None,
) -> tuple[dict[str, object], Path]:
    tag = image_tag(spec, version)
    archives = (
        output_dir / f"{spec.name}-a.oci.tar",
        output_dir / f"{spec.name}-b.oci.tar",
    )
    evidence: list[OCIArchiveEvidence] = []
    for archive in archives:
        if base_manifest_digest is not None:
            _verify_local_image(base_image, base_manifest_digest)
        _run(
            build_command(
                spec=spec,
                repo_root=repo_root,
                archive_path=archive,
                version=version,
                base_image=base_image,
                base_manifest_digest=base_manifest_digest,
            )
        )
        evidence.append(
            inspect_oci_archive(archive, expected_image=tag)
        )
    first, second = evidence
    if first != second:
        raise ReproducibilityError(
            f"{spec.name} repeated OCI exports are not byte-identical"
        )
    resolved_base_digest = (
        base_manifest_digest
        if base_manifest_digest is not None
        else f"sha256:{base_image.rsplit('@sha256:', 1)[1]}"
    )
    record: dict[str, object] = {
        "name": spec.name,
        "runtime_image": spec.runtime_image,
        "image": tag,
        "base_image": base_image,
        "base_manifest_digest": resolved_base_digest,
        "base_reference_kind": (
            "verified-local-tag"
            if base_manifest_digest is not None
            else "named-digest"
        ),
        "network": spec.network,
        "context_sha256": context_sha256,
        "source_date_epoch": _SOURCE_DATE_EPOCH,
        "repetitions": 2,
        "manifest_digest": first.manifest_digest,
        "config_digest": first.config_digest,
        "archive_sha256": first.archive_sha256,
        "archive_bytes": first.archive_bytes,
        "platform": first.platform,
        "layer_digests": list(first.layer_digests),
        "archives": [archive.name for archive in archives],
    }
    return record, archives[0]


def _verify_local_image(image: str, manifest_digest: str) -> None:
    actual = _capture(
        (
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            image,
        )
    )
    if actual != manifest_digest:
        raise ReproducibilityError(
            "local base tag does not resolve to its verified manifest"
        )


def run_reproducibility_check(
    *, repo_root: Path, output_dir: Path
) -> Mapping[str, object]:
    """Build every image twice and return a success-only canonical receipt."""

    root = repo_root.resolve(strict=True)
    project = audit_project(root)
    inputs = audit_build_inputs(root)
    output = _prepare_output_dir(root, output_dir)
    buildx_version = _capture(("docker", "buildx", "version"))
    docker_server_version = _capture(
        ("docker", "version", "--format", "{{.Server.Version}}")
    )

    records: list[dict[str, object]] = []
    guard_base_record, guard_base_archive = _build_pair(
        spec=_GUARD_BASE,
        repo_root=root,
        output_dir=output,
        version=project.version,
        base_image=inputs.python_base,
        context_sha256=inputs.context_sha256[_GUARD_BASE.name],
    )
    records.append(guard_base_record)
    guard_base_digest = _string(
        guard_base_record.get("manifest_digest"),
        "guard base manifest digest",
    )
    _run(("docker", "load", "--input", str(guard_base_archive)))
    guard_base_tag = image_tag(_GUARD_BASE, project.version)
    _verify_local_image(guard_base_tag, guard_base_digest)

    for spec, base_image, base_manifest_digest in (
        (_AGENT, inputs.python_base, None),
        (_GATEWAY, inputs.python_base, None),
        (_GUARD, guard_base_tag, guard_base_digest),
    ):
        record, _ = _build_pair(
            spec=spec,
            repo_root=root,
            output_dir=output,
            version=project.version,
            base_image=base_image,
            context_sha256=inputs.context_sha256[spec.name],
            base_manifest_digest=base_manifest_digest,
        )
        records.append(record)

    return {
        "schema": _RECEIPT_SCHEMA,
        "ok": True,
        "project_version": project.version,
        "image_tag": project.version,
        "uv_version": _UV_VERSION,
        "lock_sha256": project.lock_sha256,
        "locked_package_count": project.locked_package_count,
        "license": "Apache-2.0",
        "license_sha256": project.license_sha256,
        "python_base": inputs.python_base,
        "iptables_version": inputs.iptables_version,
        "docker_server_version": docker_server_version,
        "buildx_version": buildx_version,
        "images": records,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prove byte-reproducible WAGMI Bench OCI image exports."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="WAGMI Bench repository root (default: current directory)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="new or empty evidence directory outside the repository",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = run_reproducibility_check(
            repo_root=cast(Path, args.repo_root),
            output_dir=cast(Path, args.output_dir),
        )
    except (OSError, ReproducibilityError, ValueError) as exc:
        print(f"reproducibility check failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(receipt, indent=2, sort_keys=True, separators=(",", ": ")),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
