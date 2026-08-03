# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from data.builder import BuiltPack
from data.catalog import PackDefinition, available_pack_ids
from data.nightly import (
    MATRIX_ADAPTER,
    MATRIX_SCHEMA,
    PLAN_SCHEMA,
    NightlyEvidenceReceipt,
    NightlyHost,
    NightlyMatrixError,
    NightlyPrerequisiteError,
    NightlyServices,
    build_catalog_plan,
    execute_nightly_matrix,
    main,
    run_catalog_matrix,
    run_momentum_evidence,
)
from data.validator import PackValidationResult

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PACK = ROOT / "fixtures" / "golden-mini" / "pack"
HOST = NightlyHost(os="test", arch="test", python="3.12")
CREATED_AT_MS = 1_700_000_000_000


def _content_hash(pack_id: str) -> str:
    return "sha256:" + hashlib.sha256(pack_id.encode("utf-8")).hexdigest()


def _manifest_bytes(pack_id: str) -> bytes:
    return json.dumps(
        {
            "content_hash": _content_hash(pack_id),
            "pack_id": pack_id,
            "schema": "test_pack_manifest/v1",
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _write_repository_manifests(
    repository: Path,
    *,
    omit: str | None = None,
    overrides: Mapping[str, bytes] | None = None,
) -> dict[str, bytes]:
    repository.mkdir()
    values: dict[str, bytes] = {}
    replacements = dict(overrides or {})
    for pack_id in available_pack_ids():
        if pack_id == omit:
            continue
        payload = replacements.get(pack_id, _manifest_bytes(pack_id))
        pack_dir = repository / "packs" / pack_id
        pack_dir.mkdir(parents=True)
        (pack_dir / "manifest.json").write_bytes(payload)
        values[pack_id] = payload
    return values


def test_catalog_plan_is_complete_sorted_and_canonical() -> None:
    pack_ids = available_pack_ids()
    plan = build_catalog_plan(expected_pack_count=len(pack_ids))
    encoded = plan.to_json()

    assert tuple(pack.pack_id for pack in plan.packs) == pack_ids
    assert all(pack.archive_count > 0 for pack in plan.packs)
    assert encoded == json.dumps(
        json.loads(encoded),
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    value = json.loads(encoded)
    assert value["schema"] == PLAN_SCHEMA
    assert value["matrix_adapter"] == MATRIX_ADAPTER


def test_catalog_plan_refuses_wrong_pack_count() -> None:
    with pytest.raises(NightlyPrerequisiteError, match="nightly requires"):
        build_catalog_plan(
            expected_pack_count=len(available_pack_ids()) + 1,
        )


def test_catalog_matrix_uses_injected_adapter_in_sorted_order(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Path]] = []

    def runner(
        definition: PackDefinition,
        *,
        work_root: Path,
    ) -> None:
        calls.append((definition.pack_id, work_root))

    pack_ids = available_pack_ids()
    plan = run_catalog_matrix(
        runner,
        work_root=tmp_path,
        expected_pack_count=len(pack_ids),
    )

    assert tuple(pack.pack_id for pack in plan.packs) == pack_ids
    assert calls == [(pack_id, tmp_path) for pack_id in pack_ids]


def test_plan_only_cli_has_no_network_side_effect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        (
            "--plan-only",
            "--expected-pack-count",
            str(len(available_pack_ids())),
        )
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "plan-only"
    assert output["matrix_adapter"] == MATRIX_ADAPTER


def test_execute_matrix_uses_two_builds_and_injected_offline_services(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    manifests = _write_repository_manifests(repository)
    work_root = tmp_path / "work"
    raw_root = tmp_path / "raw"
    fetch_calls: list[tuple[str, Path, Path]] = []
    validation_calls: list[Path] = []
    evidence_calls: list[tuple[str, Path, Path, Path]] = []

    def fetcher(
        pack_id: str,
        *,
        raw_root: Path,
        packs_root: Path,
    ) -> BuiltPack:
        fetch_calls.append((pack_id, raw_root, packs_root))
        output = packs_root / pack_id
        output.mkdir(parents=True)
        manifest = output / "manifest.json"
        manifest.write_bytes(manifests[pack_id])
        series = output / "series.test"
        series.write_bytes(("deterministic:" + pack_id).encode("ascii"))
        return BuiltPack(
            directory=output,
            manifest_path=manifest,
            content_hash=_content_hash(pack_id),
            series_paths=(series,),
        )

    def validator(pack_dir: str | Path) -> PackValidationResult:
        root = Path(pack_dir)
        validation_calls.append(root)
        return PackValidationResult(
            pack_id=root.name,
            content_hash=_content_hash(root.name),
            files=1,
            bar_rows=2,
            funding_rows=1,
            actionable_bars=1,
        )

    def evidence_runner(
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
        assert content_hash == _content_hash(pack_id)
        assert market_alias == "BTC"
        assert created_at_ms == CREATED_AT_MS
        assert host == HOST
        assert pack_dir != replay_pack_dir
        evidence_calls.append(
            (pack_id, pack_dir, replay_pack_dir, bundle_dir)
        )
        identity = hashlib.sha256(pack_id.encode("ascii")).hexdigest()
        return NightlyEvidenceReceipt(
            run_id="run_" + identity[:16],
            episode_id="ep_" + identity[16:32],
            bundle_root=_content_hash("bundle-" + pack_id),
            files_compared=("ledger.jsonl", "metrics.json"),
            decisions_compared=1,
        )

    receipt = execute_nightly_matrix(
        repo_root=repository,
        work_root=work_root,
        raw_root=raw_root,
        created_at_ms=CREATED_AT_MS,
        host=HOST,
        expected_pack_count=len(available_pack_ids()),
        services=NightlyServices(
            fetcher=fetcher,
            validator=validator,
            evidence_runner=evidence_runner,
        ),
    )

    pack_ids = available_pack_ids()
    assert tuple(pack.pack_id for pack in receipt.packs) == pack_ids
    assert [pack_id for pack_id, _, _ in fetch_calls] == [
        pack_id
        for pack_id in pack_ids
        for _ in range(2)
    ]
    assert all(call_raw_root == raw_root for _, call_raw_root, _ in fetch_calls)
    assert len(validation_calls) == len(pack_ids) * 2
    assert [pack_id for pack_id, _, _, _ in evidence_calls] == list(pack_ids)
    encoded = receipt.to_json()
    assert encoded == json.dumps(
        json.loads(encoded),
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    value = json.loads(encoded)
    assert value["schema"] == MATRIX_SCHEMA
    assert value["pack_count"] == len(pack_ids)
    assert all(
        pack["double_build_match"]
        and pack["committed_manifest_match"]
        and pack["evidence"]["verify_verdict"] == "COMPLETE"
        for pack in value["packs"]
    )


def test_execute_matrix_preflights_every_manifest_before_fetch(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    missing = available_pack_ids()[-1]
    _write_repository_manifests(repository, omit=missing)
    fetch_calls: list[str] = []

    def fetcher(
        pack_id: str,
        *,
        raw_root: Path,
        packs_root: Path,
    ) -> BuiltPack:
        del raw_root, packs_root
        fetch_calls.append(pack_id)
        raise AssertionError("fetch must not run")

    with pytest.raises(
        NightlyPrerequisiteError,
        match=f"missing committed manifest for {missing}",
    ):
        execute_nightly_matrix(
            repo_root=repository,
            work_root=tmp_path / "work",
            raw_root=tmp_path / "raw",
            created_at_ms=CREATED_AT_MS,
            host=HOST,
            expected_pack_count=len(available_pack_ids()),
            services=NightlyServices(fetcher=fetcher),
        )

    assert fetch_calls == []


@pytest.mark.parametrize("mismatch", ["build", "repository"])
def test_execute_matrix_refuses_byte_or_repository_manifest_mismatch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    repository = tmp_path / "repo"
    first_pack = available_pack_ids()[0]
    overrides = (
        {first_pack: b"repository-manifest"}
        if mismatch == "repository"
        else None
    )
    _write_repository_manifests(repository, overrides=overrides)
    evidence_calls: list[str] = []

    def fetcher(
        pack_id: str,
        *,
        raw_root: Path,
        packs_root: Path,
    ) -> BuiltPack:
        del raw_root
        output = packs_root / pack_id
        output.mkdir(parents=True)
        manifest = output / "manifest.json"
        manifest.write_bytes(_manifest_bytes(pack_id))
        series = output / "series.test"
        variant = (
            b"second-build"
            if mismatch == "build" and packs_root.name == "build-b"
            else b"same-build"
        )
        series.write_bytes(variant)
        return BuiltPack(
            directory=output,
            manifest_path=manifest,
            content_hash=_content_hash(pack_id),
            series_paths=(series,),
        )

    def evidence_runner(
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
        del (
            content_hash,
            market_alias,
            pack_dir,
            replay_pack_dir,
            bundle_dir,
            created_at_ms,
            host,
        )
        evidence_calls.append(pack_id)
        raise AssertionError("evidence must not run")

    expected = (
        "double build byte mismatch"
        if mismatch == "build"
        else "generated manifest differs from repository bytes"
    )
    with pytest.raises(NightlyMatrixError, match=expected):
        execute_nightly_matrix(
            repo_root=repository,
            work_root=tmp_path / "work",
            raw_root=tmp_path / "raw",
            created_at_ms=CREATED_AT_MS,
            host=HOST,
            expected_pack_count=len(available_pack_ids()),
            services=NightlyServices(
                fetcher=fetcher,
                evidence_runner=evidence_runner,
            ),
        )

    assert evidence_calls == []


def test_momentum_evidence_is_offline_deterministic_and_replayable(
    tmp_path: Path,
) -> None:
    manifest_value = cast(
        dict[str, object],
        json.loads((GOLDEN_PACK / "manifest.json").read_text(encoding="utf-8")),
    )
    pack_id = cast(str, manifest_value["pack_id"])
    content_hash = cast(str, manifest_value["content_hash"])
    first = run_momentum_evidence(
        pack_id=pack_id,
        content_hash=content_hash,
        market_alias="BTC",
        pack_dir=GOLDEN_PACK,
        replay_pack_dir=GOLDEN_PACK,
        bundle_dir=tmp_path / "bundle-a",
        created_at_ms=CREATED_AT_MS,
        host=HOST,
    )
    second = run_momentum_evidence(
        pack_id=pack_id,
        content_hash=content_hash,
        market_alias="BTC",
        pack_dir=GOLDEN_PACK,
        replay_pack_dir=GOLDEN_PACK,
        bundle_dir=tmp_path / "bundle-b",
        created_at_ms=CREATED_AT_MS,
        host=HOST,
    )

    assert first == second
    assert first.files_compared == (
        "ledger.jsonl",
        "ledger_stress_2x.jsonl",
        "metrics.json",
    )
    assert first.decisions_compared > 0
