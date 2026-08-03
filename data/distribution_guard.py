# SPDX-License-Identifier: Apache-2.0
"""Deterministic DATA-5 guard for tracked files and Python distributions.

The repository intentionally carries source recipes, hashes, and pack
manifests, but never fetched or derived market series.  This module scans the
Git tracked set and the members of built wheels/sdists.  It does not inspect
ignored local data and never performs network I/O.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence, cast

REPORT_SCHEMA = "tradeevolve_distribution_guard/v1"
_READ_PREFIX_BYTES = 8192
_PACK_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FORBIDDEN_SUFFIXES = (
    ".arrow",
    ".avro",
    ".bz2",
    ".csv",
    ".feather",
    ".gz",
    ".h5",
    ".hdf5",
    ".npy",
    ".npz",
    ".orc",
    ".parquet",
    ".pickle",
    ".pkl",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".xz",
    ".zip",
)
_CSV_SNIFF_EXEMPT_SUFFIXES = (
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".pyi",
    ".schema.json",
    ".sh",
    ".toml",
    ".ts",
    ".yaml",
    ".yml",
)
_ARCHIVE_MAGIC = (
    (b"PK\x03\x04", "ZIP archive"),
    (b"\x1f\x8b", "gzip archive"),
    (b"BZh", "bzip2 archive"),
    (b"\xfd7zXZ\x00", "xz archive"),
    (b"PAR1", "Parquet file"),
    (b"SQLite format 3\x00", "SQLite database"),
    (b"\x89HDF\r\n\x1a\n", "HDF5 file"),
)

# These byte-pinned fixtures are synthetic protocol/test oracles.  Any new
# tracked JSONL path requires an explicit review and allowlist change.
_ALLOWED_SYNTHETIC_JSONL = frozenset(
    {
        "fixtures/golden-mini/actions.jsonl",
        "fixtures/golden-mini/derivations/c03b-shapes/main/events.jsonl",
        "fixtures/golden-mini/derivations/c03b-shapes/main/ledger.jsonl",
        (
            "fixtures/golden-mini/derivations/c03b-shapes/"
            "variant-liquidation/events.jsonl"
        ),
        (
            "fixtures/golden-mini/derivations/c03b-shapes/"
            "variant-liquidation/ledger.jsonl"
        ),
        "fixtures/golden-mini/expected/main/events.jsonl",
        "fixtures/golden-mini/expected/main/ledger.jsonl",
        "fixtures/golden-mini/expected/main/ledger_stress_2x.jsonl",
        "fixtures/golden-mini/expected/variant-liquidation/events.jsonl",
        "fixtures/golden-mini/expected/variant-liquidation/ledger.jsonl",
        (
            "fixtures/golden-mini/expected/variant-liquidation/"
            "ledger_stress_2x.jsonl"
        ),
        "fixtures/golden-mini/pack/bars_1h.jsonl",
        "fixtures/golden-mini/pack/funding.jsonl",
        "fixtures/golden-mini/pack/index_1h.jsonl",
        "fixtures/golden-mini/pack/mark_1h.jsonl",
        "fixtures/golden-mini/variant-liquidation/actions.jsonl",
        "fixtures/golden-mini/variant-liquidation/pack/bars_1h.jsonl",
        "fixtures/golden-mini/variant-liquidation/pack/funding.jsonl",
        "fixtures/golden-mini/variant-liquidation/pack/index_1h.jsonl",
        "fixtures/golden-mini/variant-liquidation/pack/mark_1h.jsonl",
        "fixtures/leakage-probe/bars_4h.jsonl",
        "fixtures/leakage-probe/funding.jsonl",
        "fixtures/leakage-probe/index_4h.jsonl",
        "fixtures/leakage-probe/mark_4h.jsonl",
    }
)


class DistributionGuardError(RuntimeError):
    """The scanner could not establish a complete fail-closed result."""


@dataclass(frozen=True, order=True, slots=True)
class ScanFinding:
    """One deterministic policy violation."""

    origin: str
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class DistributionScanReport:
    """Complete result for the requested tracked/artifact surfaces."""

    tracked_file_count: int
    artifacts: tuple[str, ...]
    findings: tuple[ScanFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_json(self) -> str:
        value: dict[str, object] = {
            "schema": REPORT_SCHEMA,
            "ok": self.ok,
            "tracked_file_count": self.tracked_file_count,
            "artifacts": list(self.artifacts),
            "findings": [
                {
                    "origin": finding.origin,
                    "path": finding.path,
                    "reason": finding.reason,
                }
                for finding in self.findings
            ],
        }
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )


def _normal_path(raw_path: str) -> tuple[str, tuple[str, ...]]:
    if (
        not raw_path
        or "\\" in raw_path
        or raw_path.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_path.split("/"))
    ):
        raise ValueError("path is not a normalized relative POSIX path")
    path = PurePosixPath(raw_path)
    return path.as_posix(), path.parts


def _pack_policy_reason(parts: tuple[str, ...]) -> str | None:
    try:
        pack_index = parts.index("packs")
    except ValueError:
        return None
    relative = parts[pack_index:]
    if (
        len(relative) == 3
        and _PACK_ID_RE.fullmatch(relative[1]) is not None
        and relative[2] == "manifest.json"
    ):
        return None
    return "packs may contain only <pack-id>/manifest.json"


def _allowed_synthetic_jsonl(path: str, *, artifact_member: bool) -> bool:
    if path in _ALLOWED_SYNTHETIC_JSONL:
        return True
    if not artifact_member:
        return False
    return any(path.endswith(f"/{allowed}") for allowed in _ALLOWED_SYNTHETIC_JSONL)


def _looks_like_numeric_market_csv(prefix: bytes) -> bool:
    matching_rows = 0
    for line in prefix.splitlines()[:4]:
        fields = line.split(b",")
        if not 3 <= len(fields) <= 16:
            continue
        timestamp = fields[0]
        if not timestamp.isdigit() or not 10 <= len(timestamp) <= 16:
            continue
        numeric_fields = 0
        for field in fields[1:]:
            candidate = field.strip()
            if candidate.startswith((b"-", b"+")):
                candidate = candidate[1:]
            if candidate.replace(b".", b"", 1).isdigit():
                numeric_fields += 1
        if numeric_fields >= 2:
            matching_rows += 1
    return matching_rows >= 2


def scan_entry(
    *,
    origin: str,
    path: str,
    prefix: bytes,
    artifact_member: bool = False,
) -> tuple[ScanFinding, ...]:
    """Inspect one named file using only its normalized path and byte prefix."""

    try:
        normalized, parts = _normal_path(path)
    except ValueError:
        return (
            ScanFinding(
                origin=origin,
                path=path,
                reason="entry path is not normalized and relative",
            ),
        )

    reasons: list[str] = []
    pack_reason = _pack_policy_reason(parts)
    if pack_reason is not None:
        reasons.append(pack_reason)

    lowered = normalized.lower()
    if lowered.endswith(".jsonl") and not _allowed_synthetic_jsonl(
        normalized,
        artifact_member=artifact_member,
    ):
        reasons.append("tracked/distributed JSONL is not an approved synthetic fixture")
    if lowered.endswith(_FORBIDDEN_SUFFIXES):
        reasons.append("raw/archive market-data file extension is forbidden")

    if any(parts[index : index + 2] == ("data", "raw") for index in range(len(parts))):
        reasons.append("data/raw content is local-only")

    for magic, description in _ARCHIVE_MAGIC:
        if prefix.startswith(magic):
            reasons.append(f"embedded {description} content is forbidden")
            break

    csv_sniff_exempt = lowered.endswith(
        _CSV_SNIFF_EXEMPT_SUFFIXES
    ) or parts[-1].lower() in {"dockerfile", ".dockerignore"}
    if not csv_sniff_exempt:
        if (
            b"open_time,open,high,low,close,volume" in prefix[:1024].lower()
            or b"calc_time,funding_interval_hours,last_funding_rate"
            in prefix[:1024].lower()
            or _looks_like_numeric_market_csv(prefix)
        ):
            reasons.append("content resembles raw Binance market-data rows")

    return tuple(
        ScanFinding(origin=origin, path=normalized, reason=reason)
        for reason in sorted(set(reasons))
    )


def _git_tracked_paths(repo_root: Path) -> tuple[str, ...]:
    try:
        completed = subprocess.run(
            ("git", "-C", str(repo_root), "ls-files", "-z"),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DistributionGuardError("unable to enumerate Git tracked files") from exc
    try:
        decoded = tuple(
            item.decode("utf-8")
            for item in completed.stdout.split(b"\x00")
            if item
        )
    except UnicodeDecodeError as exc:
        raise DistributionGuardError("tracked paths must be UTF-8") from exc
    if len(set(decoded)) != len(decoded):
        raise DistributionGuardError("Git returned duplicate tracked paths")
    return tuple(sorted(decoded))


def scan_tracked_repository(repo_root: Path) -> tuple[int, tuple[ScanFinding, ...]]:
    """Scan the checked-out bytes of every path named by ``git ls-files``."""

    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise DistributionGuardError("repository root is unavailable") from exc
    if not root.is_dir():
        raise DistributionGuardError("repository root is not a directory")

    tracked = _git_tracked_paths(root)
    findings: list[ScanFinding] = []
    for relative in tracked:
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file():
            findings.append(
                ScanFinding(
                    origin="tracked",
                    path=relative,
                    reason="tracked entry is missing, a symlink, or not a regular file",
                )
            )
            continue
        try:
            with candidate.open("rb") as stream:
                prefix = stream.read(_READ_PREFIX_BYTES)
        except OSError as exc:
            raise DistributionGuardError(
                "unable to read a tracked repository file"
            ) from exc
        findings.extend(
            scan_entry(origin="tracked", path=relative, prefix=prefix)
        )
    return len(tracked), tuple(sorted(findings))


def _zip_members(artifact: Path) -> Iterable[tuple[str, bytes, bool]]:
    try:
        with zipfile.ZipFile(artifact) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(set(names)) != len(names):
                raise DistributionGuardError(
                    f"distribution artifact {artifact.name} has duplicate members"
                )
            for member in sorted(members, key=lambda item: item.filename):
                if member.is_dir():
                    continue
                unix_mode = member.external_attr >> 16
                is_symlink = (unix_mode & 0o170000) == 0o120000
                if is_symlink:
                    yield member.filename, b"", False
                    continue
                with archive.open(member, "r") as stream:
                    yield member.filename, stream.read(_READ_PREFIX_BYTES), True
    except (OSError, zipfile.BadZipFile) as exc:
        raise DistributionGuardError(
            f"unable to inspect distribution artifact {artifact.name}"
        ) from exc


def _tar_members(artifact: Path) -> Iterable[tuple[str, bytes, bool]]:
    try:
        with tarfile.open(artifact, mode="r:*") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(set(names)) != len(names):
                raise DistributionGuardError(
                    f"distribution artifact {artifact.name} has duplicate members"
                )
            for member in sorted(members, key=lambda item: item.name):
                if member.isdir():
                    continue
                if not member.isfile():
                    yield member.name, b"", False
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    yield member.name, b"", False
                    continue
                with stream:
                    yield member.name, stream.read(_READ_PREFIX_BYTES), True
    except (OSError, tarfile.TarError) as exc:
        raise DistributionGuardError(
            f"unable to inspect distribution artifact {artifact.name}"
        ) from exc


def scan_distribution_artifact(artifact: Path) -> tuple[ScanFinding, ...]:
    """Inspect all regular members of one wheel, ZIP, or compressed sdist."""

    if artifact.is_symlink():
        raise DistributionGuardError("distribution artifact is a symlink")
    try:
        resolved = artifact.resolve(strict=True)
    except OSError as exc:
        raise DistributionGuardError("distribution artifact is unavailable") from exc
    if not resolved.is_file():
        raise DistributionGuardError("distribution artifact is not a regular file")

    lowered = resolved.name.lower()
    if lowered.endswith((".whl", ".zip")):
        members = _zip_members(resolved)
    elif lowered.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
        members = _tar_members(resolved)
    else:
        raise DistributionGuardError(
            f"unsupported distribution artifact format: {resolved.name}"
        )

    origin = f"artifact:{resolved.name}"
    findings: list[ScanFinding] = []
    for name, prefix, is_regular in members:
        if not is_regular:
            findings.append(
                ScanFinding(
                    origin=origin,
                    path=name,
                    reason="artifact member is not a regular file",
                )
            )
            continue
        findings.extend(
            scan_entry(
                origin=origin,
                path=name,
                prefix=prefix,
                artifact_member=True,
            )
        )
    return tuple(sorted(findings))


def discover_distribution_artifacts(artifact_dir: Path) -> tuple[Path, ...]:
    """Return the supported top-level wheel/sdist files in stable name order."""

    try:
        root = artifact_dir.resolve(strict=True)
    except OSError as exc:
        raise DistributionGuardError("artifact directory is unavailable") from exc
    if not root.is_dir():
        raise DistributionGuardError("artifact directory is not a directory")
    artifacts = tuple(
        path
        for path in sorted(root.iterdir(), key=lambda item: item.name)
        if path.is_file()
        and path.name.lower().endswith(
            (".whl", ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
        )
    )
    if not artifacts:
        raise DistributionGuardError(
            "artifact directory contains no supported wheel or sdist"
        )
    return artifacts


def scan_distribution(
    *,
    repo_root: Path,
    artifacts: Sequence[Path] = (),
    scan_tracked: bool = True,
) -> DistributionScanReport:
    """Build one sorted report across the selected distribution surfaces."""

    tracked_count = 0
    findings: list[ScanFinding] = []
    if scan_tracked:
        tracked_count, tracked_findings = scan_tracked_repository(repo_root)
        findings.extend(tracked_findings)

    ordered_artifacts = tuple(sorted(artifacts, key=lambda item: item.name))
    if len({artifact.name for artifact in ordered_artifacts}) != len(
        ordered_artifacts
    ):
        raise DistributionGuardError("distribution artifact names must be unique")
    for artifact in ordered_artifacts:
        findings.extend(scan_distribution_artifact(artifact))
    return DistributionScanReport(
        tracked_file_count=tracked_count,
        artifacts=tuple(artifact.name for artifact in ordered_artifacts),
        findings=tuple(sorted(findings)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan tracked files and built packages for market data",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument(
        "--skip-tracked",
        action="store_true",
        help="scan only explicitly supplied distribution artifacts",
    )
    args = parser.parse_args(argv)
    repo_root = cast(Path, args.repo_root)
    artifact_dir = cast(Path | None, args.artifact_dir)
    skip_tracked = cast(bool, args.skip_tracked)
    if skip_tracked and artifact_dir is None:
        parser.error("--skip-tracked requires --artifact-dir")
    try:
        artifacts = (
            discover_distribution_artifacts(artifact_dir)
            if artifact_dir is not None
            else ()
        )
        report = scan_distribution(
            repo_root=repo_root,
            artifacts=artifacts,
            scan_tracked=not skip_tracked,
        )
    except DistributionGuardError as exc:
        parser.exit(2, f"distribution guard failed closed: {exc}\n")
    print(report.to_json())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
