# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from sandbox.reproducible import (
    BUILD_SPECS,
    ReproducibilityError,
    audit_build_inputs,
    audit_project,
    build_command,
    image_tag,
    inspect_oci_archive,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHA_BASE = "python:3.12-slim@sha256:" + ("a" * 64)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _descriptor(data: bytes) -> dict[str, object]:
    return {
        "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
        "size": len(data),
    }


def _write_oci(path: Path, *, created: str = "1970-01-01T00:00:00Z") -> None:
    layer = b"layer"
    config = _json_bytes(
        {
            "architecture": "amd64",
            "created": created,
            "os": "linux",
            "rootfs": {
                "type": "layers",
                "diff_ids": [
                    f"sha256:{hashlib.sha256(layer).hexdigest()}"
                ],
            },
        }
    )
    config_descriptor = {
        **_descriptor(config),
        "mediaType": "application/vnd.oci.image.config.v1+json",
    }
    layer_descriptor = {
        **_descriptor(layer),
        "mediaType": "application/vnd.oci.image.layer.v1.tar",
    }
    manifest = _json_bytes(
        {
            "config": config_descriptor,
            "layers": [layer_descriptor],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        }
    )
    manifest_descriptor = {
        **_descriptor(manifest),
        "annotations": {
            "io.containerd.image.name": "docker.io/tradeevolve/agent:0.1.0",
            "org.opencontainers.image.created": created,
            "org.opencontainers.image.ref.name": "0.1.0",
        },
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "platform": {"architecture": "amd64", "os": "linux"},
    }
    config_digest = str(config_descriptor["digest"])
    layer_digest = str(layer_descriptor["digest"])
    manifest_digest = str(manifest_descriptor["digest"])
    index = _json_bytes(
        {
            "manifests": [manifest_descriptor],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        }
    )
    layout = _json_bytes({"imageLayoutVersion": "1.0.0"})
    blobs = {
        (
            "blobs/sha256/"
            + config_digest.removeprefix("sha256:")
        ): config,
        (
            "blobs/sha256/"
            + layer_digest.removeprefix("sha256:")
        ): layer,
        (
            "blobs/sha256/"
            + manifest_digest.removeprefix("sha256:")
        ): manifest,
        "index.json": index,
        "oci-layout": layout,
    }
    with tarfile.open(path, mode="w") as archive:
        for name, data in sorted(blobs.items()):
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.mtime = 0
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_repository_reproducibility_inputs_are_pinned() -> None:
    project = audit_project(REPO_ROOT)
    inputs = audit_build_inputs(REPO_ROOT)

    assert project.version == "0.1.0"
    assert project.locked_package_count > 1
    assert project.license_sha256 == (
        "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
    )
    assert inputs.python_base.startswith("python:3.12-slim@sha256:")
    assert inputs.iptables_version == "1.8.11-2"
    assert set(inputs.context_sha256) == {
        "agent",
        "gateway",
        "guard-base",
        "guard",
    }


@pytest.mark.parametrize("spec", BUILD_SPECS, ids=lambda spec: spec.name)
def test_build_commands_are_cacheless_digest_based_oci_exports(
    spec: object,
    tmp_path: Path,
) -> None:
    typed_spec = next(
        candidate for candidate in BUILD_SPECS if candidate is spec
    )
    command = build_command(
        spec=typed_spec,
        repo_root=REPO_ROOT,
        archive_path=tmp_path / f"{typed_spec.name}.oci.tar",
        version="0.1.0",
        base_image=SHA_BASE,
    )

    assert command[:3] == ("docker", "buildx", "build")
    assert "--pull=false" in command
    assert "--no-cache" in command
    assert "--provenance=false" in command
    assert f"--network={typed_spec.network}" in command
    assert "SOURCE_DATE_EPOCH=0" in command
    output = command[command.index("--output") + 1]
    assert "type=oci" in output
    assert "rewrite-timestamp=true" in output
    assert f"name={image_tag(typed_spec, '0.1.0')}" in output
    if typed_spec.runtime_image:
        assert "--network=none" in command
    else:
        assert typed_spec.name == "guard-base"
        assert "--network=default" in command


def test_oci_archive_inspection_verifies_all_content(tmp_path: Path) -> None:
    archive = tmp_path / "agent.oci.tar"
    _write_oci(archive)

    evidence = inspect_oci_archive(
        archive, expected_image="tradeevolve/agent:0.1.0"
    )

    assert evidence.platform == "linux/amd64"
    assert evidence.manifest_digest.startswith("sha256:")
    assert len(evidence.layer_digests) == 1
    assert evidence.archive_sha256 == hashlib.sha256(
        archive.read_bytes()
    ).hexdigest()


def test_local_base_tag_requires_a_verified_manifest_digest(
    tmp_path: Path,
) -> None:
    guard_spec = next(spec for spec in BUILD_SPECS if spec.name == "guard")
    with pytest.raises(ValueError, match="verified local tag"):
        build_command(
            spec=guard_spec,
            repo_root=REPO_ROOT,
            archive_path=tmp_path / "guard.oci.tar",
            version="0.1.0",
            base_image="tradeevolve/guard-base:0.1.0",
        )

    command = build_command(
        spec=guard_spec,
        repo_root=REPO_ROOT,
        archive_path=tmp_path / "guard.oci.tar",
        version="0.1.0",
        base_image="tradeevolve/guard-base:0.1.0",
        base_manifest_digest="sha256:" + ("b" * 64),
    )
    assert "GUARD_BASE=tradeevolve/guard-base:0.1.0" in command


def test_oci_archive_inspection_rejects_timestamp_drift(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "agent.oci.tar"
    _write_oci(archive, created="2026-07-26T00:00:00Z")

    with pytest.raises(ReproducibilityError, match="timestamp drifted"):
        inspect_oci_archive(
            archive, expected_image="tradeevolve/agent:0.1.0"
        )
