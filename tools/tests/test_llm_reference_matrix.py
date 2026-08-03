# SPDX-License-Identifier: Apache-2.0
"""Offline safety tests for the reusable LLM reference-matrix runner."""

from __future__ import annotations

import io
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.cost import ModelPricing, RunCostEstimate
from agents.llm import CACHED_INPUT_EXTENSION_KEY, LLMConfig
from agents.manifest import build_llm_manifest
from core.config import EpisodeConfig
from harness.protocol import AgentReply
from recorder.replay import ReplayResult
from recorder.verify import VerificationResult
from report.generator import ReportArtifacts
from spec.canonical import canonical_bytes, sha256_prefixed
from tools import llm_reference_matrix as matrix

_DIGEST = "sha256:" + "a" * 64


def _settings(tmp_path: Path) -> matrix.MatrixSettings:
    return matrix.MatrixSettings(
        repo_root=matrix.ROOT,
        packs_root=matrix.ROOT / "packs",
        output_root=tmp_path / "matrix-output",
        llm_config=LLMConfig(
            base_url=matrix.FIREWORKS_BASE_URL,
            model="accounts/fireworks/models/test-public-model",
            temperature="0",
            max_tokens=128,
            timeout_seconds=100,
            api_key_env_name="FIREWORKS_API_KEY",
        ),
        pricing=ModelPricing.from_strings(
            input_usd_per_million="1",
            cached_input_usd_per_million="0.25",
            output_usd_per_million="2",
        ),
        episode_config=EpisodeConfig(),
        agent_image_digest=_DIGEST,
        gateway_image_digest=_DIGEST,
        guard_image_digest=_DIGEST,
        host_http_port=18_080,
    )


def _prior_failure_receipt(
    tmp_path: Path,
    *,
    reserve: str = "0.100000",
    model: str = "accounts/fireworks/models/test-public-model",
    provider: str = matrix.FIREWORKS_DOMAIN,
    trailing_lf: bool = False,
) -> tuple[Path, str]:
    raw = canonical_bytes(
        {
            "schema": "llm_reference_failure_reserve/v2",
            "status": "FAILED_CLOSED",
            "provider": provider,
            "model": model,
            "accounting_reserve_usd": reserve,
            "accounting_basis": {
                "bound_method": "test authorization bound",
                "original_failure_receipt_sha256": _DIGEST,
            },
        }
    )
    if trailing_lf:
        raw += b"\n"
    path = tmp_path / "prior-failure.json"
    path.write_bytes(raw)
    return path, sha256_prefixed(raw)


def _prior_failure_receipt_v3(
    tmp_path: Path,
    *,
    prior_reserve: str = "0.100000",
    smoke_actual: str = "0.005000",
    total_reserve: str = "0.105000",
    basis_overrides: Mapping[str, object] | None = None,
    drop_basis_key: str | None = None,
    linked_smoke_actual: str | None = None,
    smoke_overrides: Mapping[str, object] | None = None,
    smoke_actual_overrides: Mapping[str, object] | None = None,
    trailing_lf: bool = True,
) -> tuple[Path, str]:
    review_root = tmp_path / "docs" / "review"
    review_root.mkdir(parents=True)
    v2_document: dict[str, object] = {
        "schema": "llm_reference_failure_reserve/v2",
        "status": "FAILED_CLOSED",
        "provider": matrix.FIREWORKS_DOMAIN,
        "model": "accounts/fireworks/models/test-public-model",
        "accounting_reserve_usd": prior_reserve,
        "accounting_basis": {
            "bound_method": "test authorization bound",
            "original_failure_receipt_sha256": _DIGEST,
        },
    }
    v2_raw = canonical_bytes(v2_document) + b"\n"
    v2_path = review_root / "prior-v2.json"
    v2_path.write_bytes(v2_raw)

    linked_actual = (
        smoke_actual
        if linked_smoke_actual is None
        else linked_smoke_actual
    )
    smoke_actual_document: dict[str, object] = {
        "usage_source": "provider_reported_with_cache_detail",
        "attempts": 1,
        "input_tokens": 1_000,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 1_000,
        "output_tokens": 2_000,
        "cost_usd": linked_actual,
    }
    if smoke_actual_overrides is not None:
        smoke_actual_document.update(smoke_actual_overrides)
    smoke_document: dict[str, object] = {
        "schema": "llm_reference_smoke_failure_receipt/v1",
        "status": "FAILED_CLOSED",
        "reason": "http_non_2xx",
        "http_status": 400,
        "pack_id": matrix.EXPECTED_PACK_IDS[0],
        "estimate_receipt_sha256": _DIGEST,
        "attempt_receipt_sha256": _DIGEST,
        "provider": {
            "domain": matrix.FIREWORKS_DOMAIN,
            "model": "accounts/fireworks/models/test-public-model",
        },
        "images": {
            "agent": _DIGEST,
            "gateway": _DIGEST,
            "guard": _DIGEST,
        },
        "response": {
            "bytes": 100,
            "sha256": _DIGEST,
        },
        "limits": {
            "matrix_cost_usd_exclusive": "300.000000",
            "pack_cost_usd_exclusive": "25.000000",
        },
        "accounting_reserve_usd": linked_actual,
        "actual": smoke_actual_document,
    }
    if smoke_overrides is not None:
        smoke_document.update(smoke_overrides)
    smoke_payload = canonical_bytes(smoke_document)
    smoke_raw = smoke_payload + b"\n"
    smoke_path = review_root / "smoke-failure.json"
    smoke_path.write_bytes(smoke_raw)

    basis: dict[str, object] = {
        "bound_method": "prior_cumulative_plus_exact_smoke_actual",
        "prior_reserve_receipt_path": "docs/review/prior-v2.json",
        "prior_reserve_receipt_sha256": sha256_prefixed(v2_raw),
        "prior_reserve_usd": prior_reserve,
        "smoke_actual_cost_usd": smoke_actual,
        "smoke_failure_receipt_path": "docs/review/smoke-failure.json",
        "smoke_failure_receipt_sha256": sha256_prefixed(smoke_payload),
        "smoke_failure_storage_sha256": sha256_prefixed(smoke_raw),
    }
    if basis_overrides is not None:
        basis.update(basis_overrides)
    if drop_basis_key is not None:
        basis.pop(drop_basis_key)
    document: dict[str, object] = {
        "schema": "llm_reference_failure_reserve/v3",
        "status": "FAILED_CLOSED",
        "provider": matrix.FIREWORKS_DOMAIN,
        "model": "accounts/fireworks/models/test-public-model",
        "accounting_reserve_usd": total_reserve,
        "accounting_basis": basis,
    }
    raw = canonical_bytes(document)
    if trailing_lf:
        raw += b"\n"
    path = review_root / "prior-v3.json"
    path.write_bytes(raw)
    return path, sha256_prefixed(raw)


def _materialize_smoke_attempt(
    settings: matrix.MatrixSettings,
    smoke_plan: matrix.SmokePlan,
) -> None:
    matrix._write_new(
        matrix._smoke_paths(settings)["attempt"],
        canonical_bytes(
            matrix._smoke_attempt_document(settings, smoke_plan)
        ),
    )


def _plans(cost: str = "0.010000") -> tuple[matrix.PackPlan, ...]:
    result: list[matrix.PackPlan] = []
    for pack_id in matrix.EXPECTED_PACK_IDS:
        estimate = RunCostEstimate(
            turns=1,
            attempts_per_turn=2,
            input_tokens=10,
            output_tokens=20,
            estimated_cost_usd=Decimal(cost),
        )
        document: dict[str, object] = {
            "schema": "test_estimate/v1",
            "pack_id": pack_id,
            "estimate": estimate.to_mapping(),
        }
        result.append(
            matrix.PackPlan(
                pack_id=pack_id,
                pack_path=matrix.ROOT / "packs" / pack_id,
                content_hash="sha256:" + "b" * 64,
                bars_total=1,
                estimate=estimate,
                estimate_document=document,
            )
        )
    return tuple(result)


def _smoke_plan(
    cost: str = "0.000030",
    *,
    authorization_cost: str | None = None,
) -> matrix.SmokePlan:
    observation: dict[str, object] = {
        "schema": "observation/v1",
        "episode": {"turn": 0},
        "markets": {"BTC": {}},
        "risk": {},
    }
    estimate = RunCostEstimate(
        turns=1,
        attempts_per_turn=1,
        input_tokens=10,
        output_tokens=10,
        estimated_cost_usd=Decimal(cost),
    )
    document: dict[str, object] = {
        "schema": "test_smoke_estimate/v1",
        "estimate": estimate.to_mapping(),
    }
    return matrix.SmokePlan(
        pack_id=matrix.EXPECTED_PACK_IDS[0],
        observation=observation,
        estimate=estimate,
        estimate_document=document,
        authorization_bound=(
            None
            if authorization_cost is None
            else replace(
                estimate,
                estimated_cost_usd=Decimal(authorization_cost),
            )
        ),
    )


def _smoke_evidence(
    settings: matrix.MatrixSettings,
    smoke_plan: matrix.SmokePlan,
) -> matrix.SmokeEvidence:
    response = canonical_bytes(
        {
            "schema": "action/v1",
            "intent_kind": "leverage_target",
            "target": {"BTC": "0"},
            "max_slippage_bps": 100,
            "usage": {"input_tokens": 12, "output_tokens": 4},
            "ext": {CACHED_INPUT_EXTENSION_KEY: 2},
        }
    )
    usage = matrix._smoke_usage(settings, smoke_plan, response)
    return matrix.SmokeEvidence(
        response_bytes=response,
        receipt_document=matrix._smoke_receipt(
            settings=settings,
            smoke_plan=smoke_plan,
            response_bytes=response,
            usage=usage,
        ),
    )


def _decision(attempts: Sequence[object]) -> dict[str, object]:
    return {
        "meant": {
            "status": "parsed",
            "action": {},
            "rejected": None,
        },
        "said": {
            "attempts": list(attempts),
        },
    }


def _valid_response(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> bytes:
    return canonical_bytes(
        {
            "schema": "action/v1",
            "intent_kind": "leverage_target",
            "target": {"BTC": "0"},
            "max_slippage_bps": 100,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "ext": {
                CACHED_INPUT_EXTENSION_KEY: cached_input_tokens,
            },
        }
    )


def _invalid_response(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> bytes:
    return canonical_bytes(
        {
            "error": "invalid_contract",
            "reason": "action_contract_invalid",
            "usage": {
                "cached_input_tokens": cached_input_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        }
    )


def _attempt_record(
    *,
    attempt: int,
    raw_ref: str,
    response: bytes,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, object]:
    return {
        "attempt": attempt,
        "raw_ref": raw_ref,
        "raw_sha256": sha256_prefixed(response),
        "raw_bytes": len(response),
        "latency_ms": 1,
        "http_status": 200 if attempt == 2 else 400,
        "token_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        "transport": "http",
    }


def test_default_execution_is_network_free_and_write_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    called = False

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("default plan crossed a write/network boundary")

    monkeypatch.setattr(matrix, "DockerSandbox", forbidden_side_effect)
    monkeypatch.setattr(matrix, "_write_new", forbidden_side_effect)
    monkeypatch.setattr(matrix, "_isolation_plan", forbidden_side_effect)

    def paid_runner(
        _settings_value: matrix.MatrixSettings,
        _plan: matrix.PackPlan,
        _manifest: object,
    ) -> None:
        nonlocal called
        called = True
        raise AssertionError("paid runner crossed the confirmation seam")

    output = io.StringIO()
    result = matrix.execute_matrix(
        settings,
        _plans(),
        _smoke_plan(),
        confirmed=False,
        resume=False,
        stdout=output,
        paid_runner=cast(matrix.PaidRunner, paid_runner),
    )

    assert result is None
    assert called is False
    assert not settings.output_root.exists()
    assert "no sandbox started" in output.getvalue()
    assert "--confirm-transmission-and-spend" in output.getvalue()


def test_pack_estimate_must_be_strictly_below_25_dollars() -> None:
    with pytest.raises(matrix.MatrixError, match="strictly below"):
        matrix._enforce_estimate_budget(
            _plans("25.000000"),
            _smoke_plan(),
            prior_reserve=Decimal(0),
        )


def test_matrix_estimate_must_be_strictly_below_300_dollars() -> None:
    with pytest.raises(matrix.MatrixError, match="accounted matrix estimate"):
        matrix._enforce_estimate_budget(
            _plans("24.000000"),
            _smoke_plan(),
            prior_reserve=Decimal(0),
        )


def test_accounted_estimate_includes_prior_reserve_and_smoke() -> None:
    total = matrix._enforce_estimate_budget(
        _plans("1.000000"),
        _smoke_plan("0.500000"),
        prior_reserve=Decimal("2.250000"),
    )
    assert total == Decimal("15.750000")


def test_all_estimate_receipts_exist_before_paid_runner_is_called(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()

    class PaidSeamReached(RuntimeError):
        pass

    def paid_runner(
        settings_value: matrix.MatrixSettings,
        _plan: matrix.PackPlan,
        _manifest: Mapping[str, object],
    ) -> None:
        assert all(
            (
                settings_value.output_root
                / "estimates"
                / f"{plan.pack_id}.json"
            ).read_bytes()
            == canonical_bytes(plan.estimate_document)
            for plan in plans
        )
        raise PaidSeamReached

    def paid_smoke_runner(
        settings_value: matrix.MatrixSettings,
        smoke_value: matrix.SmokePlan,
        _manifest: Mapping[str, object],
    ) -> matrix.SmokeEvidence:
        assert (
            settings_value.output_root / "smoke" / "estimate.json"
        ).read_bytes() == canonical_bytes(smoke_value.estimate_document)
        assert all(
            (
                settings_value.output_root
                / "estimates"
                / f"{plan.pack_id}.json"
            ).read_bytes()
            == canonical_bytes(plan.estimate_document)
            for plan in plans
        )
        assert (
            settings_value.output_root / "authorization.json"
        ).is_file()
        _materialize_smoke_attempt(settings_value, smoke_value)
        return _smoke_evidence(settings_value, smoke_value)

    with pytest.raises(PaidSeamReached):
        matrix.execute_matrix(
            settings,
            plans,
            smoke_plan,
            confirmed=True,
            resume=False,
            stdout=io.StringIO(),
            paid_runner=paid_runner,
            paid_smoke_runner=paid_smoke_runner,
        )

    assert not (settings.output_root / "bundles").exists()


def test_plan_prints_the_true_smoke_authorization_bound(
    tmp_path: Path,
) -> None:
    output = io.StringIO()
    matrix.execute_matrix(
        _settings(tmp_path),
        _plans(),
        _smoke_plan("0.010000", authorization_cost="0.020000"),
        confirmed=False,
        resume=False,
        stdout=output,
    )

    assert "estimate=$0.010000 authorized_cost<=$0.020000" in (
        output.getvalue()
    )


def test_positive_prior_reserve_requires_failure_receipt_hash(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="requires its failure receipt"):
        replace(
            _settings(tmp_path),
            prior_failed_run_reserve_usd=Decimal("0.100000"),
        )


def test_prior_reserve_receipt_is_exactly_bound_and_validated(
    tmp_path: Path,
) -> None:
    path, receipt_hash = _prior_failure_receipt(
        tmp_path,
        trailing_lf=True,
    )
    settings = replace(
        _settings(tmp_path),
        prior_failed_run_reserve_usd=Decimal("0.100000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    matrix.execute_matrix(
        settings,
        _plans(),
        _smoke_plan(),
        confirmed=False,
        resume=False,
        stdout=io.StringIO(),
    )

    path.write_bytes(path.read_bytes().replace(b"0.100000", b"0.100001"))
    with pytest.raises(matrix.MatrixError, match="configured hash"):
        matrix.execute_matrix(
            settings,
            _plans(),
            _smoke_plan(),
            confirmed=False,
            resume=False,
            stdout=io.StringIO(),
        )


@pytest.mark.parametrize(
    ("reserve", "provider", "model", "message"),
    (
        (
            "0.100001",
            matrix.FIREWORKS_DOMAIN,
            "accounts/fireworks/models/test-public-model",
            "reserve does not match",
        ),
        (
            "0.100000",
            "api.example.invalid",
            "accounts/fireworks/models/test-public-model",
            "provider does not match",
        ),
        (
            "0.100000",
            matrix.FIREWORKS_DOMAIN,
            "accounts/fireworks/models/other",
            "model does not match",
        ),
    ),
)
def test_prior_reserve_receipt_rejects_semantic_mismatch(
    tmp_path: Path,
    reserve: str,
    provider: str,
    model: str,
    message: str,
) -> None:
    path, receipt_hash = _prior_failure_receipt(
        tmp_path,
        reserve=reserve,
        provider=provider,
        model=model,
    )
    settings = replace(
        _settings(tmp_path),
        prior_failed_run_reserve_usd=Decimal("0.100000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    with pytest.raises(matrix.MatrixError, match=message):
        matrix.execute_matrix(
            settings,
            _plans(),
            _smoke_plan(),
            confirmed=False,
            resume=False,
            stdout=io.StringIO(),
        )


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("wrong_schema", "schema is not"),
        ("missing_provider", "provider must be text"),
        ("missing_model", "model must be text"),
        ("missing_basis", "accounting_basis must be an object"),
        ("bad_original_hash", "original incident hash is invalid"),
    ),
)
def test_prior_reserve_receipt_requires_v2_incident_identity(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    document: dict[str, object] = {
        "schema": "llm_reference_failure_reserve/v2",
        "status": "FAILED_CLOSED",
        "provider": matrix.FIREWORKS_DOMAIN,
        "model": "accounts/fireworks/models/test-public-model",
        "accounting_reserve_usd": "0.100000",
        "accounting_basis": {
            "bound_method": "test authorization bound",
            "original_failure_receipt_sha256": _DIGEST,
        },
    }
    if case == "wrong_schema":
        document["schema"] = "unrelated/v9"
    elif case == "missing_provider":
        document.pop("provider")
    elif case == "missing_model":
        document.pop("model")
    elif case == "missing_basis":
        document.pop("accounting_basis")
    else:
        cast(dict[str, object], document["accounting_basis"])[
            "original_failure_receipt_sha256"
        ] = "not-a-digest"
    raw = canonical_bytes(document)
    path = tmp_path / "prior-v2.json"
    path.write_bytes(raw)
    settings = replace(
        _settings(tmp_path),
        prior_failed_run_reserve_usd=Decimal("0.100000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=sha256_prefixed(raw),
    )

    with pytest.raises(matrix.MatrixError, match=message):
        matrix.execute_matrix(
            settings,
            _plans(),
            _smoke_plan(),
            confirmed=False,
            resume=False,
            stdout=io.StringIO(),
        )


def test_prior_reserve_receipt_accepts_hash_bound_v3_components(
    tmp_path: Path,
) -> None:
    path, receipt_hash = _prior_failure_receipt_v3(tmp_path)
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )
    output = io.StringIO()

    matrix.execute_matrix(
        settings,
        _plans(),
        _smoke_plan(),
        confirmed=False,
        resume=False,
        stdout=output,
    )

    assert (
        "prior failed-run accounting reserve: $0.105000"
        in output.getvalue()
    )
    assert not settings.output_root.exists()


def test_authorization_binds_only_the_cumulative_v3_reserve(
    tmp_path: Path,
) -> None:
    path, receipt_hash = _prior_failure_receipt_v3(tmp_path)
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    document = matrix._authorization_document(
        settings,
        _plans(),
        _smoke_plan(),
    )

    assert document["accounting"] == {
        "prior_failed_run_reserve_usd": "0.105000",
        "prior_failure_receipt_sha256": receipt_hash,
    }


@pytest.mark.parametrize(
    ("basis_overrides", "message"),
    (
        (
            {"bound_method": "unverified_sum"},
            "bound_method is not supported",
        ),
        (
            {"prior_reserve_receipt_sha256": "sha256:" + "b" * 64},
            "linked prior reserve receipt bytes",
        ),
        (
            {"smoke_failure_receipt_sha256": "sha256:" + "b" * 64},
            "canonical payload mismatches source hash",
        ),
        (
            {"smoke_failure_storage_sha256": "sha256:" + "b" * 64},
            "linked smoke failure receipt bytes",
        ),
        (
            {"smoke_actual_cost_usd": "-0.005000"},
            "six-decimal amount",
        ),
        (
            {"smoke_actual_cost_usd": "NaN"},
            "six-decimal amount",
        ),
    ),
)
def test_prior_reserve_receipt_v3_rejects_mutated_components(
    tmp_path: Path,
    basis_overrides: Mapping[str, object],
    message: str,
) -> None:
    path, receipt_hash = _prior_failure_receipt_v3(
        tmp_path,
        basis_overrides=basis_overrides,
    )
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    with pytest.raises(matrix.MatrixError, match=message):
        matrix.execute_matrix(
            settings,
            _plans(),
            _smoke_plan(),
            confirmed=False,
            resume=False,
            stdout=io.StringIO(),
        )


def test_prior_reserve_receipt_v3_rejects_bad_component_sum(
    tmp_path: Path,
) -> None:
    path, receipt_hash = _prior_failure_receipt_v3(
        tmp_path,
        smoke_actual="0.004999",
    )
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    with pytest.raises(matrix.MatrixError, match="do not sum"):
        matrix.execute_matrix(
            settings,
            _plans(),
            _smoke_plan(),
            confirmed=False,
            resume=False,
            stdout=io.StringIO(),
        )


def test_prior_reserve_receipt_v3_requires_exact_component_fields(
    tmp_path: Path,
) -> None:
    path, receipt_hash = _prior_failure_receipt_v3(
        tmp_path,
        drop_basis_key="smoke_failure_receipt_sha256",
    )
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    with pytest.raises(matrix.MatrixError, match="fields are not exact"):
        matrix.execute_matrix(
            settings,
            _plans(),
            _smoke_plan(),
            confirmed=False,
            resume=False,
            stdout=io.StringIO(),
        )


def test_prior_reserve_receipt_v3_rejects_linked_amount_mismatch(
    tmp_path: Path,
) -> None:
    path, receipt_hash = _prior_failure_receipt_v3(
        tmp_path,
        linked_smoke_actual="0.004999",
    )
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    with pytest.raises(matrix.MatrixError, match="amount mismatches v3"):
        matrix.execute_matrix(
            settings,
            _plans(),
            _smoke_plan(),
            confirmed=False,
            resume=False,
            stdout=io.StringIO(),
        )


def test_prior_reserve_receipt_v3_recomputes_smoke_cost(
    tmp_path: Path,
) -> None:
    path, receipt_hash = _prior_failure_receipt_v3(
        tmp_path,
        smoke_actual_overrides={
            "input_tokens": 1_001,
            "uncached_input_tokens": 1_001,
        },
    )
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    with pytest.raises(matrix.MatrixError, match="amount mismatches v3"):
        matrix.execute_matrix(
            settings,
            _plans(),
            _smoke_plan(),
            confirmed=False,
            resume=False,
            stdout=io.StringIO(),
        )


def test_prior_reserve_receipt_v3_rejects_wrong_smoke_outcome(
    tmp_path: Path,
) -> None:
    path, receipt_hash = _prior_failure_receipt_v3(
        tmp_path,
        smoke_overrides={"http_status": 500},
    )
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    with pytest.raises(matrix.MatrixError, match="outcome is invalid"):
        matrix.execute_matrix(
            settings,
            _plans(),
            _smoke_plan(),
            confirmed=False,
            resume=False,
            stdout=io.StringIO(),
        )


def test_authorization_receipt_binds_prior_reserve_and_hash(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    path, receipt_hash = _prior_failure_receipt(tmp_path)
    changed = replace(
        settings,
        prior_failed_run_reserve_usd=Decimal("0.100000"),
        prior_failure_receipt_path=path,
        prior_failure_receipt_sha256=receipt_hash,
    )

    with pytest.raises(matrix.MatrixError, match="byte-match"):
        matrix._preflight_authorization(changed, plans, smoke_plan)


def test_passing_smoke_cannot_resume_with_a_missing_pack_estimate(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    _materialize_smoke_attempt(settings, smoke_plan)
    smoke_evidence = _smoke_evidence(settings, smoke_plan)
    matrix._materialize_smoke(settings, smoke_plan, smoke_evidence)
    (
        settings.output_root
        / "estimates"
        / f"{plans[0].pack_id}.json"
    ).unlink()
    manifest = build_llm_manifest(
        settings.llm_config,
        image_digest=settings.agent_image_digest,
    )

    with pytest.raises(matrix.MatrixError, match="paid smoke exists"):
        matrix._preflight_existing(
            settings,
            plans,
            manifest,
            smoke_evidence,
            resume=True,
        )


def test_failed_smoke_stops_before_any_pack_runner(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    smoke_plan = _smoke_plan()
    pack_called = False

    def failed_smoke(
        _settings_value: matrix.MatrixSettings,
        _smoke_value: matrix.SmokePlan,
        _manifest: Mapping[str, object],
    ) -> matrix.SmokeEvidence:
        raise matrix.MatrixError("smoke response is not a valid action/v1")

    def paid_runner(
        _settings_value: matrix.MatrixSettings,
        _plan: matrix.PackPlan,
        _manifest: Mapping[str, object],
    ) -> None:
        nonlocal pack_called
        pack_called = True

    with pytest.raises(matrix.MatrixError, match="smoke response"):
        matrix.execute_matrix(
            settings,
            _plans(),
            smoke_plan,
            confirmed=True,
            resume=False,
            stdout=io.StringIO(),
            paid_runner=paid_runner,
            paid_smoke_runner=failed_smoke,
        )

    assert pack_called is False
    assert not (settings.output_root / "bundles").exists()
    assert (
        settings.output_root / "smoke" / "estimate.json"
    ).read_bytes() == canonical_bytes(smoke_plan.estimate_document)


def test_paid_smoke_runner_must_publish_pre_call_attempt_marker(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    smoke_plan = _smoke_plan()

    def markerless_smoke(
        settings_value: matrix.MatrixSettings,
        smoke_value: matrix.SmokePlan,
        _manifest: Mapping[str, object],
    ) -> matrix.SmokeEvidence:
        return _smoke_evidence(settings_value, smoke_value)

    with pytest.raises(matrix.MatrixError, match="pre-call attempt"):
        matrix.execute_matrix(
            settings,
            _plans(),
            smoke_plan,
            confirmed=True,
            resume=False,
            stdout=io.StringIO(),
            paid_smoke_runner=markerless_smoke,
        )

    assert not (settings.output_root / "smoke" / "response.json").exists()


def test_smoke_actual_may_not_exceed_authorization_bound(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    smoke_plan = _smoke_plan(
        "0.000010",
        authorization_cost="0.000018",
    )

    with pytest.raises(matrix.MatrixError, match="authorization bound"):
        _smoke_evidence(settings, smoke_plan)


def test_incomplete_paid_smoke_attempt_is_terminal_on_resume(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    _materialize_smoke_attempt(settings, smoke_plan)

    with pytest.raises(matrix.MatrixError, match="terminal"):
        matrix._preflight_smoke(settings, smoke_plan)


def test_default_paid_smoke_persists_terminal_http_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    response = _invalid_response(
        input_tokens=100,
        cached_input_tokens=25,
        output_tokens=20,
    )

    class FailedSmokeSandbox:
        def __init__(self, _plan: object) -> None:
            pass

        def __enter__(self) -> FailedSmokeSandbox:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def decide(self, _request: object) -> AgentReply:
            return AgentReply(
                body=response,
                http_status=400,
                transport="http",
            )

    monkeypatch.setattr(matrix, "DockerSandbox", FailedSmokeSandbox)
    with pytest.raises(matrix.MatrixError, match="HTTP 400"):
        matrix._run_paid_smoke(
            settings,
            smoke_plan,
            {},
        )

    smoke_root = settings.output_root / "smoke"
    assert (smoke_root / "failure-response.json").read_bytes() == response
    failure = matrix._load_canonical_object(
        smoke_root / "failure-receipt.json"
    )
    assert failure["status"] == "FAILED_CLOSED"
    assert failure["reason"] == "http_non_2xx"
    actual = cast(Mapping[str, object], failure["actual"])
    assert actual["cached_input_tokens"] == 25
    with pytest.raises(matrix.MatrixError, match="terminal"):
        matrix._preflight_smoke(settings, smoke_plan)


def test_terminal_failed_smoke_resume_invokes_no_paid_runner(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    _materialize_smoke_attempt(settings, smoke_plan)
    matrix._materialize_smoke_failure(
        settings=settings,
        smoke_plan=smoke_plan,
        reason="http_non_2xx",
        http_status=400,
        response_bytes=_invalid_response(
            input_tokens=100,
            cached_input_tokens=25,
            output_tokens=20,
        ),
    )
    smoke_calls = 0
    pack_calls = 0

    def paid_smoke(
        _settings_value: matrix.MatrixSettings,
        _smoke_value: matrix.SmokePlan,
        _manifest: Mapping[str, object],
    ) -> matrix.SmokeEvidence:
        nonlocal smoke_calls
        smoke_calls += 1
        raise AssertionError("terminal smoke must not be repurchased")

    def paid_pack(
        _settings_value: matrix.MatrixSettings,
        _plan: matrix.PackPlan,
        _manifest: Mapping[str, object],
    ) -> None:
        nonlocal pack_calls
        pack_calls += 1
        raise AssertionError("terminal smoke must stop every pack")

    with pytest.raises(matrix.MatrixError, match="terminal"):
        matrix.execute_matrix(
            settings,
            plans,
            smoke_plan,
            confirmed=True,
            resume=True,
            stdout=io.StringIO(),
            paid_runner=paid_pack,
            paid_smoke_runner=paid_smoke,
        )

    assert smoke_calls == 0
    assert pack_calls == 0


def test_cleanup_failure_after_paid_smoke_preserves_terminal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    response = _valid_response(
        input_tokens=100,
        cached_input_tokens=25,
        output_tokens=20,
    )

    class CleanupFailureSandbox:
        def __init__(self, _plan: object) -> None:
            pass

        def __enter__(self) -> CleanupFailureSandbox:
            return self

        def __exit__(self, *_args: object) -> None:
            raise RuntimeError("synthetic cleanup failure")

        def decide(self, _request: object) -> AgentReply:
            return AgentReply(
                body=response,
                http_status=200,
                transport="http",
            )

    monkeypatch.setattr(matrix, "DockerSandbox", CleanupFailureSandbox)
    with pytest.raises(
        matrix.MatrixError,
        match="paid-call boundary",
    ):
        matrix._run_paid_smoke(
            settings,
            smoke_plan,
            {},
        )

    failure = matrix._load_canonical_object(
        settings.output_root / "smoke" / "failure-receipt.json"
    )
    assert failure["reason"] == "paid_call_or_cleanup_error"
    assert failure["http_status"] == 200
    with pytest.raises(matrix.MatrixError, match="terminal"):
        matrix._preflight_smoke(settings, smoke_plan)


def test_full_pack_writes_attempt_receipt_before_episode_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()
    first = plans[0]
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)

    class EnteredSandbox:
        def __init__(self, _plan: object) -> None:
            pass

        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    def interrupted_episode(**_kwargs: object) -> object:
        attempt = matrix._paths(settings, first)["attempt"]
        assert attempt.read_bytes() == canonical_bytes(
            matrix._pack_attempt_document(settings, first)
        )
        raise RuntimeError("synthetic host interruption")

    monkeypatch.setattr(matrix, "DockerSandbox", EnteredSandbox)
    monkeypatch.setattr(matrix, "run_episode", interrupted_episode)
    with pytest.raises(RuntimeError, match="host interruption"):
        matrix._run_paid_bundle(settings, first, {})

    assert matrix._paths(settings, first)["attempt"].is_file()
    assert not matrix._paths(settings, first)["bundle"].exists()


def test_output_layout_rejects_symlinked_evidence_parent(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.output_root.mkdir()
    external = tmp_path / "external-estimates"
    external.mkdir()
    (settings.output_root / "estimates").symlink_to(
        external,
        target_is_directory=True,
    )

    with pytest.raises(matrix.MatrixError, match="real directory"):
        matrix._preflight_output_layout(settings)


def test_output_layout_rejects_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-root"
    external.mkdir()
    linked = tmp_path / "linked-root"
    linked.symlink_to(external, target_is_directory=True)
    settings = replace(
        _settings(tmp_path),
        output_root=linked / "matrix-output",
    )

    with pytest.raises(matrix.MatrixError, match="traverse a symlink"):
        matrix._preflight_output_layout(settings)


def test_output_layout_rejects_unexpected_inventory(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.output_root.mkdir()
    (settings.output_root / "unexpected.txt").write_text(
        "not evidence",
        encoding="utf-8",
    )

    with pytest.raises(matrix.MatrixError, match="unexpected"):
        matrix._preflight_output_layout(settings)


def test_actual_usage_sums_every_stored_attempt(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    decisions = bundle / "decisions"
    raw = bundle / "raw"
    decisions.mkdir(parents=True)
    raw.mkdir()
    first_response = _invalid_response(
        input_tokens=100,
        cached_input_tokens=40,
        output_tokens=20,
    )
    second_response = _valid_response(
        input_tokens=110,
        cached_input_tokens=50,
        output_tokens=10,
    )
    (raw / "0000-a1.txt").write_bytes(first_response)
    (raw / "0000-a2.txt").write_bytes(second_response)
    attempts = [
        _attempt_record(
            attempt=1,
            raw_ref="raw/0000-a1.txt",
            response=first_response,
            input_tokens=100,
            output_tokens=20,
        ),
        _attempt_record(
            attempt=2,
            raw_ref="raw/0000-a2.txt",
            response=second_response,
            input_tokens=110,
            output_tokens=10,
        ),
    ]
    (decisions / "0000.json").write_bytes(
        canonical_bytes(_decision(attempts))
    )

    settings = replace(
        _settings(tmp_path),
        pricing=ModelPricing.from_strings(
            input_usd_per_million="1",
            cached_input_usd_per_million="0.25",
            output_usd_per_million="2",
        ),
    )
    usage = matrix._collect_usage(bundle, settings)

    assert usage.attempts == 2
    assert usage.input_tokens == 210
    assert usage.cached_input_tokens == 90
    assert usage.uncached_input_tokens == 120
    assert usage.output_tokens == 30
    assert usage.cost_usd == Decimal("0.000203")


def test_pack_actual_may_not_exceed_authorization_bound(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    plan = _plans(cost="0.010000")[0]
    root = "sha256:" + "c" * 64
    verification = VerificationResult(
        verdict="COMPLETE",
        message="complete",
        stream=None,
        seq=None,
        path=None,
        root=root,
        last_good={},
        inventory={},
        turns=(),
    )
    replay = ReplayResult(
        run_id="run_test",
        bundle_root=root,
        files_compared=(),
        decisions_compared=0,
    )
    usage = matrix.UsageReceipt(
        attempts=1,
        input_tokens=1,
        cached_input_tokens=0,
        uncached_input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0.010001"),
    )

    with pytest.raises(matrix.MatrixError, match="authorization bound"):
        matrix._actual_receipt(
            settings=settings,
            plan=plan,
            manifest={
                "run_id": "run_test",
                "episode_id": "ep_test",
                "agent_manifest_sha256": _DIGEST,
                "pack": {"manifest_sha256": _DIGEST},
            },
            verification=verification,
            replay=replay,
            usage=usage,
            artifacts=ReportArtifacts("", "", ""),
        )


@pytest.mark.parametrize(
    "attempts",
    (
        [],
        [{"token_usage": None}],
    ),
)
def test_actual_usage_refuses_unpriced_provider_attempts(
    tmp_path: Path,
    attempts: list[object],
) -> None:
    bundle = tmp_path / "bundle"
    decisions = bundle / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "0000.json").write_bytes(
        canonical_bytes(_decision(attempts))
    )

    with pytest.raises(matrix.MatrixError, match="usage"):
        matrix._collect_usage(
            bundle,
            replace(
                _settings(tmp_path),
                pricing=ModelPricing.from_strings(
                    input_usd_per_million="1",
                    cached_input_usd_per_million="0.25",
                    output_usd_per_million="1",
                ),
            ),
        )


def test_actual_usage_refuses_rejected_reference_action(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    decisions = bundle / "decisions"
    decisions.mkdir(parents=True)
    document = _decision(
        [
            {
                "token_usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                },
            }
        ]
    )
    document["meant"] = {
        "status": "rejected",
        "action": None,
        "rejected": {
            "reason": "schema_invalid",
            "detail": "invalid",
            "validator_error": "invalid",
            "attempts": 1,
        },
    }
    (decisions / "0000.json").write_bytes(canonical_bytes(document))

    with pytest.raises(matrix.MatrixError, match="was rejected"):
        matrix._collect_usage(
            bundle,
            replace(
                _settings(tmp_path),
                pricing=ModelPricing.from_strings(
                    input_usd_per_million="1",
                    cached_input_usd_per_million="0.25",
                    output_usd_per_million="1",
                ),
            ),
        )


def test_actual_usage_bounds_first_attempt_agent_error(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    bundle = tmp_path / "bundle"
    decisions = bundle / "decisions"
    observations = bundle / "observations"
    decisions.mkdir(parents=True)
    observations.mkdir()
    observation = _smoke_plan().observation
    observation_bytes = canonical_bytes(observation)
    (observations / "0000.json").write_bytes(observation_bytes)
    document = _decision([])
    document.update(
        {
            "turn": 0,
            "saw": {
                "observation_ref": "observations/0000.json",
                "observation_sha256": sha256_prefixed(
                    observation_bytes
                ),
            },
        }
    )
    document["meant"] = {
        "status": "rejected",
        "action": None,
        "rejected": {
            "reason": "agent_error",
            "detail": "agent transport failed",
            "validator_error": None,
            "attempts": 1,
        },
    }
    (decisions / "0000.json").write_bytes(canonical_bytes(document))

    usage = matrix._collect_usage(bundle, settings)

    assert usage.attempts == 0
    assert usage.cost_usd == Decimal("0.000000")
    assert len(usage.unpriced_attempts) == 1
    missed = usage.unpriced_attempts[0]
    assert missed.turn == 0
    assert missed.attempt == 1
    assert missed.reason == "agent_error"
    assert missed.input_tokens_upper_bound > 0
    assert missed.output_tokens_upper_bound == 128
    assert missed.cost_upper_bound_usd > 0


def test_actual_usage_bounds_second_attempt_timeout(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    bundle = tmp_path / "bundle"
    decisions = bundle / "decisions"
    observations = bundle / "observations"
    raw = bundle / "raw"
    decisions.mkdir(parents=True)
    observations.mkdir()
    raw.mkdir()
    observation = _smoke_plan().observation
    observation_bytes = canonical_bytes(observation)
    (observations / "0000.json").write_bytes(observation_bytes)
    first_response = _invalid_response(
        input_tokens=100,
        cached_input_tokens=40,
        output_tokens=20,
    )
    (raw / "0000-a1.txt").write_bytes(first_response)
    document = _decision(
        [
            _attempt_record(
                attempt=1,
                raw_ref="raw/0000-a1.txt",
                response=first_response,
                input_tokens=100,
                output_tokens=20,
            )
        ]
    )
    document.update(
        {
            "turn": 0,
            "saw": {
                "observation_ref": "observations/0000.json",
                "observation_sha256": sha256_prefixed(
                    observation_bytes
                ),
            },
        }
    )
    document["meant"] = {
        "status": "rejected",
        "action": None,
        "rejected": {
            "reason": "timeout",
            "detail": "deadline",
            "validator_error": "schema_invalid: invalid action",
            "attempts": 2,
        },
    }
    (decisions / "0000.json").write_bytes(canonical_bytes(document))

    usage = matrix._collect_usage(bundle, settings)

    assert usage.attempts == 1
    assert usage.input_tokens == 100
    assert usage.cached_input_tokens == 40
    assert len(usage.unpriced_attempts) == 1
    assert usage.unpriced_attempts[0].attempt == 2
    assert (
        usage.unpriced_attempts[0].input_tokens_upper_bound
        > matrix.AUTHORIZATION_FRAMING_TOKENS
        + matrix.MAX_RETRY_CORRECTION_BYTES
    )


@pytest.mark.parametrize(
    ("attempts", "validator_error"),
    [
        (1, "unexpected"),
        (2, None),
    ],
)
def test_missed_decision_retry_shape_fails_closed(
    tmp_path: Path,
    attempts: int,
    validator_error: str | None,
) -> None:
    bundle = tmp_path / "bundle"
    decisions = bundle / "decisions"
    decisions.mkdir(parents=True)
    document = _decision([])
    document["meant"] = {
        "status": "rejected",
        "action": None,
        "rejected": {
            "reason": "agent_error",
            "detail": "failed",
            "validator_error": validator_error,
            "attempts": attempts,
        },
    }
    (decisions / "0000.json").write_bytes(canonical_bytes(document))

    with pytest.raises(
        matrix.MatrixError,
        match="missed-decision",
    ):
        matrix._collect_usage(bundle, _settings(tmp_path))


def test_bounded_actual_receipt_preserves_known_and_upper_cost(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans(cost="0.010000")
    plan = plans[0]
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(
        settings,
        plans,
        _smoke_plan(),
    )
    root = "sha256:" + "c" * 64
    verification = VerificationResult(
        verdict="COMPLETE",
        message="complete",
        stream=None,
        seq=None,
        path=None,
        root=root,
        last_good={},
        inventory={},
        turns=(),
    )
    replay = ReplayResult(
        run_id="run_test",
        bundle_root=root,
        files_compared=(),
        decisions_compared=1,
    )
    usage = matrix.UsageReceipt(
        attempts=1,
        input_tokens=100,
        cached_input_tokens=10,
        uncached_input_tokens=90,
        output_tokens=10,
        cost_usd=Decimal("0.006000"),
        unpriced_attempts=(
            matrix.UnpricedAttempt(
                turn=0,
                attempt=1,
                reason="agent_error",
                observation_sha256=_DIGEST,
                input_tokens_upper_bound=100,
                output_tokens_upper_bound=10,
                cost_upper_bound_usd=Decimal("0.001000"),
            ),
        ),
    )

    receipt = matrix._actual_receipt(
        settings=settings,
        plan=plan,
        manifest={
            "run_id": "run_test",
            "episode_id": "ep_test",
            "agent_manifest_sha256": _DIGEST,
            "pack": {"manifest_sha256": _DIGEST},
        },
        verification=verification,
        replay=replay,
        usage=usage,
        artifacts=ReportArtifacts("", "", ""),
    )

    actual = cast(Mapping[str, object], receipt["actual"])
    comparison = cast(
        Mapping[str, object],
        receipt["estimate_comparison"],
    )
    assert receipt["schema"] == "llm_reference_actual_receipt/v2"
    assert actual["cost_usd"] == "0.006000"
    assert actual["unpriced_cost_reserve_usd"] == "0.001000"
    assert actual["cost_upper_bound_usd"] == "0.007000"
    assert actual["unpriced_attempts"] == 1
    assert comparison["within_2x"] is True
    assert (
        comparison["comparison_basis"]
        == "entire_closed_actual_cost_interval"
    )
    assert matrix._receipt_cost(receipt) == Decimal("0.006000")
    assert matrix._receipt_accounting_cost(receipt) == Decimal(
        "0.007000"
    )


def test_bounded_actual_receipt_enforces_authorization_on_upper(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    plan = _plans(cost="0.010000")[0]
    root = "sha256:" + "c" * 64
    usage = matrix.UsageReceipt(
        attempts=1,
        input_tokens=1,
        cached_input_tokens=0,
        uncached_input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0.009000"),
        unpriced_attempts=(
            matrix.UnpricedAttempt(
                turn=0,
                attempt=1,
                reason="timeout",
                observation_sha256=_DIGEST,
                input_tokens_upper_bound=1,
                output_tokens_upper_bound=1,
                cost_upper_bound_usd=Decimal("0.002000"),
            ),
        ),
    )

    with pytest.raises(matrix.MatrixError, match="accounting upper"):
        matrix._actual_receipt(
            settings=settings,
            plan=plan,
            manifest={
                "run_id": "run_test",
                "episode_id": "ep_test",
                "agent_manifest_sha256": _DIGEST,
                "pack": {"manifest_sha256": _DIGEST},
            },
            verification=VerificationResult(
                verdict="COMPLETE",
                message="complete",
                stream=None,
                seq=None,
                path=None,
                root=root,
                last_good={},
                inventory={},
                turns=(),
            ),
            replay=ReplayResult(
                run_id="run_test",
                bundle_root=root,
                files_compared=(),
                decisions_compared=1,
            ),
            usage=usage,
            artifacts=ReportArtifacts("", "", ""),
        )


def test_immutable_writer_never_replaces_existing_bytes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "receipts" / "pack.json"
    matrix._write_new(target, b"first")
    with pytest.raises(matrix.MatrixError, match="overwrite"):
        matrix._write_new(target, b"second")
    assert target.read_bytes() == b"first"


def test_resume_refuses_non_complete_bundle(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()
    first = plans[0]
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    matrix._write_new(
        matrix._paths(settings, first)["attempt"],
        canonical_bytes(matrix._pack_attempt_document(settings, first)),
    )
    (settings.output_root / "bundles" / first.pack_id).mkdir(parents=True)
    manifest = build_llm_manifest(
        settings.llm_config,
        image_digest=settings.agent_image_digest,
    )

    with pytest.raises(matrix.MatrixError, match="only verified COMPLETE"):
        matrix._preflight_existing(
            settings,
            plans,
            manifest,
            None,
            resume=True,
        )


def test_incomplete_paid_pack_attempt_is_terminal_on_resume(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    plans = _plans()
    smoke_plan = _smoke_plan()
    first = plans[0]
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    _materialize_smoke_attempt(settings, smoke_plan)
    smoke_evidence = _smoke_evidence(settings, smoke_plan)
    matrix._materialize_smoke(settings, smoke_plan, smoke_evidence)
    matrix._write_new(
        matrix._paths(settings, first)["attempt"],
        canonical_bytes(matrix._pack_attempt_document(settings, first)),
    )
    manifest = build_llm_manifest(
        settings.llm_config,
        image_digest=settings.agent_image_digest,
    )

    with pytest.raises(matrix.MatrixError, match="terminal"):
        matrix._preflight_existing(
            settings,
            plans,
            manifest,
            smoke_evidence,
            resume=True,
        )


def test_docker_argv_carries_only_the_credential_name(
    tmp_path: Path,
) -> None:
    plan = matrix._isolation_plan(_settings(tmp_path))
    agent_command = plan.create_commands()[-1]
    key_positions = [
        index
        for index, value in enumerate(agent_command)
        if value == "FIREWORKS_API_KEY"
    ]

    assert len(key_positions) == 1
    assert agent_command[key_positions[0] - 1] == "--env"
    assert all(
        not value.startswith("FIREWORKS_API_KEY=")
        for value in agent_command
    )


def test_model_argument_rejects_credential_shaped_values() -> None:
    with pytest.raises(matrix.MatrixError, match="never a credential"):
        matrix._validate_model("sk-do-not-print-this")


def test_image_arguments_require_exact_content_digests() -> None:
    with pytest.raises(matrix.MatrixError, match="exact sha256"):
        matrix._validate_digest("agent:latest", "agent image digest")


def test_aggregate_receipt_preserves_v1_bytes_without_unpriced_attempts(
    tmp_path: Path,
) -> None:
    prior_path, prior_hash = _prior_failure_receipt_v3(tmp_path)
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=prior_path,
        prior_failure_receipt_sha256=prior_hash,
    )
    plans = _plans()
    smoke_plan = _smoke_plan()
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    smoke_receipt = _smoke_evidence(
        settings,
        smoke_plan,
    ).receipt_document
    evidence: dict[str, matrix.PackEvidence] = {}
    for plan in plans:
        receipt: dict[str, object] = {
            "actual": {
                "cost_usd": "0.001000",
                "attempts": 2,
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "uncached_input_tokens": 6,
                "output_tokens": 5,
            },
            "bundle": {
                "root": "sha256:" + "c" * 64,
            },
        }
        evidence[plan.pack_id] = matrix.PackEvidence(
            receipt_document=receipt,
            report_artifacts=ReportArtifacts("", "", ""),
            reports_present=True,
            receipt_present=True,
        )

    receipt = matrix._aggregate_receipt(
        settings,
        plans,
        evidence,
        smoke_receipt,
    )

    assert receipt["schema"] == "llm_reference_matrix_receipt/v1"
    assert "known_current_run_cost_usd" not in cast(
        Mapping[str, object],
        receipt["accounting"],
    )
    assert "unpriced_attempts" not in cast(
        Mapping[str, object],
        receipt["totals"],
    )
    assert sha256_prefixed(canonical_bytes(receipt)) == (
        "sha256:"
        "cdfd3555861a3c3c1b39fe90e4714586bf3aad6cecb9e518cb3b645ac88f6ce3"
    )


def test_aggregate_receipt_is_canonical_and_deterministic(
    tmp_path: Path,
) -> None:
    prior_path, prior_hash = _prior_failure_receipt_v3(tmp_path)
    settings = replace(
        _settings(tmp_path),
        repo_root=tmp_path,
        prior_failed_run_reserve_usd=Decimal("0.105000"),
        prior_failure_receipt_path=prior_path,
        prior_failure_receipt_sha256=prior_hash,
    )
    plans = _plans()
    smoke_plan = _smoke_plan()
    matrix._ensure_directory(settings.output_root)
    matrix._materialize_estimates(settings, plans, smoke_plan)
    smoke_receipt = _smoke_evidence(
        settings,
        smoke_plan,
    ).receipt_document
    evidence: dict[str, matrix.PackEvidence] = {}
    for index, plan in enumerate(plans):
        bounded_fields: dict[str, object] = {}
        if index == 0:
            bounded_fields = {
                "unpriced_attempts": 1,
                "unpriced_cost_reserve_usd": "0.000500",
                "cost_upper_bound_usd": "0.001500",
            }
        receipt: dict[str, object] = {
            "actual": {
                "cost_usd": "0.001000",
                "attempts": 2,
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "uncached_input_tokens": 6,
                "output_tokens": 5,
                **bounded_fields,
            },
            "bundle": {
                "root": "sha256:" + "c" * 64,
            },
        }
        evidence[plan.pack_id] = matrix.PackEvidence(
            receipt_document=receipt,
            report_artifacts=ReportArtifacts("", "", ""),
            reports_present=True,
            receipt_present=True,
        )

    first = matrix._aggregate_receipt(
        settings,
        plans,
        evidence,
        smoke_receipt,
    )
    second = matrix._aggregate_receipt(
        settings,
        plans,
        evidence,
        smoke_receipt,
    )

    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["schema"] == "llm_reference_matrix_receipt/v2"
    totals = cast(Mapping[str, object], first["totals"])
    assert totals["actual_cost_usd"] == "0.013000"
    assert totals["unpriced_cost_reserve_usd"] == "0.000500"
    assert totals["accounting_cost_upper_bound_usd"] == "0.013500"
    assert totals["attempts"] == 27
    assert totals["reported_attempts"] == 26
    assert totals["unpriced_attempts"] == 1
    assert totals["cached_input_tokens"] == 52
    assert totals["uncached_input_tokens"] == 78
    accounting = cast(Mapping[str, object], first["accounting"])
    assert accounting["smoke_actual_cost_usd"] == "0.000019"
    assert accounting["prior_failure_receipt_sha256"] == prior_hash
    assert accounting["known_current_run_cost_usd"] == "0.013019"
    assert (
        accounting["current_run_unpriced_cost_reserve_usd"]
        == "0.000500"
    )
    assert accounting["accounted_upper_bound_usd"] == "0.118519"


@pytest.mark.skipif(
    not (
        matrix.ROOT / "docs" / "review" / "M3-kimi-failed-run-reserve-v3.json"
    ).is_file(),
    reason=(
        "internal spend receipts are not shipped in the public distribution"
    ),
)
def test_production_kimi_authorization_bounds_are_locked(
    tmp_path: Path,
) -> None:
    base = _settings(tmp_path)
    prior_path = (
        matrix.ROOT
        / "docs"
        / "review"
        / "M3-kimi-failed-run-reserve-v3.json"
    )
    prior_hash = (
        "sha256:"
        "b353ceac15a00f21466e745c4ce89cbb29bfa42677a2c20b17d8df1ba0e9b2aa"
    )
    settings = replace(
        base,
        llm_config=replace(
            base.llm_config,
            model="accounts/fireworks/models/kimi-k2p6",
            max_tokens=512,
            timeout_seconds=600,
        ),
        pricing=ModelPricing.from_strings(
            input_usd_per_million="0.95",
            cached_input_usd_per_million="0.16",
            output_usd_per_million="4",
        ),
        episode_config=replace(
            base.episode_config,
            response_deadline_ms=660_000,
        ),
        prior_failed_run_reserve_usd=Decimal("1.372646"),
        prior_failure_receipt_path=prior_path,
        prior_failure_receipt_sha256=prior_hash,
    )
    matrix._validate_prior_failure_receipt(settings)
    manifest = build_llm_manifest(
        settings.llm_config,
        image_digest=settings.agent_image_digest,
    )
    plans, smoke_plan = matrix._build_plans(settings, manifest)
    accounted = matrix._enforce_estimate_budget(
        plans,
        smoke_plan,
        prior_reserve=settings.prior_failed_run_reserve_usd,
    )

    assert accounted == Decimal("79.305926")
    assert matrix._smoke_authorization_cost(smoke_plan) == Decimal(
        "0.012219"
    )
    assert max(
        matrix._authorization_cost(plan) for plan in plans
    ) == Decimal("16.582354")
    assert not settings.output_root.exists()
