# SPDX-License-Identifier: Apache-2.0
"""Fail-closed orchestration for the scheduled M4 market-pack matrix.

``--plan-only`` resolves the complete catalog without network I/O.  The
scheduled ``--run-matrix`` path then fetches each pack into a runner-local raw
cache, builds it twice, compares every produced byte, matches the repository
manifest, validates both builds, and proves one deterministic MomentumAgent
bundle through verification and replay against the second build.

No wall clock is read here.  The workflow supplies the single explicit
``created_at_ms`` value recorded in evidence manifests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

from core.config import EpisodeConfig
from core.engine import run_episode
from data.builder import BuiltPack
from data.catalog import (
    PackDefinition,
    available_pack_ids,
    fetch_and_build_pack,
    get_pack_definition,
)
from data.validator import PackValidationResult, validate_pack
from harness.scripted import MomentumAgent
from recorder.replay import replay_bundle
from recorder.verify import verify_bundle
from recorder.writer import build_bundle_manifest, record_episode_bundle

PLAN_SCHEMA: Final = "tradeevolve_nightly_catalog_plan/v1"
MATRIX_SCHEMA: Final = "tradeevolve_nightly_matrix_receipt/v1"
MATRIX_ADAPTER: Final = "fetch-build-validate-momentum-verify-replay/v1"
EXPECTED_V1_PACK_COUNT: Final = 13
_MAX_IJSON_INT: Final = 2**53 - 1
_COMPARE_CHUNK_BYTES: Final = 1024 * 1024


class NightlyPrerequisiteError(RuntimeError):
    """The repository cannot support the complete scheduled matrix."""


class NightlyMatrixError(RuntimeError):
    """A matrix step failed before a success receipt could be emitted."""


@dataclass(frozen=True, slots=True)
class NightlyHost:
    """Informational host provenance carried into each evidence bundle."""

    os: str
    arch: str
    python: str

    def to_value(self) -> dict[str, object]:
        return {
            "arch": self.arch,
            "os": self.os,
            "python": self.python,
        }


@dataclass(frozen=True, slots=True)
class NightlyPackPlan:
    pack_id: str
    archive_count: int


@dataclass(frozen=True, slots=True)
class NightlyCatalogPlan:
    packs: tuple[NightlyPackPlan, ...]

    def to_json(self) -> str:
        value: dict[str, object] = {
            "schema": PLAN_SCHEMA,
            "mode": "plan-only",
            "pack_count": len(self.packs),
            "packs": [
                {
                    "pack_id": pack.pack_id,
                    "archive_count": pack.archive_count,
                }
                for pack in self.packs
            ],
            "matrix_adapter": MATRIX_ADAPTER,
        }
        return _canonical_json(value)


@dataclass(frozen=True, slots=True)
class NightlyEvidenceReceipt:
    """Successful record, stored-byte verification, and economic replay."""

    run_id: str
    episode_id: str
    bundle_root: str
    files_compared: tuple[str, ...]
    decisions_compared: int

    def to_value(self) -> dict[str, object]:
        return {
            "bundle_root": self.bundle_root,
            "decisions_compared": self.decisions_compared,
            "episode_id": self.episode_id,
            "files_compared": list(self.files_compared),
            "run_id": self.run_id,
            "verify_verdict": "COMPLETE",
        }


@dataclass(frozen=True, slots=True)
class NightlyPackReceipt:
    """Successful byte, manifest, validation, and replay evidence for one pack."""

    pack_id: str
    content_hash: str
    pack_file_count: int
    validation: PackValidationResult
    evidence: NightlyEvidenceReceipt

    def to_value(self) -> dict[str, object]:
        return {
            "committed_manifest_match": True,
            "content_hash": self.content_hash,
            "double_build_match": True,
            "evidence": self.evidence.to_value(),
            "pack_file_count": self.pack_file_count,
            "pack_id": self.pack_id,
            "validation": {
                "actionable_bars": self.validation.actionable_bars,
                "bar_rows": self.validation.bar_rows,
                "files": self.validation.files,
                "funding_rows": self.validation.funding_rows,
            },
        }


@dataclass(frozen=True, slots=True)
class NightlyMatrixReceipt:
    """Canonical success-only receipt for the complete scheduled matrix."""

    created_at_ms: int
    host: NightlyHost
    packs: tuple[NightlyPackReceipt, ...]

    def to_json(self) -> str:
        value: dict[str, object] = {
            "schema": MATRIX_SCHEMA,
            "mode": "run-matrix",
            "matrix_adapter": MATRIX_ADAPTER,
            "created_at_ms": self.created_at_ms,
            "host": self.host.to_value(),
            "pack_count": len(self.packs),
            "packs": [pack.to_value() for pack in self.packs],
        }
        return _canonical_json(value)


class CatalogPackRunner(Protocol):
    """Injected catalog visitor used by the offline orchestration test."""

    def __call__(
        self,
        definition: PackDefinition,
        *,
        work_root: Path,
    ) -> None:
        """Visit one catalog definition."""


class PackFetcher(Protocol):
    """Fetch raw inputs and build one named pack."""

    def __call__(
        self,
        pack_id: str,
        *,
        raw_root: Path,
        packs_root: Path,
    ) -> BuiltPack:
        """Return one built pack."""


class PackValidator(Protocol):
    """Read-only semantic pack validation boundary."""

    def __call__(self, pack_dir: str | Path) -> PackValidationResult:
        """Return a successful semantic validation receipt."""


class EvidenceRunner(Protocol):
    """Deterministic agent record/verify/replay boundary."""

    def __call__(
        self,
        *,
        pack_id: str,
        content_hash: str,
        market_alias: str,
        pack_dir: Path,
        replay_pack_dir: Path,
        bundle_dir: Path,
        created_at_ms: int,
        host: NightlyHost,
    ) -> NightlyEvidenceReceipt:
        """Produce and replay one sealed evidence bundle."""


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_catalog_plan(
    *,
    expected_pack_count: int = EXPECTED_V1_PACK_COUNT,
) -> NightlyCatalogPlan:
    """Resolve every catalog entry without network I/O in stable ID order."""

    if expected_pack_count < 1:
        raise ValueError("expected_pack_count must be positive")
    pack_ids = available_pack_ids()
    if pack_ids != tuple(sorted(pack_ids)) or len(set(pack_ids)) != len(pack_ids):
        raise NightlyPrerequisiteError(
            "catalog IDs must be unique and deterministically sorted"
        )
    if len(pack_ids) != expected_pack_count:
        raise NightlyPrerequisiteError(
            f"nightly requires {expected_pack_count} packs; catalog has {len(pack_ids)}"
        )

    plans: list[NightlyPackPlan] = []
    for pack_id in pack_ids:
        definition = get_pack_definition(pack_id)
        if definition.pack_id != pack_id:
            raise NightlyPrerequisiteError(
                "catalog key does not match resolved pack definition"
            )
        if not definition.archives:
            raise NightlyPrerequisiteError(
                f"catalog pack {pack_id} has no source archives"
            )
        plans.append(
            NightlyPackPlan(
                pack_id=pack_id,
                archive_count=len(definition.archives),
            )
        )
    return NightlyCatalogPlan(packs=tuple(plans))


def run_catalog_matrix(
    runner: CatalogPackRunner,
    *,
    work_root: Path,
    expected_pack_count: int = EXPECTED_V1_PACK_COUNT,
) -> NightlyCatalogPlan:
    """Invoke an injected visitor once per complete catalog entry."""

    plan = build_catalog_plan(expected_pack_count=expected_pack_count)
    for pack in plan.packs:
        runner(get_pack_definition(pack.pack_id), work_root=work_root)
    return plan


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _prepare_external_roots(
    *,
    repo_root: Path,
    work_root: Path,
    raw_root: Path,
) -> tuple[Path, Path, Path]:
    try:
        repository = repo_root.resolve(strict=True)
    except OSError as exc:
        raise NightlyPrerequisiteError("repository root is unavailable") from exc
    if not repository.is_dir() or repo_root.is_symlink():
        raise NightlyPrerequisiteError(
            "repository root must be a regular directory"
        )

    if work_root.is_symlink():
        raise NightlyPrerequisiteError("work root must not be a symlink")
    if raw_root.is_symlink():
        raise NightlyPrerequisiteError("raw root must not be a symlink")
    work = work_root.resolve(strict=False)
    raw = raw_root.resolve(strict=False)
    if _is_within(work, repository) or _is_within(raw, repository):
        raise NightlyPrerequisiteError(
            "work and raw roots must remain outside the repository"
        )
    if _is_within(work, raw) or _is_within(raw, work):
        raise NightlyPrerequisiteError(
            "work and raw roots must not contain one another"
        )
    if work.exists():
        if not work.is_dir() or any(work.iterdir()):
            raise NightlyPrerequisiteError(
                "work root must be absent or an empty directory"
            )
    try:
        work.mkdir(parents=True, exist_ok=True)
        raw.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NightlyPrerequisiteError(
            "unable to prepare runner-local work and raw roots"
        ) from exc
    if not raw.is_dir():
        raise NightlyPrerequisiteError("raw root must be a directory")
    return repository, work, raw


def _read_repository_manifests(
    repository: Path,
    plan: NightlyCatalogPlan,
) -> dict[str, bytes]:
    """Read every expected manifest before the first possible fetch."""

    manifests: dict[str, bytes] = {}
    packs_root = repository / "packs"
    if packs_root.is_symlink():
        raise NightlyPrerequisiteError("repository packs path is a symlink")
    for pack in plan.packs:
        pack_root = packs_root / pack.pack_id
        manifest_path = pack_root / "manifest.json"
        if (
            pack_root.is_symlink()
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
        ):
            raise NightlyPrerequisiteError(
                f"missing committed manifest for {pack.pack_id}"
            )
        try:
            manifests[pack.pack_id] = manifest_path.read_bytes()
        except OSError as exc:
            raise NightlyPrerequisiteError(
                f"cannot read committed manifest for {pack.pack_id}"
            ) from exc
    return manifests


def _pack_inventory(pack_dir: Path) -> tuple[str, ...]:
    if pack_dir.is_symlink() or not pack_dir.is_dir():
        raise NightlyMatrixError(f"built pack is not a regular directory: {pack_dir}")
    paths: list[str] = []
    for candidate in sorted(
        pack_dir.rglob("*"),
        key=lambda item: item.relative_to(pack_dir).as_posix(),
    ):
        relative = candidate.relative_to(pack_dir).as_posix()
        if candidate.is_symlink():
            raise NightlyMatrixError(
                f"built pack contains a symlink: {relative}"
            )
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise NightlyMatrixError(
                f"built pack contains a non-regular entry: {relative}"
            )
        paths.append(relative)
    if not paths:
        raise NightlyMatrixError("built pack is empty")
    return tuple(paths)


def _same_file_bytes(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_stream, right.open("rb") as right_stream:
            while True:
                left_chunk = left_stream.read(_COMPARE_CHUNK_BYTES)
                right_chunk = right_stream.read(_COMPARE_CHUNK_BYTES)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError as exc:
        raise NightlyMatrixError("cannot compare built pack bytes") from exc


def _compare_builds(left: Path, right: Path) -> tuple[str, ...]:
    left_inventory = _pack_inventory(left)
    right_inventory = _pack_inventory(right)
    if left_inventory != right_inventory:
        raise NightlyMatrixError("double build produced different file inventories")
    for relative in left_inventory:
        if not _same_file_bytes(left / relative, right / relative):
            raise NightlyMatrixError(
                f"double build byte mismatch: {relative}"
            )
    return left_inventory


def _require_expected_build(
    built: BuiltPack,
    *,
    packs_root: Path,
    pack_id: str,
) -> Path:
    expected = packs_root / pack_id
    if (
        built.directory.is_symlink()
        or built.manifest_path.is_symlink()
        or not built.directory.is_dir()
        or not built.manifest_path.is_file()
    ):
        raise NightlyMatrixError(f"builder returned an invalid pack for {pack_id}")
    try:
        actual_root = built.directory.resolve(strict=True)
        expected_root = expected.resolve(strict=True)
        actual_manifest = built.manifest_path.resolve(strict=True)
    except OSError as exc:
        raise NightlyMatrixError(
            f"builder returned unavailable paths for {pack_id}"
        ) from exc
    if actual_root != expected_root:
        raise NightlyMatrixError(
            f"builder returned an unexpected output directory for {pack_id}"
        )
    if actual_manifest != actual_root / "manifest.json":
        raise NightlyMatrixError(
            f"builder returned an unexpected manifest path for {pack_id}"
        )
    return actual_root


def _momentum_agent_manifest() -> dict[str, object]:
    return {
        "schema": "agent_manifest/v1",
        "name": "momentum",
        "adapter": "in_process",
        "model_id": "none",
        "endpoint_domains": [],
        "inference_params": {},
        "prompt_sha256": None,
        "agent_version": "0.1.0",
        "image_sha256": None,
    }


def run_momentum_evidence(
    *,
    pack_id: str,
    content_hash: str,
    market_alias: str,
    pack_dir: Path,
    replay_pack_dir: Path,
    bundle_dir: Path,
    created_at_ms: int,
    host: NightlyHost,
) -> NightlyEvidenceReceipt:
    """Run, record, verify, and exactly replay the deterministic reference agent."""

    if created_at_ms < 0 or created_at_ms > _MAX_IJSON_INT:
        raise NightlyMatrixError("created_at_ms is outside the I-JSON integer range")
    if bundle_dir.exists() or bundle_dir.is_symlink():
        raise NightlyMatrixError("evidence bundle directory already exists")
    identity = hashlib.sha256(
        (
            pack_id
            + "\0"
            + content_hash
            + "\0"
            + str(created_at_ms)
        ).encode("utf-8")
    ).hexdigest()
    run_id = "run_" + identity[:16]
    episode_id = "ep_" + identity[16:32]
    result = run_episode(
        pack_dir=pack_dir,
        agent=MomentumAgent(market=market_alias),
        config=EpisodeConfig(),
        run_id=run_id,
        episode_id=episode_id,
    )
    if result.pack.pack_id != pack_id:
        raise NightlyMatrixError("engine loaded a different pack_id")
    if result.pack.content_hash != content_hash:
        raise NightlyMatrixError("engine loaded a different pack content_hash")

    agent_manifest = _momentum_agent_manifest()
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=created_at_ms,
        host=host.to_value(),
    )
    record_episode_bundle(
        bundle_dir,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )
    verification = verify_bundle(bundle_dir)
    if not verification.is_complete or verification.root is None:
        raise NightlyMatrixError(
            "bundle verification failed: "
            f"{verification.verdict}: {verification.message}"
        )
    replay = replay_bundle(bundle_dir, pack_dir=replay_pack_dir)
    if replay.run_id != run_id or replay.bundle_root != verification.root:
        raise NightlyMatrixError(
            "verification and replay receipts do not identify the same bundle"
        )
    return NightlyEvidenceReceipt(
        run_id=run_id,
        episode_id=episode_id,
        bundle_root=replay.bundle_root,
        files_compared=replay.files_compared,
        decisions_compared=replay.decisions_compared,
    )


@dataclass(frozen=True, slots=True)
class NightlyServices:
    """Injection surface keeping ordinary tests entirely offline."""

    fetcher: PackFetcher = fetch_and_build_pack
    validator: PackValidator = validate_pack
    evidence_runner: EvidenceRunner = run_momentum_evidence


def execute_nightly_matrix(
    *,
    repo_root: Path,
    work_root: Path,
    raw_root: Path,
    created_at_ms: int,
    host: NightlyHost,
    expected_pack_count: int = EXPECTED_V1_PACK_COUNT,
    services: NightlyServices | None = None,
) -> NightlyMatrixReceipt:
    """Execute the complete matrix and return a receipt only after all gates pass."""

    if created_at_ms < 0 or created_at_ms > _MAX_IJSON_INT:
        raise ValueError("created_at_ms is outside the I-JSON integer range")
    if not host.os or not host.arch or not host.python:
        raise ValueError("host fields must be non-empty")

    plan = build_catalog_plan(expected_pack_count=expected_pack_count)
    repository, work, raw = _prepare_external_roots(
        repo_root=repo_root,
        work_root=work_root,
        raw_root=raw_root,
    )
    committed_manifests = _read_repository_manifests(repository, plan)
    active_services = services or NightlyServices()

    receipts: list[NightlyPackReceipt] = []
    for pack in plan.packs:
        definition = get_pack_definition(pack.pack_id)
        pack_work = work / pack.pack_id
        try:
            pack_work.mkdir()
        except OSError as exc:
            raise NightlyMatrixError(
                f"cannot create work directory for {pack.pack_id}"
            ) from exc
        build_a_root = pack_work / "build-a"
        build_b_root = pack_work / "build-b"
        built_a = active_services.fetcher(
            pack.pack_id,
            raw_root=raw,
            packs_root=build_a_root,
        )
        built_b = active_services.fetcher(
            pack.pack_id,
            raw_root=raw,
            packs_root=build_b_root,
        )
        pack_a = _require_expected_build(
            built_a,
            packs_root=build_a_root,
            pack_id=pack.pack_id,
        )
        pack_b = _require_expected_build(
            built_b,
            packs_root=build_b_root,
            pack_id=pack.pack_id,
        )
        inventory = _compare_builds(pack_a, pack_b)

        try:
            generated_manifest = built_a.manifest_path.read_bytes()
        except OSError as exc:
            raise NightlyMatrixError(
                f"cannot read generated manifest for {pack.pack_id}"
            ) from exc
        if generated_manifest != committed_manifests[pack.pack_id]:
            raise NightlyMatrixError(
                f"generated manifest differs from repository bytes: {pack.pack_id}"
            )

        validation_a = active_services.validator(pack_a)
        validation_b = active_services.validator(pack_b)
        if validation_a != validation_b:
            raise NightlyMatrixError(
                f"double-build validation receipts differ: {pack.pack_id}"
            )
        if (
            validation_a.pack_id != pack.pack_id
            or validation_a.content_hash != built_a.content_hash
            or validation_a.content_hash != built_b.content_hash
        ):
            raise NightlyMatrixError(
                f"builder and validator identities differ: {pack.pack_id}"
            )
        evidence = active_services.evidence_runner(
            pack_id=pack.pack_id,
            content_hash=validation_a.content_hash,
            market_alias=definition.config.market_alias,
            pack_dir=pack_a,
            replay_pack_dir=pack_b,
            bundle_dir=pack_work / "bundle",
            created_at_ms=created_at_ms,
            host=host,
        )
        receipts.append(
            NightlyPackReceipt(
                pack_id=pack.pack_id,
                content_hash=validation_a.content_hash,
                pack_file_count=len(inventory),
                validation=validation_a,
                evidence=evidence,
            )
        )
    return NightlyMatrixReceipt(
        created_at_ms=created_at_ms,
        host=host,
        packs=tuple(receipts),
    )


def _current_host() -> NightlyHost:
    return NightlyHost(
        os=platform.system().lower(),
        arch=platform.machine().lower(),
        python=platform.python_version(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan or execute the deterministic M4 nightly matrix",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--run-matrix", action="store_true")
    parser.add_argument(
        "--expected-pack-count",
        type=int,
        default=EXPECTED_V1_PACK_COUNT,
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--created-at-ms", type=int)
    args = parser.parse_args(argv)

    expected_pack_count = cast(int, args.expected_pack_count)
    plan_only = cast(bool, args.plan_only)
    work_root = cast(Path | None, args.work_root)
    raw_root = cast(Path | None, args.raw_root)
    created_at_ms = cast(int | None, args.created_at_ms)
    if plan_only:
        if work_root is not None or raw_root is not None or created_at_ms is not None:
            parser.error(
                "--work-root, --raw-root, and --created-at-ms require --run-matrix"
            )
        try:
            plan = build_catalog_plan(expected_pack_count=expected_pack_count)
        except (NightlyPrerequisiteError, ValueError) as exc:
            parser.exit(1, f"nightly catalog prerequisite failed: {exc}\n")
        print(plan.to_json())
        return 0

    missing = [
        flag
        for flag, value in (
            ("--work-root", work_root),
            ("--raw-root", raw_root),
            ("--created-at-ms", created_at_ms),
        )
        if value is None
    ]
    if missing:
        parser.error("--run-matrix requires " + ", ".join(missing))
    try:
        receipt = execute_nightly_matrix(
            repo_root=cast(Path, args.repo_root),
            work_root=cast(Path, work_root),
            raw_root=cast(Path, raw_root),
            created_at_ms=cast(int, created_at_ms),
            host=_current_host(),
            expected_pack_count=expected_pack_count,
        )
    except Exception as exc:
        parser.exit(1, f"nightly matrix failed closed: {exc}\n")
    print(receipt.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
