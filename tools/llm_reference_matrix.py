#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Safely run the Fireworks LLM reference baseline across all hydrated packs.

The default invocation is an offline plan: it loads every catalogued pack,
runs the keyless momentum policy only to size the largest observation, and
prints conservative token/cost estimates.  No sandbox is constructed, no
credential source is read, no provider request is sent, and no artifact is
written unless ``--confirm-transmission-and-spend`` is present.

Confirmed runs preserve one immutable COMPLETE bundle per pack, prove an exact
offline replay, render deterministic reports, record provider-reported token
usage and actual cost, conservatively bound any contract-valid IC-6 missed
call whose provider usage is unavailable, and finally seal one canonical
aggregate receipt.
Interrupted or non-COMPLETE bundle directories are never resumed or replaced.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Final, TextIO, cast

ROOT: Final = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.common import (  # noqa: E402
    AgentContractError,
    decode_action_object,
    market_aliases,
    observation_json,
    validate_action_document,
)
from agents.cost import (  # noqa: E402
    ModelPricing,
    RunCostEstimate,
    estimate_run_cost,
    estimate_text_tokens,
)
from agents.llm import (  # noqa: E402
    CACHED_INPUT_EXTENSION_KEY,
    MAX_IJSON_INTEGER,
    MAX_RETRY_CORRECTION_BYTES,
    PROVIDER_REASONING_EFFORT,
    LLMConfig,
    provider_request_controls_json,
)
from agents.manifest import build_llm_manifest  # noqa: E402
from agents.prompt import load_prompt  # noqa: E402
from core.config import EpisodeConfig  # noqa: E402
from core.engine import run_episode  # noqa: E402
from core.pack import load_pack  # noqa: E402
from data.catalog import available_pack_ids  # noqa: E402
from harness.http import HTTPAgentError  # noqa: E402
from harness.protocol import AgentReply, DecisionTimeout  # noqa: E402
from harness.scripted import MomentumAgent  # noqa: E402
from recorder.replay import ReplayResult, replay_bundle  # noqa: E402
from recorder.verify import VerificationResult, verify_bundle  # noqa: E402
from recorder.writer import (  # noqa: E402
    build_bundle_manifest,
    record_episode_bundle,
)
from report.generator import ReportArtifacts, generate_report  # noqa: E402
from sandbox.orchestration import (  # noqa: E402
    DockerSandbox,
    IsolationPlan,
)
from spec.canonical import canonical_bytes, sha256_prefixed  # noqa: E402

JsonObject = dict[str, object]
PaidRunner = Callable[["MatrixSettings", "PackPlan", Mapping[str, object]], None]
PaidSmokeRunner = Callable[
    ["MatrixSettings", "SmokePlan", Mapping[str, object]],
    "SmokeEvidence",
]

FIREWORKS_BASE_URL: Final = "https://api.fireworks.ai/inference/v1"
FIREWORKS_DOMAIN: Final = "api.fireworks.ai"
PACK_COST_LIMIT_USD: Final = Decimal("25")
MATRIX_COST_LIMIT_USD: Final = Decimal("300")
MONEY_QUANTUM: Final = Decimal("0.000001")
PACK_COST_LIMIT_TEXT: Final = "25.000000"
MATRIX_COST_LIMIT_TEXT: Final = "300.000000"
AUTHORIZATION_INPUT_BYTES_PER_TOKEN: Final = 1
AUTHORIZATION_FRAMING_TOKENS: Final = 128
EXPECTED_PACK_IDS: Final = available_pack_ids()

_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MODEL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
_SECRETISH_MODEL: Final = re.compile(
    r"(?i)(?:^(?:sk-|ghp_|github_pat_|xox[baprs]-|AIza)|"
    r"(?:api[_-]?key|access[_-]?token|secret|password|bearer))"
)
_MONEY_TEXT: Final = re.compile(r"(?:0|[1-9][0-9]*)\.[0-9]{6}\Z")
_DECISION_NAME: Final = re.compile(r"[0-9]{4}\.json\Z")
_RAW_RESPONSE_NAME: Final = re.compile(r"[0-9]{4}-a[12]\.txt\Z")


class MatrixError(RuntimeError):
    """The matrix cannot proceed without weakening an evidence or spend gate."""


@dataclass(frozen=True, slots=True)
class MatrixSettings:
    """Public, secret-free inputs shared by every pack run."""

    repo_root: Path
    packs_root: Path
    output_root: Path
    llm_config: LLMConfig
    pricing: ModelPricing
    episode_config: EpisodeConfig
    agent_image_digest: str
    gateway_image_digest: str
    guard_image_digest: str
    host_http_port: int
    prior_failed_run_reserve_usd: Decimal = Decimal(0)
    prior_failure_receipt_path: Path | None = None
    prior_failure_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        reserve = self.prior_failed_run_reserve_usd
        if not reserve.is_finite() or reserve < 0:
            raise ValueError(
                "prior failed-run reserve must be finite and non-negative"
            )
        receipt_hash = self.prior_failure_receipt_sha256
        receipt_path = self.prior_failure_receipt_path
        if reserve > 0 and (
            receipt_hash is None or receipt_path is None
        ):
            raise ValueError(
                "a positive prior reserve requires its failure receipt "
                "path and hash"
            )
        if (receipt_hash is None) != (receipt_path is None):
            raise ValueError(
                "prior failure receipt path and hash must be supplied together"
            )
        if receipt_hash is not None and _DIGEST.fullmatch(receipt_hash) is None:
            raise ValueError("prior failure receipt hash is invalid")
        if self.pricing.cached_input_usd_per_million is None:
            raise ValueError(
                "exact Fireworks accounting requires cached-input pricing"
            )


@dataclass(frozen=True, slots=True)
class PackPlan:
    """One fully hydrated pack and its conservative offline estimate."""

    pack_id: str
    pack_path: Path
    content_hash: str
    bars_total: int
    estimate: RunCostEstimate
    estimate_document: JsonObject
    authorization_bound: RunCostEstimate | None = None


@dataclass(frozen=True, slots=True)
class SmokePlan:
    """One paid call that must pass before the first full pack starts."""

    pack_id: str
    observation: JsonObject
    estimate: RunCostEstimate
    estimate_document: JsonObject
    authorization_bound: RunCostEstimate | None = None


@dataclass(frozen=True, slots=True)
class UnpricedAttempt:
    """One IC-6 missed decision whose provider usage was not returned."""

    turn: int
    attempt: int
    reason: str
    observation_sha256: str
    input_tokens_upper_bound: int
    output_tokens_upper_bound: int
    cost_upper_bound_usd: Decimal


@dataclass(frozen=True, slots=True)
class UsageReceipt:
    """Known usage plus bounded IC-6 attempts with no provider receipt."""

    attempts: int
    input_tokens: int
    cached_input_tokens: int
    uncached_input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    unpriced_attempts: tuple[UnpricedAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class PackEvidence:
    """Verified/replayed evidence and deterministic derived surfaces."""

    receipt_document: JsonObject
    report_artifacts: ReportArtifacts
    reports_present: bool
    receipt_present: bool


@dataclass(frozen=True, slots=True)
class SmokeEvidence:
    """Validated one-call response and its deterministic receipt."""

    response_bytes: bytes
    receipt_document: JsonObject


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _cost(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    pricing: ModelPricing,
) -> Decimal:
    if not 0 <= cached_input_tokens <= input_tokens:
        raise MatrixError("cached input usage exceeds total input usage")
    cached_rate = pricing.cached_input_usd_per_million
    if cached_rate is None:
        raise MatrixError("cached-input pricing is unavailable")
    uncached_input_tokens = input_tokens - cached_input_tokens
    numerator = (
        Decimal(uncached_input_tokens) * pricing.input_usd_per_million
        + Decimal(cached_input_tokens) * cached_rate
        + Decimal(output_tokens) * pricing.output_usd_per_million
    )
    return (numerator / Decimal(1_000_000)).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_CEILING,
    )


def _estimated_cost(plan: PackPlan) -> Decimal:
    value = plan.estimate.estimated_cost_usd
    if value is None:
        raise MatrixError(f"{plan.pack_id}: pre-run estimate has no USD price")
    return value


def _authorization_cost(plan: PackPlan) -> Decimal:
    estimate = (
        plan.estimate
        if plan.authorization_bound is None
        else plan.authorization_bound
    )
    value = estimate.estimated_cost_usd
    if value is None:
        raise MatrixError(
            f"{plan.pack_id}: authorization bound has no USD price"
        )
    return value


def _smoke_authorization_cost(smoke_plan: SmokePlan) -> Decimal:
    estimate = (
        smoke_plan.estimate
        if smoke_plan.authorization_bound is None
        else smoke_plan.authorization_bound
    )
    value = estimate.estimated_cost_usd
    if value is None:
        raise MatrixError("smoke authorization bound has no USD price")
    return value


def _estimated_smoke_cost(smoke_plan: SmokePlan) -> Decimal:
    value = smoke_plan.estimate.estimated_cost_usd
    if value is None:
        raise MatrixError("smoke pre-run estimate has no USD price")
    return value


def _within_two_x(estimate: Decimal, actual: Decimal) -> bool:
    if estimate < 0 or actual < 0:
        return False
    if estimate == 0 or actual == 0:
        return estimate == actual
    return estimate <= actual * 2 and actual <= estimate * 2


def _within_two_x_interval(
    estimate: Decimal,
    known_actual: Decimal,
    actual_upper_bound: Decimal,
) -> bool:
    """Prove the 2x condition for every actual in a closed cost interval."""

    if (
        estimate < 0
        or known_actual < 0
        or actual_upper_bound < known_actual
    ):
        return False
    if known_actual == actual_upper_bound:
        return _within_two_x(estimate, known_actual)
    if estimate == 0 or known_actual == 0:
        return False
    return (
        estimate <= known_actual * 2
        and actual_upper_bound <= estimate * 2
    )


def _limits_document() -> JsonObject:
    return {
        "pack_cost_usd_exclusive": PACK_COST_LIMIT_TEXT,
        "matrix_cost_usd_exclusive": MATRIX_COST_LIMIT_TEXT,
    }


def _pricing_document(pricing: ModelPricing) -> JsonObject:
    cached_rate = pricing.cached_input_usd_per_million
    if cached_rate is None:
        raise MatrixError("cached-input pricing is unavailable")
    return {
        "input_usd_per_million": _decimal_text(
            pricing.input_usd_per_million
        ),
        "cached_input_usd_per_million": _decimal_text(
            cached_rate
        ),
        "output_usd_per_million": _decimal_text(
            pricing.output_usd_per_million
        ),
        "estimate_input_assumption": "all_uncached",
    }


def _prior_accounting_document(settings: MatrixSettings) -> JsonObject:
    return {
        "prior_failed_run_reserve_usd": _decimal_text(
            settings.prior_failed_run_reserve_usd
        ),
        "prior_failure_receipt_sha256": (
            settings.prior_failure_receipt_sha256
        ),
    }


def _images_document(settings: MatrixSettings) -> JsonObject:
    return {
        "agent": settings.agent_image_digest,
        "gateway": settings.gateway_image_digest,
        "guard": settings.guard_image_digest,
    }


def _validate_digest(value: str, field: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise MatrixError(f"{field} must be an exact sha256:<64 lowercase hex> digest")
    return value


def _validate_model(value: str) -> str:
    if _MODEL.fullmatch(value) is None or _SECRETISH_MODEL.search(value):
        raise MatrixError(
            "model must be a public provider model slug, never a credential"
        )
    return value


def _estimate_document(
    *,
    settings: MatrixSettings,
    pack_id: str,
    content_hash: str,
    bars_total: int,
    estimate: RunCostEstimate,
    authorization_bound: RunCostEstimate,
    agent_manifest: Mapping[str, object],
) -> JsonObject:
    prompt_hash = agent_manifest.get("prompt_sha256")
    if not isinstance(prompt_hash, str):
        raise MatrixError("LLM agent manifest has no prompt commitment")
    return {
        "schema": "llm_reference_estimate/v1",
        "pack": {
            "pack_id": pack_id,
            "content_hash": content_hash,
            "bars_total": bars_total,
        },
        "provider": {
            "domain": FIREWORKS_DOMAIN,
            "model": settings.llm_config.model,
        },
        "llm": {
            "temperature": settings.llm_config.temperature,
            "max_output_tokens": settings.llm_config.max_tokens,
            "provider_timeout_seconds": settings.llm_config.timeout_seconds,
            "prompt_sha256": prompt_hash,
            "reasoning_effort": PROVIDER_REASONING_EFFORT,
            "response_format": "json_schema",
        },
        "episode_config": {
            **settings.episode_config.to_run_config(),
            "parse_failure_retries": (
                settings.episode_config.parse_failure_retries
            ),
        },
        "pricing": _pricing_document(settings.pricing),
        "images": _images_document(settings),
        "accounting": _prior_accounting_document(settings),
        "estimate": estimate.to_mapping(),
        "authorization_bound": authorization_bound.to_mapping(),
        "limits": _limits_document(),
    }


def _smoke_estimate_document(
    *,
    settings: MatrixSettings,
    pack_id: str,
    observation: Mapping[str, object],
    estimate: RunCostEstimate,
    authorization_bound: RunCostEstimate,
    agent_manifest: Mapping[str, object],
) -> JsonObject:
    prompt_hash = agent_manifest.get("prompt_sha256")
    if not isinstance(prompt_hash, str):
        raise MatrixError("LLM agent manifest has no prompt commitment")
    observation_bytes = canonical_bytes(dict(observation))
    return {
        "schema": "llm_reference_smoke_estimate/v1",
        "pack_id": pack_id,
        "observation_sha256": sha256_prefixed(observation_bytes),
        "provider": {
            "domain": FIREWORKS_DOMAIN,
            "model": settings.llm_config.model,
        },
        "llm": {
            "temperature": settings.llm_config.temperature,
            "max_output_tokens": settings.llm_config.max_tokens,
            "provider_timeout_seconds": settings.llm_config.timeout_seconds,
            "prompt_sha256": prompt_hash,
            "reasoning_effort": PROVIDER_REASONING_EFFORT,
            "response_format": "json_schema",
        },
        "pricing": _pricing_document(settings.pricing),
        "images": _images_document(settings),
        "accounting": _prior_accounting_document(settings),
        "estimate": estimate.to_mapping(),
        "authorization_bound": authorization_bound.to_mapping(),
        "limits": _limits_document(),
    }


def _build_plans(
    settings: MatrixSettings,
    agent_manifest: Mapping[str, object],
) -> tuple[tuple[PackPlan, ...], SmokePlan]:
    """Hydrate all 13 packs and estimate them without provider access."""

    prompt = load_prompt()
    plans: list[PackPlan] = []
    smoke_plan: SmokePlan | None = None
    for index, pack_id in enumerate(EXPECTED_PACK_IDS):
        pack_path = settings.packs_root / pack_id
        pack = load_pack(pack_path)
        if pack.pack_id != pack_id:
            raise MatrixError(
                f"{pack_id}: hydrated manifest identifies {pack.pack_id!r}"
            )
        suffix = f"{index:016x}"
        dry_result = run_episode(
            pack_dir=pack_path,
            agent=MomentumAgent(),
            config=settings.episode_config,
            run_id=f"run_{suffix}",
            episode_id=f"ep_{suffix}",
        )
        if not dry_result.observations:
            raise MatrixError(f"{pack_id}: no observation is available to estimate")
        sample_observation = max(
            dry_result.observations,
            key=lambda observation: len(
                (
                    observation_json(observation)
                    + provider_request_controls_json(observation)
                ).encode("utf-8")
            ),
        )
        sample_json = observation_json(sample_observation)
        estimate = estimate_run_cost(
            system_prompt=prompt,
            sample_observation_json=sample_json,
            turns=pack.bars_total,
            max_output_tokens=settings.llm_config.max_tokens,
            attempts_per_turn=1,
            pricing=settings.pricing,
            request_overhead_text=provider_request_controls_json(
                sample_observation
            ),
        )
        authorization_bound = estimate_run_cost(
            system_prompt=prompt,
            sample_observation_json=sample_json,
            turns=pack.bars_total,
            max_output_tokens=settings.llm_config.max_tokens,
            attempts_per_turn=(
                1 + settings.episode_config.parse_failure_retries
            ),
            pricing=settings.pricing,
            request_overhead_text=provider_request_controls_json(
                sample_observation
            ),
            retry_overhead_text="x" * MAX_RETRY_CORRECTION_BYTES,
            input_bytes_per_token=AUTHORIZATION_INPUT_BYTES_PER_TOKEN,
            framing_tokens_per_attempt=AUTHORIZATION_FRAMING_TOKENS,
        )
        plans.append(
            PackPlan(
                pack_id=pack_id,
                pack_path=pack_path,
                content_hash=pack.content_hash,
                bars_total=pack.bars_total,
                estimate=estimate,
                estimate_document=_estimate_document(
                    settings=settings,
                    pack_id=pack_id,
                    content_hash=pack.content_hash,
                    bars_total=pack.bars_total,
                    estimate=estimate,
                    authorization_bound=authorization_bound,
                    agent_manifest=agent_manifest,
                ),
                authorization_bound=authorization_bound,
            )
        )
        if smoke_plan is None:
            smoke_observation = dict(dry_result.observations[0])
            smoke_estimate = estimate_run_cost(
                system_prompt=prompt,
                sample_observation_json=observation_json(smoke_observation),
                turns=1,
                max_output_tokens=settings.llm_config.max_tokens,
                attempts_per_turn=1,
                pricing=settings.pricing,
                request_overhead_text=provider_request_controls_json(
                    smoke_observation
                ),
            )
            smoke_authorization_bound = estimate_run_cost(
                system_prompt=prompt,
                sample_observation_json=observation_json(smoke_observation),
                turns=1,
                max_output_tokens=settings.llm_config.max_tokens,
                attempts_per_turn=1,
                pricing=settings.pricing,
                request_overhead_text=provider_request_controls_json(
                    smoke_observation
                ),
                input_bytes_per_token=(
                    AUTHORIZATION_INPUT_BYTES_PER_TOKEN
                ),
                framing_tokens_per_attempt=(
                    AUTHORIZATION_FRAMING_TOKENS
                ),
            )
            smoke_plan = SmokePlan(
                pack_id=pack_id,
                observation=smoke_observation,
                estimate=smoke_estimate,
                estimate_document=_smoke_estimate_document(
                    settings=settings,
                    pack_id=pack_id,
                    observation=smoke_observation,
                    estimate=smoke_estimate,
                    authorization_bound=smoke_authorization_bound,
                    agent_manifest=agent_manifest,
                ),
                authorization_bound=smoke_authorization_bound,
            )
    actual_ids = tuple(plan.pack_id for plan in plans)
    if actual_ids != EXPECTED_PACK_IDS or len(plans) != 13:
        raise MatrixError("reference matrix must contain the exact 13-pack catalog")
    if smoke_plan is None:
        raise MatrixError("reference matrix has no smoke observation")
    return tuple(plans), smoke_plan


def _enforce_estimate_budget(
    plans: Sequence[PackPlan],
    smoke_plan: SmokePlan,
    *,
    prior_reserve: Decimal,
) -> Decimal:
    """Require strict pre-run pack and aggregate limits."""

    if tuple(plan.pack_id for plan in plans) != EXPECTED_PACK_IDS:
        raise MatrixError("matrix plans do not match the exact 13-pack catalog")
    if (
        not prior_reserve.is_finite()
        or prior_reserve < 0
    ):
        raise MatrixError("prior failed-run reserve must be finite and non-negative")
    smoke_cost = _smoke_authorization_cost(smoke_plan)
    if smoke_cost >= PACK_COST_LIMIT_USD:
        raise MatrixError(
            f"smoke estimate ${_decimal_text(smoke_cost)} is not strictly "
            f"below ${PACK_COST_LIMIT_TEXT}"
        )
    total = Decimal(0)
    for plan in plans:
        cost = _authorization_cost(plan)
        if cost >= PACK_COST_LIMIT_USD:
            raise MatrixError(
                f"{plan.pack_id}: authorization bound "
                f"${_decimal_text(cost)} is not strictly below "
                f"${PACK_COST_LIMIT_TEXT}"
            )
        total += cost
    accounted_total = prior_reserve + smoke_cost + total
    if accounted_total >= MATRIX_COST_LIMIT_USD:
        raise MatrixError(
            f"accounted matrix estimate ${_decimal_text(accounted_total)} "
            "including prior reserve and smoke is not strictly below "
            f"${MATRIX_COST_LIMIT_TEXT}"
        )
    return accounted_total


def _print_plan(
    settings: MatrixSettings,
    plans: Sequence[PackPlan],
    smoke_plan: SmokePlan,
    total: Decimal,
    *,
    stream: TextIO,
) -> None:
    print(
        "PLAN ONLY: no sandbox started, credential source read, external "
        "request sent, or artifact written.",
        file=stream,
    )
    print(f"Fireworks model: {settings.llm_config.model}", file=stream)
    print(
        f"fail-fast smoke: pack={smoke_plan.pack_id} turns=1 "
        "attempts/turn=1 "
        f"estimate=${_decimal_text(_estimated_smoke_cost(smoke_plan))} "
        "authorized_cost<="
        f"${_decimal_text(_smoke_authorization_cost(smoke_plan))}",
        file=stream,
    )
    for plan in plans:
        estimate = plan.estimate
        print(
            f"{plan.pack_id}: turns={estimate.turns} "
            f"expected_attempts/turn={estimate.attempts_per_turn} "
            f"estimate=${_decimal_text(_estimated_cost(plan))} "
            f"authorized_cost<=${_decimal_text(_authorization_cost(plan))}",
            file=stream,
        )
    print(
        "prior failed-run accounting reserve: "
        f"${_decimal_text(settings.prior_failed_run_reserve_usd)}",
        file=stream,
    )
    print(
        f"accounted conservative total: ${_decimal_text(total)} "
        f"(must remain < ${MATRIX_COST_LIMIT_TEXT})",
        file=stream,
    )
    print(
        "To authorize observation transmission and provider spend, repeat "
        "with --confirm-transmission-and-spend.",
        file=stream,
    )


def _ensure_directory(path: Path) -> None:
    """Create or validate one directory without accepting a symlink."""

    if path.is_symlink():
        raise MatrixError(f"refusing symlink directory: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MatrixError(f"cannot create output directory: {path}") from exc
    if not path.is_dir() or path.is_symlink():
        raise MatrixError(f"output path is not a real directory: {path}")


def _preflight_output_layout(settings: MatrixSettings) -> None:
    """Reject parent symlinks and unexpected evidence-root inventory."""

    root = settings.output_root
    if root == Path(root.anchor):
        raise MatrixError("output root may not be a filesystem root")
    _reject_symlink_path(root, "output root")
    if not root.exists():
        return
    if not root.is_dir():
        raise MatrixError("output root exists and is not a directory")

    allowed_top_level = {
        "authorization.json",
        "estimates",
        "smoke",
        "attempts",
        "bundles",
        "reports",
        "receipts",
        "receipt.json",
    }
    if any(path.name not in allowed_top_level for path in root.iterdir()):
        raise MatrixError("output root has unexpected evidence entries")

    allowed_by_directory = {
        "estimates": {
            f"{pack_id}.json" for pack_id in EXPECTED_PACK_IDS
        },
        "smoke": {
            "attempt.json",
            "estimate.json",
            "response.json",
            "receipt.json",
            "failure-response.json",
            "failure-receipt.json",
        },
        "attempts": {
            f"{pack_id}.json" for pack_id in EXPECTED_PACK_IDS
        },
        "bundles": set(EXPECTED_PACK_IDS),
        "reports": set(EXPECTED_PACK_IDS),
        "receipts": {
            f"{pack_id}.json" for pack_id in EXPECTED_PACK_IDS
        },
    }
    for name, allowed_entries in allowed_by_directory.items():
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_dir():
            raise MatrixError(
                f"{name} evidence path must be a real directory"
            )
        if any(
            child.name not in allowed_entries
            for child in path.iterdir()
        ):
            raise MatrixError(
                f"{name} evidence directory has unexpected entries"
            )


def _reject_symlink_path(path: Path, label: str) -> None:
    """Reject a symlink at the path or in any of its existing ancestors."""

    current = path
    while True:
        if current.is_symlink():
            raise MatrixError(f"{label} may not traverse a symlink: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise MatrixError(f"cannot open output directory for fsync: {path}") from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path: Path, data: bytes) -> None:
    """Atomically publish one new immutable file; never replace a path."""

    if path.exists() or path.is_symlink():
        raise MatrixError(f"refusing to overwrite immutable artifact: {path}")
    _ensure_directory(path.parent)
    temporary = path.parent / (
        f".{path.name}.tmp-{secrets.token_hex(8)}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o644)
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(descriptor, view[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path, follow_symlinks=False)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise MatrixError(
            f"refusing to overwrite immutable artifact: {path}"
        ) from exc
    except OSError as exc:
        raise MatrixError(f"cannot publish immutable artifact: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _expect_bytes(path: Path, expected: bytes, *, label: str) -> bool:
    """Validate existing bytes or report that the artifact is absent."""

    if path.is_symlink():
        raise MatrixError(f"{label} may not be a symlink: {path}")
    if not path.exists():
        return False
    if not path.is_file():
        raise MatrixError(f"{label} is not a regular file: {path}")
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise MatrixError(f"cannot read {label}: {path}") from exc
    if actual != expected:
        raise MatrixError(f"{label} does not byte-match the canonical receipt")
    return True


def _reject_float(token: str) -> None:
    raise MatrixError(f"fractional JSON number is forbidden: {token}")


def _reject_constant(token: str) -> None:
    raise MatrixError(f"non-finite JSON number is forbidden: {token}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MatrixError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_canonical_object(raw: bytes, context: str) -> JsonObject:
    try:
        value = cast(
            object,
            json.loads(
                raw.decode("utf-8"),
                parse_float=_reject_float,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            ),
        )
    except UnicodeDecodeError as exc:
        raise MatrixError(f"{context} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise MatrixError(f"{context} is malformed") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise MatrixError(f"{context} is not an object")
    document = cast(JsonObject, value)
    if canonical_bytes(document) != raw:
        raise MatrixError(f"{context} is not canonical")
    return document


def _load_canonical_object(path: Path) -> JsonObject:
    if not path.is_file() or path.is_symlink():
        raise MatrixError(f"JSON artifact is missing or invalid: {path}")
    return _decode_canonical_object(
        path.read_bytes(),
        f"JSON artifact {path}",
    )


def _receipt_amount(value: object, context: str) -> Decimal:
    text = _string(value, context)
    if _MONEY_TEXT.fullmatch(text) is None:
        raise MatrixError(f"{context} must be a six-decimal amount")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise MatrixError(f"{context} is invalid") from exc
    if not amount.is_finite() or amount < 0:
        raise MatrixError(f"{context} must be finite and non-negative")
    return amount


def _linked_receipt_path(
    settings: MatrixSettings,
    value: object,
    context: str,
) -> Path:
    text = _string(value, context)
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or pure.as_posix() != text
        or len(pure.parts) < 3
        or pure.parts[:2] != ("docs", "review")
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.suffix != ".json"
    ):
        raise MatrixError(
            f"{context} must be a normalized docs/review JSON path"
        )
    path = settings.repo_root.joinpath(*pure.parts)
    _reject_symlink_path(path, context)
    if not path.is_file():
        raise MatrixError(f"{context} is not a regular file")
    return path


def _read_hash_bound_receipt(
    path: Path,
    expected_hash: str,
    context: str,
) -> tuple[bytes, JsonObject]:
    if _DIGEST.fullmatch(expected_hash) is None:
        raise MatrixError(f"{context} hash is invalid")
    raw = path.read_bytes()
    if sha256_prefixed(raw) != expected_hash:
        raise MatrixError(f"{context} bytes do not match their hash")
    payload = raw[:-1] if raw.endswith(b"\n") else raw
    return payload, _decode_canonical_object(payload, context)


def _validate_v2_basis(
    basis: Mapping[str, object],
    *,
    context: str,
) -> None:
    expected = {
        "bound_method",
        "original_failure_receipt_sha256",
    }
    if set(basis) != expected:
        raise MatrixError(f"{context} fields are not exact")
    if not _string(basis.get("bound_method"), f"{context}.bound_method"):
        raise MatrixError(f"{context}.bound_method may not be empty")
    original_hash = _string(
        basis.get("original_failure_receipt_sha256"),
        f"{context}.original_failure_receipt_sha256",
    )
    if _DIGEST.fullmatch(original_hash) is None:
        raise MatrixError(
            "prior failure receipt original incident hash is invalid"
        )


def _validate_v3_basis(
    settings: MatrixSettings,
    basis: Mapping[str, object],
    *,
    receipt_reserve: Decimal,
) -> None:
    context = "prior failure receipt.accounting_basis"
    expected = {
        "bound_method",
        "prior_reserve_receipt_path",
        "prior_reserve_receipt_sha256",
        "prior_reserve_usd",
        "smoke_actual_cost_usd",
        "smoke_failure_receipt_path",
        "smoke_failure_receipt_sha256",
        "smoke_failure_storage_sha256",
    }
    if set(basis) != expected:
        raise MatrixError(f"{context} fields are not exact")
    if (
        _string(basis.get("bound_method"), f"{context}.bound_method")
        != "prior_cumulative_plus_exact_smoke_actual"
    ):
        raise MatrixError(f"{context}.bound_method is not supported")

    prior_reserve = _receipt_amount(
        basis.get("prior_reserve_usd"),
        f"{context}.prior_reserve_usd",
    )
    smoke_actual = _receipt_amount(
        basis.get("smoke_actual_cost_usd"),
        f"{context}.smoke_actual_cost_usd",
    )
    if prior_reserve + smoke_actual != receipt_reserve:
        raise MatrixError(
            "prior failure receipt v3 component amounts do not sum "
            "to its reserve"
        )

    prior_hash = _string(
        basis.get("prior_reserve_receipt_sha256"),
        f"{context}.prior_reserve_receipt_sha256",
    )
    prior_path = _linked_receipt_path(
        settings,
        basis.get("prior_reserve_receipt_path"),
        f"{context}.prior_reserve_receipt_path",
    )
    _, prior_document = _read_hash_bound_receipt(
        prior_path,
        prior_hash,
        "linked prior reserve receipt",
    )
    if (
        prior_document.get("schema")
        != "llm_reference_failure_reserve/v2"
        or prior_document.get("status") != "FAILED_CLOSED"
        or prior_document.get("provider") != FIREWORKS_DOMAIN
        or prior_document.get("model") != settings.llm_config.model
    ):
        raise MatrixError("linked prior reserve receipt identity is invalid")
    if set(prior_document) != {
        "accounting_basis",
        "accounting_reserve_usd",
        "model",
        "provider",
        "schema",
        "status",
    }:
        raise MatrixError("linked prior reserve receipt fields are not exact")
    if _receipt_amount(
        prior_document.get("accounting_reserve_usd"),
        "linked prior reserve receipt.accounting_reserve_usd",
    ) != prior_reserve:
        raise MatrixError("linked prior reserve receipt amount mismatches v3")
    _validate_v2_basis(
        _mapping(
            prior_document.get("accounting_basis"),
            "linked prior reserve receipt.accounting_basis",
        ),
        context="linked prior reserve receipt.accounting_basis",
    )

    smoke_storage_hash = _string(
        basis.get("smoke_failure_storage_sha256"),
        f"{context}.smoke_failure_storage_sha256",
    )
    smoke_path = _linked_receipt_path(
        settings,
        basis.get("smoke_failure_receipt_path"),
        f"{context}.smoke_failure_receipt_path",
    )
    smoke_payload, smoke_document = _read_hash_bound_receipt(
        smoke_path,
        smoke_storage_hash,
        "linked smoke failure receipt",
    )
    smoke_source_hash = _string(
        basis.get("smoke_failure_receipt_sha256"),
        f"{context}.smoke_failure_receipt_sha256",
    )
    if (
        _DIGEST.fullmatch(smoke_source_hash) is None
        or sha256_prefixed(smoke_payload) != smoke_source_hash
    ):
        raise MatrixError(
            "linked smoke failure canonical payload mismatches source hash"
        )
    smoke_provider = _mapping(
        smoke_document.get("provider"),
        "linked smoke failure receipt.provider",
    )
    if (
        set(smoke_provider) != {"domain", "model"}
        or
        smoke_document.get("schema")
        != "llm_reference_smoke_failure_receipt/v1"
        or smoke_document.get("status") != "FAILED_CLOSED"
        or smoke_provider.get("domain") != FIREWORKS_DOMAIN
        or smoke_provider.get("model") != settings.llm_config.model
    ):
        raise MatrixError("linked smoke failure receipt identity is invalid")
    if set(smoke_document) != {
        "accounting_reserve_usd",
        "actual",
        "attempt_receipt_sha256",
        "estimate_receipt_sha256",
        "http_status",
        "images",
        "limits",
        "pack_id",
        "provider",
        "reason",
        "response",
        "schema",
        "status",
    }:
        raise MatrixError("linked smoke failure receipt fields are not exact")
    if (
        smoke_document.get("reason") != "http_non_2xx"
        or smoke_document.get("http_status") != 400
        or smoke_document.get("pack_id") != EXPECTED_PACK_IDS[0]
    ):
        raise MatrixError("linked smoke failure receipt outcome is invalid")
    for field in (
        "attempt_receipt_sha256",
        "estimate_receipt_sha256",
    ):
        digest = _string(
            smoke_document.get(field),
            f"linked smoke failure receipt.{field}",
        )
        if _DIGEST.fullmatch(digest) is None:
            raise MatrixError(
                f"linked smoke failure receipt.{field} is invalid"
            )
    smoke_images = _mapping(
        smoke_document.get("images"),
        "linked smoke failure receipt.images",
    )
    if set(smoke_images) != {"agent", "gateway", "guard"} or any(
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        for digest in smoke_images.values()
    ):
        raise MatrixError("linked smoke failure receipt images are invalid")
    smoke_limits = _mapping(
        smoke_document.get("limits"),
        "linked smoke failure receipt.limits",
    )
    if smoke_limits != _limits_document():
        raise MatrixError("linked smoke failure receipt limits are invalid")
    smoke_response = _mapping(
        smoke_document.get("response"),
        "linked smoke failure receipt.response",
    )
    response_bytes = _exact_non_negative_int(
        smoke_response.get("bytes"),
        "linked smoke failure receipt.response.bytes",
    )
    response_hash = _string(
        smoke_response.get("sha256"),
        "linked smoke failure receipt.response.sha256",
    )
    if (
        set(smoke_response) != {"bytes", "sha256"}
        or response_bytes == 0
        or _DIGEST.fullmatch(response_hash) is None
    ):
        raise MatrixError("linked smoke failure receipt response is invalid")
    smoke_receipt_reserve = _receipt_amount(
        smoke_document.get("accounting_reserve_usd"),
        "linked smoke failure receipt.accounting_reserve_usd",
    )
    smoke_actual_document = _mapping(
        smoke_document.get("actual"),
        "linked smoke failure receipt.actual",
    )
    if set(smoke_actual_document) != {
        "attempts",
        "cached_input_tokens",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "uncached_input_tokens",
        "usage_source",
    }:
        raise MatrixError("linked smoke failure actual fields are not exact")
    attempts = _exact_non_negative_int(
        smoke_actual_document.get("attempts"),
        "linked smoke failure receipt.actual.attempts",
    )
    input_tokens = _exact_non_negative_int(
        smoke_actual_document.get("input_tokens"),
        "linked smoke failure receipt.actual.input_tokens",
    )
    cached_tokens = _exact_non_negative_int(
        smoke_actual_document.get("cached_input_tokens"),
        "linked smoke failure receipt.actual.cached_input_tokens",
    )
    uncached_tokens = _exact_non_negative_int(
        smoke_actual_document.get("uncached_input_tokens"),
        "linked smoke failure receipt.actual.uncached_input_tokens",
    )
    output_tokens = _exact_non_negative_int(
        smoke_actual_document.get("output_tokens"),
        "linked smoke failure receipt.actual.output_tokens",
    )
    smoke_document_cost = _receipt_amount(
        smoke_actual_document.get("cost_usd"),
        "linked smoke failure receipt.actual.cost_usd",
    )
    if (
        attempts != 1
        or smoke_actual_document.get("usage_source")
        != "provider_reported_with_cache_detail"
        or max(input_tokens, cached_tokens, uncached_tokens, output_tokens)
        > MAX_IJSON_INTEGER
        or cached_tokens > input_tokens
        or uncached_tokens != input_tokens - cached_tokens
    ):
        raise MatrixError("linked smoke failure usage is invalid")
    recomputed_cost = _cost(
        input_tokens,
        cached_tokens,
        output_tokens,
        settings.pricing,
    )
    if (
        smoke_receipt_reserve != smoke_actual
        or smoke_document_cost != smoke_actual
        or recomputed_cost != smoke_actual
    ):
        raise MatrixError("linked smoke failure receipt amount mismatches v3")


def _validate_prior_failure_receipt(settings: MatrixSettings) -> None:
    """Bind a carried reserve to exact, canonical prior incident bytes."""

    reserve = settings.prior_failed_run_reserve_usd
    receipt_path = settings.prior_failure_receipt_path
    receipt_hash = settings.prior_failure_receipt_sha256
    if reserve == 0 and receipt_path is None and receipt_hash is None:
        return
    if receipt_path is None or receipt_hash is None:
        raise MatrixError(
            "prior failed-run reserve has no receipt path and hash"
        )
    _reject_symlink_path(receipt_path, "prior failure receipt path")
    if not receipt_path.is_file():
        raise MatrixError("prior failure receipt is not a regular file")
    raw = receipt_path.read_bytes()
    if sha256_prefixed(raw) != receipt_hash:
        raise MatrixError(
            "prior failure receipt bytes do not match the configured hash"
        )
    # The incident ledger is a single line terminated by one LF. Its exact
    # raw bytes remain hash-bound; only the line terminator is excluded from
    # the JCS byte comparison.
    canonical_payload = raw[:-1] if raw.endswith(b"\n") else raw
    document = _decode_canonical_object(
        canonical_payload,
        "prior failure receipt",
    )
    schema = document.get("schema")
    if schema not in {
        "llm_reference_failure_reserve/v2",
        "llm_reference_failure_reserve/v3",
    }:
        raise MatrixError(
            "prior failure receipt schema is not a supported reserve receipt"
        )
    if document.get("status") != "FAILED_CLOSED":
        raise MatrixError(
            "prior failure receipt status is not FAILED_CLOSED"
        )
    receipt_reserve = _receipt_amount(
        document.get("accounting_reserve_usd"),
        "prior failure receipt.accounting_reserve_usd",
    )
    if (
        receipt_reserve != reserve
    ):
        raise MatrixError(
            "prior failure receipt reserve does not match configured reserve"
        )
    provider = _string(
        document.get("provider"),
        "prior failure receipt.provider",
    )
    if provider != FIREWORKS_DOMAIN:
        raise MatrixError(
            "prior failure receipt provider does not match Fireworks"
        )
    model = _string(
        document.get("model"),
        "prior failure receipt.model",
    )
    if model != settings.llm_config.model:
        raise MatrixError(
            "prior failure receipt model does not match configured model"
        )
    accounting_basis = _mapping(
        document.get("accounting_basis"),
        "prior failure receipt.accounting_basis",
    )
    if set(document) != {
        "accounting_basis",
        "accounting_reserve_usd",
        "model",
        "provider",
        "schema",
        "status",
    }:
        raise MatrixError("prior failure receipt fields are not exact")
    if schema == "llm_reference_failure_reserve/v2":
        _validate_v2_basis(
            accounting_basis,
            context="prior failure receipt.accounting_basis",
        )
    else:
        _validate_v3_basis(
            settings,
            accounting_basis,
            receipt_reserve=receipt_reserve,
        )


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise MatrixError(f"{context} must be an object")
    return cast(Mapping[str, object], value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise MatrixError(f"{context} must be text")
    return value


def _exact_non_negative_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MatrixError(f"{context} must be a non-negative integer")
    return value


def _cached_input_from_response(
    response_bytes: bytes,
    *,
    input_tokens: int,
    output_tokens: int,
    context: str,
) -> int:
    document = _decode_canonical_object(response_bytes, context)
    usage = _mapping(document.get("usage"), f"{context}.usage")
    raw_input = _exact_non_negative_int(
        usage.get("input_tokens"),
        f"{context}.usage.input_tokens",
    )
    raw_output = _exact_non_negative_int(
        usage.get("output_tokens"),
        f"{context}.usage.output_tokens",
    )
    if raw_input != input_tokens or raw_output != output_tokens:
        raise MatrixError(
            f"{context} usage disagrees with chained AgentResponded telemetry"
        )
    if document.get("schema") == "action/v1":
        ext = _mapping(document.get("ext"), f"{context}.ext")
        cached_value = ext.get(CACHED_INPUT_EXTENSION_KEY)
    elif document.get("error") == "invalid_contract":
        cached_value = usage.get("cached_input_tokens")
    else:
        raise MatrixError(
            f"{context} has no trusted cached-input telemetry carrier"
        )
    cached = _exact_non_negative_int(
        cached_value,
        f"{context}.cached_input_tokens",
    )
    if cached > input_tokens:
        raise MatrixError(
            f"{context} cached input exceeds total input"
        )
    return cached


def _attempt_response_bytes(
    bundle_dir: Path,
    attempt: Mapping[str, object],
    context: str,
) -> bytes:
    raw_ref = _string(attempt.get("raw_ref"), f"{context}.raw_ref")
    relative = PurePosixPath(raw_ref)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "raw"
        or _RAW_RESPONSE_NAME.fullmatch(relative.parts[1]) is None
    ):
        raise MatrixError(f"{context}.raw_ref is invalid")
    path = bundle_dir / relative.parts[0] / relative.parts[1]
    if path.is_symlink() or not path.is_file():
        raise MatrixError(f"{context} raw response is missing or invalid")
    raw = path.read_bytes()
    raw_bytes = _exact_non_negative_int(
        attempt.get("raw_bytes"),
        f"{context}.raw_bytes",
    )
    raw_sha256 = _string(
        attempt.get("raw_sha256"),
        f"{context}.raw_sha256",
    )
    if len(raw) != raw_bytes or sha256_prefixed(raw) != raw_sha256:
        raise MatrixError(
            f"{context} raw response disagrees with its commitment"
        )
    return raw


def _unpriced_attempt(
    *,
    bundle_dir: Path,
    decision_path: Path,
    decision: Mapping[str, object],
    attempt: int,
    reason: str,
    settings: MatrixSettings,
    system_prompt: str,
) -> UnpricedAttempt:
    """Bound one IC-6 missed call from its exact sealed observation."""

    turn = _exact_non_negative_int(
        decision.get("turn"),
        f"{decision_path.name}.turn",
    )
    if decision_path.name != f"{turn:04d}.json":
        raise MatrixError(
            f"{decision_path.name}: decision filename disagrees with turn"
        )
    saw = _mapping(
        decision.get("saw"),
        f"{decision_path.name}.saw",
    )
    observation_ref = _string(
        saw.get("observation_ref"),
        f"{decision_path.name}.saw.observation_ref",
    )
    expected_ref = f"observations/{decision_path.name}"
    if observation_ref != expected_ref:
        raise MatrixError(
            f"{decision_path.name}: observation reference is invalid"
        )
    observation_sha256 = _string(
        saw.get("observation_sha256"),
        f"{decision_path.name}.saw.observation_sha256",
    )
    if _DIGEST.fullmatch(observation_sha256) is None:
        raise MatrixError(
            f"{decision_path.name}: observation hash is invalid"
        )
    observation_path = bundle_dir / "observations" / decision_path.name
    if observation_path.is_symlink() or not observation_path.is_file():
        raise MatrixError(
            f"{decision_path.name}: observation is missing or invalid"
        )
    observation_bytes = observation_path.read_bytes()
    if sha256_prefixed(observation_bytes) != observation_sha256:
        raise MatrixError(
            f"{decision_path.name}: observation bytes disagree with their hash"
        )
    observation = _decode_canonical_object(
        observation_bytes,
        f"{decision_path.name}.observation",
    )
    input_tokens_upper_bound = (
        estimate_text_tokens(
            system_prompt,
            bytes_per_token=AUTHORIZATION_INPUT_BYTES_PER_TOKEN,
        )
        + estimate_text_tokens(
            observation_json(observation),
            bytes_per_token=AUTHORIZATION_INPUT_BYTES_PER_TOKEN,
        )
        + estimate_text_tokens(
            provider_request_controls_json(observation),
            bytes_per_token=AUTHORIZATION_INPUT_BYTES_PER_TOKEN,
        )
        + AUTHORIZATION_FRAMING_TOKENS
    )
    if attempt == 2:
        input_tokens_upper_bound += estimate_text_tokens(
            "x" * MAX_RETRY_CORRECTION_BYTES,
            bytes_per_token=AUTHORIZATION_INPUT_BYTES_PER_TOKEN,
        )
    output_tokens_upper_bound = settings.llm_config.max_tokens
    return UnpricedAttempt(
        turn=turn,
        attempt=attempt,
        reason=reason,
        observation_sha256=observation_sha256,
        input_tokens_upper_bound=input_tokens_upper_bound,
        output_tokens_upper_bound=output_tokens_upper_bound,
        cost_upper_bound_usd=_cost(
            input_tokens_upper_bound,
            0,
            output_tokens_upper_bound,
            settings.pricing,
        ),
    )


def _collect_usage(
    bundle_dir: Path,
    settings: MatrixSettings,
) -> UsageReceipt:
    """Collect exact usage and bound contract-valid missed provider calls."""

    decisions_dir = bundle_dir / "decisions"
    if not decisions_dir.is_dir() or decisions_dir.is_symlink():
        raise MatrixError(f"bundle has no valid decisions directory: {bundle_dir}")
    decision_paths = tuple(
        sorted(
            path
            for path in decisions_dir.iterdir()
            if _DECISION_NAME.fullmatch(path.name)
        )
    )
    if not decision_paths:
        raise MatrixError("COMPLETE LLM bundle has no decision records")
    attempts_total = 0
    input_total = 0
    cached_input_total = 0
    output_total = 0
    unpriced: list[UnpricedAttempt] = []
    system_prompt = load_prompt()
    for path in decision_paths:
        document = _load_canonical_object(path)
        meant = _mapping(document.get("meant"), f"{path.name}.meant")
        said = _mapping(document.get("said"), f"{path.name}.said")
        attempts = said.get("attempts")
        if not isinstance(attempts, list):
            raise MatrixError(
                f"{path.name}: response attempts are not an array"
            )
        status = meant.get("status")
        if status == "parsed":
            if not attempts:
                raise MatrixError(
                    f"{path.name}: parsed action has no provider usage"
                )
        elif status == "rejected":
            rejected = _mapping(
                meant.get("rejected"),
                f"{path.name}.meant.rejected",
            )
            if meant.get("action") is not None:
                raise MatrixError(
                    f"{path.name}: rejected decision carries an action"
                )
            reason = _string(
                rejected.get("reason"),
                f"{path.name}.meant.rejected.reason",
            )
            if reason not in {"agent_error", "timeout"}:
                raise MatrixError(
                    f"{path.name}: reference action was rejected ({reason})"
                )
            declared_attempts = _exact_non_negative_int(
                rejected.get("attempts"),
                f"{path.name}.meant.rejected.attempts",
            )
            if (
                declared_attempts not in {1, 2}
                or len(attempts) != declared_attempts - 1
            ):
                raise MatrixError(
                    f"{path.name}: missed-decision attempt count is invalid"
                )
            validator_error = rejected.get("validator_error")
            if (
                declared_attempts == 1
                and validator_error is not None
            ) or (
                declared_attempts == 2
                and not isinstance(validator_error, str)
            ):
                raise MatrixError(
                    f"{path.name}: missed-decision retry evidence is invalid"
                )
            unpriced.append(
                _unpriced_attempt(
                    bundle_dir=bundle_dir,
                    decision_path=path,
                    decision=document,
                    attempt=declared_attempts,
                    reason=reason,
                    settings=settings,
                    system_prompt=system_prompt,
                )
            )
        else:
            raise MatrixError(
                f"{path.name}: decision status is invalid"
            )
        for index, raw_attempt in enumerate(attempts, start=1):
            attempt = _mapping(
                raw_attempt,
                f"{path.name}.said.attempts[{index}]",
            )
            usage_raw = attempt.get("token_usage")
            if usage_raw is None:
                raise MatrixError(
                    f"{path.name}: provider omitted token usage for attempt {index}"
                )
            usage = _mapping(
                usage_raw,
                f"{path.name}.said.attempts[{index}].token_usage",
            )
            input_tokens = _exact_non_negative_int(
                usage.get("input_tokens"),
                f"{path.name}.input_tokens",
            )
            output_tokens = _exact_non_negative_int(
                usage.get("output_tokens"),
                f"{path.name}.output_tokens",
            )
            response_bytes = _attempt_response_bytes(
                bundle_dir,
                attempt,
                f"{path.name}.said.attempts[{index}]",
            )
            cached_input_tokens = _cached_input_from_response(
                response_bytes,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                context=f"{path.name}.raw[{index}]",
            )
            attempts_total += 1
            input_total += input_tokens
            cached_input_total += cached_input_tokens
            output_total += output_tokens
    return UsageReceipt(
        attempts=attempts_total,
        input_tokens=input_total,
        cached_input_tokens=cached_input_total,
        uncached_input_tokens=input_total - cached_input_total,
        output_tokens=output_total,
        cost_usd=_cost(
            input_total,
            cached_input_total,
            output_total,
            settings.pricing,
        ),
        unpriced_attempts=tuple(unpriced),
    )


def _report_contents(artifacts: ReportArtifacts) -> Mapping[str, bytes]:
    return {
        "report.txt": artifacts.terminal_text.encode("utf-8"),
        "report.html": artifacts.html.encode("utf-8"),
        "share-card.svg": artifacts.share_card_svg.encode("utf-8"),
    }


def _report_document(artifacts: ReportArtifacts) -> JsonObject:
    return {
        name: {
            "bytes": len(content),
            "sha256": sha256_prefixed(content),
        }
        for name, content in sorted(_report_contents(artifacts).items())
    }


def _unpriced_cost_reserve(usage: UsageReceipt) -> Decimal:
    return sum(
        (
            attempt.cost_upper_bound_usd
            for attempt in usage.unpriced_attempts
        ),
        start=Decimal(0),
    )


def _accounting_cost_upper_bound(usage: UsageReceipt) -> Decimal:
    return usage.cost_usd + _unpriced_cost_reserve(usage)


def _unpriced_attempt_document(attempt: UnpricedAttempt) -> JsonObject:
    return {
        "turn": attempt.turn,
        "attempt": attempt.attempt,
        "reason": attempt.reason,
        "observation_sha256": attempt.observation_sha256,
        "input_tokens_upper_bound": attempt.input_tokens_upper_bound,
        "output_tokens_upper_bound": attempt.output_tokens_upper_bound,
        "cost_upper_bound_usd": _decimal_text(
            attempt.cost_upper_bound_usd
        ),
    }


def _actual_receipt(
    *,
    settings: MatrixSettings,
    plan: PackPlan,
    manifest: Mapping[str, object],
    verification: VerificationResult,
    replay: ReplayResult,
    usage: UsageReceipt,
    artifacts: ReportArtifacts,
) -> JsonObject:
    if not verification.is_complete or verification.root is None:
        raise MatrixError(f"{plan.pack_id}: actual receipt requires COMPLETE evidence")
    if verification.root != replay.bundle_root:
        raise MatrixError(f"{plan.pack_id}: verify/replay roots disagree")
    pack_manifest = _mapping(manifest.get("pack"), "manifest.pack")
    estimate_cost = _estimated_cost(plan)
    authorization_cost = _authorization_cost(plan)
    accounting_upper = _accounting_cost_upper_bound(usage)
    if accounting_upper > authorization_cost:
        raise MatrixError(
            f"{plan.pack_id}: accounting upper bound exceeds its "
            "authorization bound"
        )
    common: JsonObject = {
        "schema": "llm_reference_actual_receipt/v1",
        "status": "COMPLETE",
        "pack": {
            "pack_id": plan.pack_id,
            "content_hash": plan.content_hash,
            "manifest_sha256": _string(
                pack_manifest.get("manifest_sha256"),
                "manifest.pack.manifest_sha256",
            ),
        },
        "estimate_receipt_sha256": sha256_prefixed(
            canonical_bytes(plan.estimate_document)
        ),
        "attempt_receipt_sha256": sha256_prefixed(
            canonical_bytes(_pack_attempt_document(settings, plan))
        ),
        "provider": {
            "domain": FIREWORKS_DOMAIN,
            "model": settings.llm_config.model,
        },
        "images": _images_document(settings),
        "bundle": {
            "run_id": _string(manifest.get("run_id"), "manifest.run_id"),
            "episode_id": _string(
                manifest.get("episode_id"),
                "manifest.episode_id",
            ),
            "root": verification.root,
            "agent_manifest_sha256": _string(
                manifest.get("agent_manifest_sha256"),
                "manifest.agent_manifest_sha256",
            ),
        },
        "replay": {
            "bundle_root": replay.bundle_root,
            "files_compared": list(replay.files_compared),
            "decisions_compared": replay.decisions_compared,
        },
        "pricing": _pricing_document(settings.pricing),
        "actual": {
            "usage_source": (
                "provider_reported_all_stored_attempts_with_cache_detail"
            ),
            "attempts": usage.attempts,
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "uncached_input_tokens": usage.uncached_input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": _decimal_text(usage.cost_usd),
            "within_pack_limit": usage.cost_usd < PACK_COST_LIMIT_USD,
        },
        "estimate_comparison": {
            "estimated_cost_usd": _decimal_text(estimate_cost),
            "actual_cost_usd": _decimal_text(usage.cost_usd),
            "within_2x": _within_two_x(estimate_cost, usage.cost_usd),
        },
        "authorization_comparison": {
            "authorization_bound_usd": _decimal_text(
                authorization_cost
            ),
            "actual_cost_usd": _decimal_text(usage.cost_usd),
            "within_authorization": True,
        },
        "reports": _report_document(artifacts),
        "limits": _limits_document(),
    }
    if not usage.unpriced_attempts:
        return common

    reserve = _unpriced_cost_reserve(usage)
    interval_within_two_x = _within_two_x_interval(
        estimate_cost,
        usage.cost_usd,
        accounting_upper,
    )
    common["schema"] = "llm_reference_actual_receipt/v2"
    common["actual"] = {
        "usage_source": (
            "provider_reported_stored_attempts_plus_bounded_ic6_misses"
        ),
        "attempts": usage.attempts,
        "unpriced_attempts": len(usage.unpriced_attempts),
        "total_attempts": usage.attempts + len(usage.unpriced_attempts),
        "missed_decisions": len(usage.unpriced_attempts),
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "uncached_input_tokens": usage.uncached_input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": _decimal_text(usage.cost_usd),
        "unpriced_cost_reserve_usd": _decimal_text(reserve),
        "cost_upper_bound_usd": _decimal_text(accounting_upper),
        "within_pack_limit": accounting_upper < PACK_COST_LIMIT_USD,
    }
    common["unpriced_attempts"] = [
        _unpriced_attempt_document(attempt)
        for attempt in usage.unpriced_attempts
    ]
    common["estimate_comparison"] = {
        "estimated_cost_usd": _decimal_text(estimate_cost),
        "actual_cost_lower_bound_usd": _decimal_text(usage.cost_usd),
        "actual_cost_upper_bound_usd": _decimal_text(accounting_upper),
        "comparison_basis": "entire_closed_actual_cost_interval",
        "within_2x": interval_within_two_x,
    }
    common["authorization_comparison"] = {
        "authorization_bound_usd": _decimal_text(authorization_cost),
        "accounting_cost_upper_bound_usd": _decimal_text(
            accounting_upper
        ),
        "within_authorization": True,
    }
    return common


def _paths(settings: MatrixSettings, plan: PackPlan) -> Mapping[str, Path]:
    return {
        "estimate": settings.output_root / "estimates" / f"{plan.pack_id}.json",
        "attempt": settings.output_root / "attempts" / f"{plan.pack_id}.json",
        "bundle": settings.output_root / "bundles" / plan.pack_id,
        "reports": settings.output_root / "reports" / plan.pack_id,
        "receipt": settings.output_root / "receipts" / f"{plan.pack_id}.json",
    }


def _validate_report_directory(
    report_dir: Path,
    artifacts: ReportArtifacts,
) -> bool:
    expected = _report_contents(artifacts)
    if report_dir.is_symlink():
        raise MatrixError(f"report directory may not be a symlink: {report_dir}")
    if not report_dir.exists():
        return False
    if not report_dir.is_dir():
        raise MatrixError(f"report path is not a directory: {report_dir}")
    actual_names = frozenset(path.name for path in report_dir.iterdir())
    if actual_names != frozenset(expected):
        raise MatrixError(
            f"report directory is partial or has unexpected files: {report_dir}"
        )
    for name, content in expected.items():
        _expect_bytes(
            report_dir / name,
            content,
            label=f"{name} report",
        )
    return True


def _verify_bundle_evidence(
    settings: MatrixSettings,
    plan: PackPlan,
    agent_manifest: Mapping[str, object],
) -> PackEvidence:
    paths = _paths(settings, plan)
    bundle_dir = paths["bundle"]
    verification = verify_bundle(bundle_dir)
    if not verification.is_complete:
        raise MatrixError(
            f"{plan.pack_id}: existing bundle is {verification.verdict}; "
            "resume accepts only verified COMPLETE bundles"
        )
    replay = replay_bundle(bundle_dir, pack_dir=plan.pack_path)
    expected_agent_bytes = canonical_bytes(dict(agent_manifest))
    _expect_bytes(
        bundle_dir / "agent_manifest.json",
        expected_agent_bytes,
        label=f"{plan.pack_id} agent manifest",
    )
    manifest = _load_canonical_object(bundle_dir / "manifest.json")
    pack_manifest = _mapping(manifest.get("pack"), "manifest.pack")
    if pack_manifest.get("pack_id") != plan.pack_id:
        raise MatrixError(f"{plan.pack_id}: bundle pack id does not match")
    if pack_manifest.get("content_hash") != plan.content_hash:
        raise MatrixError(f"{plan.pack_id}: bundle pack hash does not match")
    run_config = _mapping(manifest.get("run_config"), "manifest.run_config")
    if dict(run_config) != settings.episode_config.to_run_config():
        raise MatrixError(f"{plan.pack_id}: bundle run configuration drifted")
    usage = _collect_usage(bundle_dir, settings)
    artifacts = generate_report(bundle_dir)
    receipt = _actual_receipt(
        settings=settings,
        plan=plan,
        manifest=manifest,
        verification=verification,
        replay=replay,
        usage=usage,
        artifacts=artifacts,
    )
    reports_present = _validate_report_directory(paths["reports"], artifacts)
    receipt_present = _expect_bytes(
        paths["receipt"],
        canonical_bytes(receipt),
        label=f"{plan.pack_id} actual receipt",
    )
    return PackEvidence(
        receipt_document=receipt,
        report_artifacts=artifacts,
        reports_present=reports_present,
        receipt_present=receipt_present,
    )


def _authorization_document(
    settings: MatrixSettings,
    plans: Sequence[PackPlan],
    smoke_plan: SmokePlan,
) -> JsonObject:
    return {
        "schema": "llm_reference_authorization/v1",
        "provider": {
            "domain": FIREWORKS_DOMAIN,
            "model": settings.llm_config.model,
        },
        "images": _images_document(settings),
        "accounting": _prior_accounting_document(settings),
        "smoke_estimate_sha256": sha256_prefixed(
            canonical_bytes(smoke_plan.estimate_document)
        ),
        "pack_estimate_sha256": {
            plan.pack_id: sha256_prefixed(
                canonical_bytes(plan.estimate_document)
            )
            for plan in plans
        },
        "limits": _limits_document(),
    }


def _pack_attempt_document(
    settings: MatrixSettings,
    plan: PackPlan,
) -> JsonObject:
    authorization_path = settings.output_root / "authorization.json"
    if authorization_path.is_symlink() or not authorization_path.is_file():
        raise MatrixError(
            f"{plan.pack_id}: paid attempt requires authorization.json"
        )
    authorization_bytes = authorization_path.read_bytes()
    return {
        "schema": "llm_reference_pack_attempt/v1",
        "status": "STARTED",
        "pack": {
            "pack_id": plan.pack_id,
            "content_hash": plan.content_hash,
        },
        "provider": {
            "domain": FIREWORKS_DOMAIN,
            "model": settings.llm_config.model,
        },
        "images": _images_document(settings),
        "accounting": _prior_accounting_document(settings),
        "estimate_receipt_sha256": sha256_prefixed(
            canonical_bytes(plan.estimate_document)
        ),
        "authorization_receipt_sha256": sha256_prefixed(
            authorization_bytes
        ),
        "authorization_bound_usd": _decimal_text(
            _authorization_cost(plan)
        ),
    }


def _smoke_attempt_document(
    settings: MatrixSettings,
    smoke_plan: SmokePlan,
) -> JsonObject:
    authorization_path = settings.output_root / "authorization.json"
    if authorization_path.is_symlink() or not authorization_path.is_file():
        raise MatrixError("paid smoke attempt requires authorization.json")
    authorization_bytes = authorization_path.read_bytes()
    return {
        "schema": "llm_reference_smoke_attempt/v1",
        "status": "STARTED",
        "pack_id": smoke_plan.pack_id,
        "provider": {
            "domain": FIREWORKS_DOMAIN,
            "model": settings.llm_config.model,
        },
        "images": _images_document(settings),
        "accounting": _prior_accounting_document(settings),
        "estimate_receipt_sha256": sha256_prefixed(
            canonical_bytes(smoke_plan.estimate_document)
        ),
        "authorization_receipt_sha256": sha256_prefixed(
            authorization_bytes
        ),
        "authorization_bound_usd": _decimal_text(
            _smoke_authorization_cost(smoke_plan)
        ),
    }


def _paid_evidence_exists(settings: MatrixSettings) -> bool:
    smoke_root = settings.output_root / "smoke"
    attempts_root = settings.output_root / "attempts"
    bundles_root = settings.output_root / "bundles"
    smoke_paid = any(
        (smoke_root / name).exists() or (smoke_root / name).is_symlink()
        for name in (
            "attempt.json",
            "response.json",
            "receipt.json",
            "failure-response.json",
            "failure-receipt.json",
        )
    )
    attempts_paid = attempts_root.exists() or attempts_root.is_symlink()
    bundles_paid = bundles_root.exists() or bundles_root.is_symlink()
    return smoke_paid or attempts_paid or bundles_paid


def _preflight_authorization(
    settings: MatrixSettings,
    plans: Sequence[PackPlan],
    smoke_plan: SmokePlan,
) -> bool:
    path = settings.output_root / "authorization.json"
    present = _expect_bytes(
        path,
        canonical_bytes(
            _authorization_document(settings, plans, smoke_plan)
        ),
        label="matrix authorization receipt",
    )
    if _paid_evidence_exists(settings) and not present:
        raise MatrixError(
            "paid evidence exists without its immutable authorization receipt"
        )
    return present


def _materialize_estimates(
    settings: MatrixSettings,
    plans: Sequence[PackPlan],
    smoke_plan: SmokePlan,
) -> None:
    """Publish every pre-run estimate before the first sandbox starts."""

    authorization_path = settings.output_root / "authorization.json"
    authorization_bytes = canonical_bytes(
        _authorization_document(settings, plans, smoke_plan)
    )
    if not _expect_bytes(
        authorization_path,
        authorization_bytes,
        label="matrix authorization receipt",
    ):
        _write_new(authorization_path, authorization_bytes)
    smoke_estimate_path = settings.output_root / "smoke" / "estimate.json"
    smoke_estimate_bytes = canonical_bytes(smoke_plan.estimate_document)
    if not _expect_bytes(
        smoke_estimate_path,
        smoke_estimate_bytes,
        label="smoke estimate receipt",
    ):
        _write_new(smoke_estimate_path, smoke_estimate_bytes)
    for plan in plans:
        path = _paths(settings, plan)["estimate"]
        expected = canonical_bytes(plan.estimate_document)
        if not _expect_bytes(
            path,
            expected,
            label=f"{plan.pack_id} estimate receipt",
        ):
            _write_new(path, expected)


def _smoke_paths(settings: MatrixSettings) -> Mapping[str, Path]:
    root = settings.output_root / "smoke"
    return {
        "attempt": root / "attempt.json",
        "estimate": root / "estimate.json",
        "response": root / "response.json",
        "receipt": root / "receipt.json",
    }


def _smoke_usage(
    settings: MatrixSettings,
    smoke_plan: SmokePlan,
    response_bytes: bytes,
) -> UsageReceipt:
    try:
        output = response_bytes.decode("utf-8")
        action = decode_action_object(output)
        validated = validate_action_document(
            action,
            market_aliases(smoke_plan.observation),
            require_all_markets=True,
        )
    except (UnicodeDecodeError, AgentContractError):
        raise MatrixError("smoke response is not a valid action/v1") from None
    if canonical_bytes(validated) != response_bytes:
        raise MatrixError("smoke response is not canonical")
    usage = _mapping(validated.get("usage"), "smoke response.usage")
    input_tokens = _exact_non_negative_int(
        usage.get("input_tokens"),
        "smoke response.usage.input_tokens",
    )
    output_tokens = _exact_non_negative_int(
        usage.get("output_tokens"),
        "smoke response.usage.output_tokens",
    )
    cached_input_tokens = _cached_input_from_response(
        response_bytes,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        context="smoke response",
    )
    return UsageReceipt(
        attempts=1,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        uncached_input_tokens=input_tokens - cached_input_tokens,
        output_tokens=output_tokens,
        cost_usd=_cost(
            input_tokens,
            cached_input_tokens,
            output_tokens,
            settings.pricing,
        ),
    )


def _unvalidated_smoke_usage(
    settings: MatrixSettings,
    response_bytes: bytes,
) -> UsageReceipt | None:
    try:
        decoded = cast(
            object,
            json.loads(
                response_bytes.decode("utf-8"),
                parse_float=_reject_float,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, MatrixError):
        return None
    if not isinstance(decoded, dict):
        return None
    root = cast(dict[str, object], decoded)
    usage_value = root.get("usage")
    if not isinstance(usage_value, Mapping):
        return None
    usage = cast(Mapping[str, object], usage_value)
    try:
        input_tokens = _exact_non_negative_int(
            usage.get("input_tokens"),
            "failed smoke usage.input_tokens",
        )
        output_tokens = _exact_non_negative_int(
            usage.get("output_tokens"),
            "failed smoke usage.output_tokens",
        )
        cached_input_tokens = _cached_input_from_response(
            response_bytes,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context="failed smoke response",
        )
    except MatrixError:
        return None
    return UsageReceipt(
        attempts=1,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        uncached_input_tokens=input_tokens - cached_input_tokens,
        output_tokens=output_tokens,
        cost_usd=_cost(
            input_tokens,
            cached_input_tokens,
            output_tokens,
            settings.pricing,
        ),
    )


def _smoke_failure_receipt(
    *,
    settings: MatrixSettings,
    smoke_plan: SmokePlan,
    reason: str,
    http_status: int | None,
    response_bytes: bytes | None,
) -> JsonObject:
    usage = (
        None
        if response_bytes is None
        else _unvalidated_smoke_usage(settings, response_bytes)
    )
    response: JsonObject | None = None
    if response_bytes is not None:
        response = {
            "bytes": len(response_bytes),
            "sha256": sha256_prefixed(response_bytes),
        }
    actual: JsonObject | None = None
    if usage is not None:
        actual = {
            "usage_source": "provider_reported_with_cache_detail",
            "attempts": 1,
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "uncached_input_tokens": usage.uncached_input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": _decimal_text(usage.cost_usd),
        }
    return {
        "schema": "llm_reference_smoke_failure_receipt/v1",
        "status": "FAILED_CLOSED",
        "reason": reason,
        "http_status": http_status,
        "pack_id": smoke_plan.pack_id,
        "estimate_receipt_sha256": sha256_prefixed(
            canonical_bytes(smoke_plan.estimate_document)
        ),
        "attempt_receipt_sha256": sha256_prefixed(
            canonical_bytes(
                _smoke_attempt_document(settings, smoke_plan)
            )
        ),
        "provider": {
            "domain": FIREWORKS_DOMAIN,
            "model": settings.llm_config.model,
        },
        "images": _images_document(settings),
        "response": response,
        "actual": actual,
        "accounting_reserve_usd": (
            _decimal_text(usage.cost_usd)
            if usage is not None
            else _decimal_text(_smoke_authorization_cost(smoke_plan))
        ),
        "limits": _limits_document(),
    }


def _materialize_smoke_failure(
    *,
    settings: MatrixSettings,
    smoke_plan: SmokePlan,
    reason: str,
    http_status: int | None,
    response_bytes: bytes | None,
) -> None:
    smoke_root = settings.output_root / "smoke"
    response_path = smoke_root / "failure-response.json"
    receipt_path = smoke_root / "failure-receipt.json"
    if response_bytes is not None:
        _write_new(response_path, response_bytes)
    _write_new(
        receipt_path,
        canonical_bytes(
            _smoke_failure_receipt(
                settings=settings,
                smoke_plan=smoke_plan,
                reason=reason,
                http_status=http_status,
                response_bytes=response_bytes,
            )
        ),
    )


def _smoke_receipt(
    *,
    settings: MatrixSettings,
    smoke_plan: SmokePlan,
    response_bytes: bytes,
    usage: UsageReceipt,
) -> JsonObject:
    if usage.cost_usd >= PACK_COST_LIMIT_USD:
        raise MatrixError(
            "smoke actual cost is not strictly below the per-pack limit"
        )
    estimate_cost = smoke_plan.estimate.estimated_cost_usd
    if estimate_cost is None:
        raise MatrixError("smoke pre-run estimate has no USD price")
    authorization_cost = _smoke_authorization_cost(smoke_plan)
    if usage.cost_usd > authorization_cost:
        raise MatrixError(
            "smoke actual cost exceeds its authorization bound"
        )
    return {
        "schema": "llm_reference_smoke_receipt/v1",
        "status": "PASS",
        "pack_id": smoke_plan.pack_id,
        "observation_sha256": sha256_prefixed(
            canonical_bytes(smoke_plan.observation)
        ),
        "estimate_receipt_sha256": sha256_prefixed(
            canonical_bytes(smoke_plan.estimate_document)
        ),
        "attempt_receipt_sha256": sha256_prefixed(
            canonical_bytes(
                _smoke_attempt_document(settings, smoke_plan)
            )
        ),
        "provider": {
            "domain": FIREWORKS_DOMAIN,
            "model": settings.llm_config.model,
        },
        "images": _images_document(settings),
        "response": {
            "bytes": len(response_bytes),
            "sha256": sha256_prefixed(response_bytes),
        },
        "pricing": _pricing_document(settings.pricing),
        "actual": {
            "usage_source": "provider_reported_with_cache_detail",
            "attempts": 1,
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "uncached_input_tokens": usage.uncached_input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": _decimal_text(usage.cost_usd),
            "within_pack_limit": True,
        },
        "estimate_comparison": {
            "estimated_cost_usd": _decimal_text(estimate_cost),
            "actual_cost_usd": _decimal_text(usage.cost_usd),
            "within_2x": _within_two_x(estimate_cost, usage.cost_usd),
        },
        "authorization_comparison": {
            "authorization_bound_usd": _decimal_text(
                authorization_cost
            ),
            "actual_cost_usd": _decimal_text(usage.cost_usd),
            "within_authorization": True,
        },
        "limits": _limits_document(),
    }


def _validated_smoke_evidence(
    settings: MatrixSettings,
    smoke_plan: SmokePlan,
    evidence: SmokeEvidence,
) -> SmokeEvidence:
    usage = _smoke_usage(
        settings,
        smoke_plan,
        evidence.response_bytes,
    )
    expected_receipt = _smoke_receipt(
        settings=settings,
        smoke_plan=smoke_plan,
        response_bytes=evidence.response_bytes,
        usage=usage,
    )
    if (
        canonical_bytes(evidence.receipt_document)
        != canonical_bytes(expected_receipt)
    ):
        raise MatrixError("smoke receipt does not match the validated response")
    return SmokeEvidence(
        response_bytes=evidence.response_bytes,
        receipt_document=expected_receipt,
    )


def _preflight_smoke(
    settings: MatrixSettings,
    smoke_plan: SmokePlan,
) -> SmokeEvidence | None:
    smoke_root = settings.output_root / "smoke"
    if smoke_root.is_symlink():
        raise MatrixError("smoke evidence directory may not be a symlink")
    if smoke_root.exists() and not smoke_root.is_dir():
        raise MatrixError("smoke evidence path is not a directory")
    if smoke_root.exists():
        allowed = {
            "attempt.json",
            "estimate.json",
            "response.json",
            "receipt.json",
            "failure-response.json",
            "failure-receipt.json",
        }
        unexpected = sorted(
            path.name for path in smoke_root.iterdir()
            if path.name not in allowed
        )
        if unexpected:
            raise MatrixError(
                "smoke evidence directory has unexpected entries"
            )
    paths = _smoke_paths(settings)
    estimate_present = _expect_bytes(
        paths["estimate"],
        canonical_bytes(smoke_plan.estimate_document),
        label="smoke estimate receipt",
    )
    attempt_path_present = (
        paths["attempt"].exists() or paths["attempt"].is_symlink()
    )
    attempt_present = (
        _expect_bytes(
            paths["attempt"],
            canonical_bytes(
                _smoke_attempt_document(settings, smoke_plan)
            ),
            label="smoke paid-attempt receipt",
        )
        if attempt_path_present
        else False
    )
    failure_paths = (
        smoke_root / "failure-response.json",
        smoke_root / "failure-receipt.json",
    )
    failure_present = any(
        path.exists() or path.is_symlink() for path in failure_paths
    )
    if failure_present:
        if not attempt_present:
            raise MatrixError(
                "failed smoke evidence has no immutable pre-call "
                "attempt receipt"
            )
        raise MatrixError(
            "failed smoke evidence is terminal; choose a fresh output root"
        )
    response_present = paths["response"].exists() or paths["response"].is_symlink()
    receipt_present = paths["receipt"].exists() or paths["receipt"].is_symlink()
    if response_present != receipt_present:
        raise MatrixError("smoke evidence is partial and cannot be resumed")
    if response_present and not attempt_present:
        raise MatrixError(
            "passing smoke evidence has no immutable pre-call attempt receipt"
        )
    if attempt_present and not response_present:
        raise MatrixError(
            "paid smoke attempt has no final receipt; it is terminal and "
            "may not be purchased again"
        )
    if not response_present:
        paid_pack_paths = (
            settings.output_root / "attempts",
            settings.output_root / "bundles",
        )
        if any(
            path.exists() or path.is_symlink()
            for path in paid_pack_paths
        ):
            raise MatrixError(
                "paid pack evidence exists without a passing smoke receipt"
            )
        return None
    if not estimate_present:
        raise MatrixError("smoke evidence has no immutable pre-run estimate")
    if paths["response"].is_symlink() or not paths["response"].is_file():
        raise MatrixError("smoke response path is invalid")
    response_bytes = paths["response"].read_bytes()
    receipt_document = _load_canonical_object(paths["receipt"])
    validated = _validated_smoke_evidence(
        settings,
        smoke_plan,
        SmokeEvidence(
            response_bytes=response_bytes,
            receipt_document=receipt_document,
        ),
    )
    _expect_bytes(
        paths["receipt"],
        canonical_bytes(validated.receipt_document),
        label="smoke actual receipt",
    )
    return validated


def _run_paid_smoke(
    settings: MatrixSettings,
    smoke_plan: SmokePlan,
    agent_manifest: Mapping[str, object],
) -> SmokeEvidence:
    """Make exactly one paid call through the normal sandbox and validate it."""

    del agent_manifest
    request: JsonObject = {
        "schema": "runner_request/v1",
        "attempt": 1,
        "observation": smoke_plan.observation,
        "retry": None,
    }
    reply: AgentReply | None = None
    paid_call_boundary_crossed = False
    try:
        with DockerSandbox(_isolation_plan(settings)) as agent:
            _write_new(
                _smoke_paths(settings)["attempt"],
                canonical_bytes(
                    _smoke_attempt_document(settings, smoke_plan)
                ),
            )
            paid_call_boundary_crossed = True
            reply = agent.decide(request)
    except DecisionTimeout:
        _materialize_smoke_failure(
            settings=settings,
            smoke_plan=smoke_plan,
            reason="timeout",
            http_status=None,
            response_bytes=None,
        )
        raise MatrixError(
            "smoke timed out; no full pack will start"
        ) from None
    except HTTPAgentError as exc:
        _materialize_smoke_failure(
            settings=settings,
            smoke_plan=smoke_plan,
            reason="agent_error",
            http_status=exc.http_status,
            response_bytes=exc.body,
        )
        raise MatrixError(
            "smoke agent transport failed; no full pack will start"
        ) from None
    except Exception:
        if paid_call_boundary_crossed:
            _materialize_smoke_failure(
                settings=settings,
                smoke_plan=smoke_plan,
                reason="paid_call_or_cleanup_error",
                http_status=(
                    None if reply is None else reply.http_status
                ),
                response_bytes=(
                    None if reply is None else reply.body
                ),
            )
            raise MatrixError(
                "smoke failed after crossing the paid-call boundary; "
                "terminal evidence was preserved"
            ) from None
        raise MatrixError(
            "smoke sandbox failed before the paid-call boundary"
        ) from None
    if reply is None:
        raise MatrixError("smoke produced no reply")
    if (
        reply.http_status is None
        or not 200 <= reply.http_status <= 299
    ):
        _materialize_smoke_failure(
            settings=settings,
            smoke_plan=smoke_plan,
            reason="http_non_2xx",
            http_status=reply.http_status,
            response_bytes=reply.body,
        )
        raise MatrixError(
            f"smoke response returned HTTP {reply.http_status}; "
            "no full pack will start"
        )
    try:
        usage = _smoke_usage(settings, smoke_plan, reply.body)
        receipt = _smoke_receipt(
            settings=settings,
            smoke_plan=smoke_plan,
            response_bytes=reply.body,
            usage=usage,
        )
    except MatrixError:
        _materialize_smoke_failure(
            settings=settings,
            smoke_plan=smoke_plan,
            reason="invalid_or_unpriced_response",
            http_status=reply.http_status,
            response_bytes=reply.body,
        )
        raise
    return SmokeEvidence(
        response_bytes=reply.body,
        receipt_document=receipt,
    )


def _materialize_smoke(
    settings: MatrixSettings,
    smoke_plan: SmokePlan,
    evidence: SmokeEvidence,
) -> None:
    paths = _smoke_paths(settings)
    if paths["response"].exists() or paths["receipt"].exists():
        raise MatrixError("refusing to replace existing smoke evidence")
    if not _expect_bytes(
        paths["attempt"],
        canonical_bytes(_smoke_attempt_document(settings, smoke_plan)),
        label="smoke paid-attempt receipt",
    ):
        raise MatrixError(
            "refusing passing smoke without its pre-call attempt receipt"
        )
    _write_new(paths["response"], evidence.response_bytes)
    _write_new(paths["receipt"], canonical_bytes(evidence.receipt_document))


def _materialize_pack_evidence(
    settings: MatrixSettings,
    plan: PackPlan,
    evidence: PackEvidence,
) -> PackEvidence:
    paths = _paths(settings, plan)
    report_dir = paths["reports"]
    if not evidence.reports_present:
        if report_dir.exists() or report_dir.is_symlink():
            raise MatrixError(
                f"{plan.pack_id}: refusing partial report directory"
            )
        _ensure_directory(report_dir)
        for name, content in sorted(
            _report_contents(evidence.report_artifacts).items()
        ):
            _write_new(report_dir / name, content)
    if not evidence.receipt_present:
        _write_new(
            paths["receipt"],
            canonical_bytes(evidence.receipt_document),
        )
    return PackEvidence(
        receipt_document=evidence.receipt_document,
        report_artifacts=evidence.report_artifacts,
        reports_present=True,
        receipt_present=True,
    )


def _orphan_checks(settings: MatrixSettings, plan: PackPlan) -> None:
    paths = _paths(settings, plan)
    for key in ("reports", "receipt"):
        path = paths[key]
        if path.exists() or path.is_symlink():
            raise MatrixError(
                f"{plan.pack_id}: {key} exists without a COMPLETE bundle"
            )


def _preflight_existing(
    settings: MatrixSettings,
    plans: Sequence[PackPlan],
    agent_manifest: Mapping[str, object],
    smoke_evidence: SmokeEvidence | None,
    *,
    resume: bool,
) -> dict[str, PackEvidence]:
    root = settings.output_root
    if root == Path(root.anchor):
        raise MatrixError("output root may not be a filesystem root")
    if root.is_symlink():
        raise MatrixError("output root may not be a symlink")
    if root.exists() and not root.is_dir():
        raise MatrixError("output root exists and is not a directory")
    if root.exists() and not resume:
        raise MatrixError(
            "output root already exists; choose a fresh path or pass --resume"
        )
    evidence: dict[str, PackEvidence] = {}
    if not root.exists():
        return evidence
    for plan in plans:
        paths = _paths(settings, plan)
        estimate_path = paths["estimate"]
        estimate_present = _expect_bytes(
            estimate_path,
            canonical_bytes(plan.estimate_document),
            label=f"{plan.pack_id} estimate receipt",
        )
        if smoke_evidence is not None and not estimate_present:
            raise MatrixError(
                f"{plan.pack_id}: paid smoke exists but the immutable "
                "pre-run estimate is missing"
            )
        attempt_present = _expect_bytes(
            paths["attempt"],
            canonical_bytes(_pack_attempt_document(settings, plan)),
            label=f"{plan.pack_id} paid-attempt receipt",
        )
        bundle = paths["bundle"]
        if bundle.exists() or bundle.is_symlink():
            if not estimate_present:
                raise MatrixError(
                    f"{plan.pack_id}: COMPLETE bundle has no immutable "
                    "pre-run estimate receipt"
                )
            if not attempt_present:
                raise MatrixError(
                    f"{plan.pack_id}: COMPLETE bundle has no immutable "
                    "paid-attempt receipt"
                )
            if not bundle.is_dir() or bundle.is_symlink():
                raise MatrixError(f"{plan.pack_id}: bundle path is invalid")
            item = _verify_bundle_evidence(
                settings,
                plan,
                agent_manifest,
            )
            accounting_cost = _receipt_accounting_cost(
                item.receipt_document
            )
            if accounting_cost >= PACK_COST_LIMIT_USD:
                raise MatrixError(
                    f"{plan.pack_id}: resumed accounting upper bound "
                    f"${_decimal_text(accounting_cost)} is not strictly below "
                    f"${PACK_COST_LIMIT_TEXT}"
                )
            evidence[plan.pack_id] = item
        else:
            if attempt_present:
                raise MatrixError(
                    f"{plan.pack_id}: paid attempt has no COMPLETE bundle; "
                    "it is terminal and may not be purchased again"
                )
            _orphan_checks(settings, plan)
    aggregate_path = settings.output_root / "receipt.json"
    if aggregate_path.exists() or aggregate_path.is_symlink():
        if smoke_evidence is None:
            raise MatrixError("aggregate receipt exists without passing smoke")
        if len(evidence) != len(plans):
            raise MatrixError(
                "aggregate receipt exists before all 13 bundles are COMPLETE"
            )
        if any(
            not item.reports_present or not item.receipt_present
            for item in evidence.values()
        ):
            raise MatrixError(
                "aggregate receipt exists before per-pack artifacts are complete"
            )
        aggregate = _aggregate_receipt(
            settings,
            plans,
            evidence,
            smoke_evidence.receipt_document,
        )
        _expect_bytes(
            aggregate_path,
            canonical_bytes(aggregate),
            label="aggregate receipt",
        )
    return evidence


def _isolation_plan(settings: MatrixSettings) -> IsolationPlan:
    """Build one secret-free Docker plan after explicit confirmation only."""

    sandbox_id = secrets.token_hex(16)
    suffix = sandbox_id[:12]
    return IsolationPlan(
        repo_root=settings.repo_root,
        agent_image=settings.agent_image_digest,
        gateway_image=settings.gateway_image_digest,
        guard_image=settings.guard_image_digest,
        endpoint_domains=(FIREWORKS_DOMAIN,),
        agent_command=("python3", "-m", "agents.server"),
        credential_env_names=("FIREWORKS_API_KEY",),
        agent_env={
            "TRADEVOLVE_AGENT_MODE": "llm",
            "TRADEVOLVE_LLM_MODEL": settings.llm_config.model,
            "TRADEVOLVE_LLM_BASE_URL": settings.llm_config.base_url,
            "TRADEVOLVE_LLM_TEMPERATURE": (
                settings.llm_config.temperature
            ),
            "TRADEVOLVE_LLM_MAX_TOKENS": str(
                settings.llm_config.max_tokens
            ),
            "TRADEVOLVE_LLM_TIMEOUT_SECONDS": str(
                settings.llm_config.timeout_seconds
            ),
        },
        sandbox_id=sandbox_id,
        agent_name=f"tradeevolve-agent-{suffix}",
        gateway_name=f"tradeevolve-gateway-{suffix}",
        guard_name=f"tradeevolve-guard-{suffix}",
        internal_network=f"tradeevolve-agent-net-{suffix}",
        egress_network=f"tradeevolve-egress-net-{suffix}",
        host_http_port=settings.host_http_port,
    )


def _host_manifest() -> JsonObject:
    return {
        "os": platform.system().lower() or "unknown",
        "arch": platform.machine().lower() or "unknown",
        "python": platform.python_version(),
    }


def _run_paid_bundle(
    settings: MatrixSettings,
    plan: PackPlan,
    agent_manifest: Mapping[str, object],
) -> None:
    """Run and seal one new bundle; this is the only paid execution seam."""

    bundle_dir = _paths(settings, plan)["bundle"]
    if bundle_dir.exists() or bundle_dir.is_symlink():
        raise MatrixError(f"{plan.pack_id}: paid runner requires a fresh bundle path")
    _ensure_directory(bundle_dir.parent)
    isolation = _isolation_plan(settings)
    run_id = f"run_{secrets.token_hex(8)}"
    episode_id = f"ep_{secrets.token_hex(8)}"
    with DockerSandbox(isolation) as agent:
        _write_new(
            _paths(settings, plan)["attempt"],
            canonical_bytes(_pack_attempt_document(settings, plan)),
        )
        result = run_episode(
            pack_dir=plan.pack_path,
            agent=agent,
            config=settings.episode_config,
            run_id=run_id,
            episode_id=episode_id,
        )
        manifest = build_bundle_manifest(
            result,
            agent_manifest,
            created_at_ms=time.time_ns() // 1_000_000,
            host=_host_manifest(),
        )
        record_episode_bundle(
            bundle_dir,
            result=result,
            manifest=manifest,
            agent_manifest=agent_manifest,
        )


def _receipt_actual(receipt: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(receipt.get("actual"), "actual receipt.actual")


def _receipt_cost(receipt: Mapping[str, object]) -> Decimal:
    value = _string(
        _receipt_actual(receipt).get("cost_usd"),
        "actual receipt.actual.cost_usd",
    )
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise MatrixError("actual receipt has an invalid cost") from exc
    if not parsed.is_finite() or parsed < 0:
        raise MatrixError("actual receipt has an invalid cost")
    return parsed


def _receipt_unpriced_reserve(receipt: Mapping[str, object]) -> Decimal:
    actual = _receipt_actual(receipt)
    raw = actual.get("unpriced_cost_reserve_usd")
    if raw is None:
        return Decimal(0)
    reserve = _receipt_amount(
        raw,
        "actual receipt.actual.unpriced_cost_reserve_usd",
    )
    upper = _receipt_amount(
        actual.get("cost_upper_bound_usd"),
        "actual receipt.actual.cost_upper_bound_usd",
    )
    if _receipt_cost(receipt) + reserve != upper:
        raise MatrixError(
            "actual receipt bounded-cost components do not sum"
        )
    return reserve


def _receipt_accounting_cost(receipt: Mapping[str, object]) -> Decimal:
    return _receipt_cost(receipt) + _receipt_unpriced_reserve(receipt)


def _remaining_bound(
    settings: MatrixSettings,
    plans: Sequence[PackPlan],
    evidence: Mapping[str, PackEvidence],
    *,
    smoke_actual: Decimal,
) -> Decimal:
    accounted = sum(
        (
            _receipt_accounting_cost(item.receipt_document)
            for item in evidence.values()
        ),
        start=Decimal(0),
    )
    estimated_remaining = sum(
        (
            _authorization_cost(plan)
            for plan in plans
            if plan.pack_id not in evidence
        ),
        start=Decimal(0),
    )
    return (
        settings.prior_failed_run_reserve_usd
        + smoke_actual
        + accounted
        + estimated_remaining
    )


def _smoke_actual_cost(receipt: Mapping[str, object]) -> Decimal:
    actual = _mapping(receipt.get("actual"), "smoke receipt.actual")
    value = _string(actual.get("cost_usd"), "smoke receipt.actual.cost_usd")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise MatrixError("smoke receipt has an invalid actual cost") from exc
    if not parsed.is_finite() or parsed < 0:
        raise MatrixError("smoke receipt has an invalid actual cost")
    return parsed


def _receipt_estimate_within_two_x(
    receipt: Mapping[str, object],
    context: str,
) -> bool:
    comparison = _mapping(
        receipt.get("estimate_comparison"),
        f"{context}.estimate_comparison",
    )
    value = comparison.get("within_2x")
    if not isinstance(value, bool):
        raise MatrixError(f"{context} has no valid estimate comparison")
    return value


def _aggregate_receipt(
    settings: MatrixSettings,
    plans: Sequence[PackPlan],
    evidence: Mapping[str, PackEvidence],
    smoke_receipt: Mapping[str, object],
) -> JsonObject:
    if len(evidence) != len(plans):
        raise MatrixError("aggregate receipt requires all 13 COMPLETE packs")
    rows: list[JsonObject] = []
    legacy_rows: list[JsonObject] = []
    total_estimated = Decimal(0)
    total_authorized = Decimal(0)
    total_known_actual = Decimal(0)
    total_unpriced_reserve = Decimal(0)
    total_reported_attempts = 0
    total_unpriced_attempts = 0
    total_input = 0
    total_cached_input = 0
    total_uncached_input = 0
    total_output = 0
    for plan in plans:
        item = evidence[plan.pack_id]
        receipt = item.receipt_document
        actual = _receipt_actual(receipt)
        estimate_cost = _estimated_cost(plan)
        authorization_cost = _authorization_cost(plan)
        actual_cost = _receipt_cost(receipt)
        unpriced_reserve = _receipt_unpriced_reserve(receipt)
        accounting_upper = actual_cost + unpriced_reserve
        reported_attempts = _exact_non_negative_int(
            actual.get("attempts"),
            f"{plan.pack_id}.actual.attempts",
        )
        unpriced_attempts = _exact_non_negative_int(
            actual.get("unpriced_attempts", 0),
            f"{plan.pack_id}.actual.unpriced_attempts",
        )
        if (unpriced_attempts == 0) != (unpriced_reserve == 0):
            raise MatrixError(
                f"{plan.pack_id}: unpriced attempt count and reserve disagree"
            )
        input_tokens = _exact_non_negative_int(
            actual.get("input_tokens"),
            f"{plan.pack_id}.actual.input_tokens",
        )
        cached_input_tokens = _exact_non_negative_int(
            actual.get("cached_input_tokens"),
            f"{plan.pack_id}.actual.cached_input_tokens",
        )
        uncached_input_tokens = _exact_non_negative_int(
            actual.get("uncached_input_tokens"),
            f"{plan.pack_id}.actual.uncached_input_tokens",
        )
        if (
            cached_input_tokens + uncached_input_tokens
            != input_tokens
        ):
            raise MatrixError(
                f"{plan.pack_id}: cached and uncached usage "
                "do not sum to total input"
            )
        output_tokens = _exact_non_negative_int(
            actual.get("output_tokens"),
            f"{plan.pack_id}.actual.output_tokens",
        )
        bundle = _mapping(receipt.get("bundle"), f"{plan.pack_id}.bundle")
        total_estimated += estimate_cost
        total_authorized += authorization_cost
        total_known_actual += actual_cost
        total_unpriced_reserve += unpriced_reserve
        total_reported_attempts += reported_attempts
        total_unpriced_attempts += unpriced_attempts
        total_input += input_tokens
        total_cached_input += cached_input_tokens
        total_uncached_input += uncached_input_tokens
        total_output += output_tokens
        legacy_rows.append(
            {
                "pack_id": plan.pack_id,
                "content_hash": plan.content_hash,
                "estimate_receipt_sha256": sha256_prefixed(
                    canonical_bytes(plan.estimate_document)
                ),
                "actual_receipt_sha256": sha256_prefixed(
                    canonical_bytes(receipt)
                ),
                "bundle_root": _string(
                    bundle.get("root"),
                    f"{plan.pack_id}.bundle.root",
                ),
                "estimated_cost_usd": _decimal_text(estimate_cost),
                "authorization_bound_usd": _decimal_text(
                    authorization_cost
                ),
                "actual_cost_usd": _decimal_text(actual_cost),
                "attempts": reported_attempts,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "uncached_input_tokens": uncached_input_tokens,
                "output_tokens": output_tokens,
            }
        )
        rows.append(
            {
                "pack_id": plan.pack_id,
                "content_hash": plan.content_hash,
                "estimate_receipt_sha256": sha256_prefixed(
                    canonical_bytes(plan.estimate_document)
                ),
                "actual_receipt_sha256": sha256_prefixed(
                    canonical_bytes(receipt)
                ),
                "bundle_root": _string(
                    bundle.get("root"),
                    f"{plan.pack_id}.bundle.root",
                ),
                "estimated_cost_usd": _decimal_text(estimate_cost),
                "authorization_bound_usd": _decimal_text(
                    authorization_cost
                ),
                "actual_cost_usd": _decimal_text(actual_cost),
                "unpriced_cost_reserve_usd": _decimal_text(
                    unpriced_reserve
                ),
                "accounting_cost_upper_bound_usd": _decimal_text(
                    accounting_upper
                ),
                "reported_attempts": reported_attempts,
                "unpriced_attempts": unpriced_attempts,
                "attempts": reported_attempts + unpriced_attempts,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "uncached_input_tokens": uncached_input_tokens,
                "output_tokens": output_tokens,
            }
        )
    smoke_actual = _smoke_actual_cost(smoke_receipt)
    known_current_run = smoke_actual + total_known_actual
    accounted_upper = (
        settings.prior_failed_run_reserve_usd
        + known_current_run
        + total_unpriced_reserve
    )
    if accounted_upper >= MATRIX_COST_LIMIT_USD:
        raise MatrixError(
            "accounted upper bound is not strictly below "
            "the aggregate limit"
        )
    if total_unpriced_attempts == 0:
        return {
            "schema": "llm_reference_matrix_receipt/v1",
            "status": "COMPLETE",
            "matrix": {
                "pack_count": len(plans),
                "pack_ids": [plan.pack_id for plan in plans],
                "provider_domain": FIREWORKS_DOMAIN,
                "model": settings.llm_config.model,
                "images": _images_document(settings),
                "run_config": settings.episode_config.to_run_config(),
            },
            "pricing": _pricing_document(settings.pricing),
            "accounting": {
                **_prior_accounting_document(settings),
                "smoke_actual_cost_usd": _decimal_text(smoke_actual),
                "actual_plus_prior_reserve_usd": _decimal_text(
                    accounted_upper
                ),
                "smoke_receipt_sha256": sha256_prefixed(
                    canonical_bytes(dict(smoke_receipt))
                ),
            },
            "packs": legacy_rows,
            "totals": {
                "estimated_cost_usd": _decimal_text(total_estimated),
                "authorization_bound_usd": _decimal_text(total_authorized),
                "actual_cost_usd": _decimal_text(total_known_actual),
                "attempts": total_reported_attempts,
                "input_tokens": total_input,
                "cached_input_tokens": total_cached_input,
                "uncached_input_tokens": total_uncached_input,
                "output_tokens": total_output,
            },
            "limits": _limits_document(),
        }
    return {
        "schema": "llm_reference_matrix_receipt/v2",
        "status": "COMPLETE",
        "matrix": {
            "pack_count": len(plans),
            "pack_ids": [plan.pack_id for plan in plans],
            "provider_domain": FIREWORKS_DOMAIN,
            "model": settings.llm_config.model,
            "images": _images_document(settings),
            "run_config": settings.episode_config.to_run_config(),
        },
        "pricing": _pricing_document(settings.pricing),
        "accounting": {
            **_prior_accounting_document(settings),
            "smoke_actual_cost_usd": _decimal_text(smoke_actual),
            "known_current_run_cost_usd": _decimal_text(
                known_current_run
            ),
            "current_run_unpriced_cost_reserve_usd": _decimal_text(
                total_unpriced_reserve
            ),
            "accounted_upper_bound_usd": _decimal_text(
                accounted_upper
            ),
            "smoke_receipt_sha256": sha256_prefixed(
                canonical_bytes(dict(smoke_receipt))
            ),
        },
        "packs": rows,
        "totals": {
            "estimated_cost_usd": _decimal_text(total_estimated),
            "authorization_bound_usd": _decimal_text(total_authorized),
            "actual_cost_usd": _decimal_text(total_known_actual),
            "unpriced_cost_reserve_usd": _decimal_text(
                total_unpriced_reserve
            ),
            "accounting_cost_upper_bound_usd": _decimal_text(
                total_known_actual + total_unpriced_reserve
            ),
            "reported_attempts": total_reported_attempts,
            "unpriced_attempts": total_unpriced_attempts,
            "attempts": (
                total_reported_attempts + total_unpriced_attempts
            ),
            "input_tokens": total_input,
            "cached_input_tokens": total_cached_input,
            "uncached_input_tokens": total_uncached_input,
            "output_tokens": total_output,
        },
        "limits": _limits_document(),
    }


def execute_matrix(
    settings: MatrixSettings,
    plans: Sequence[PackPlan],
    smoke_plan: SmokePlan,
    *,
    confirmed: bool,
    resume: bool,
    stdout: TextIO,
    paid_runner: PaidRunner | None = None,
    paid_smoke_runner: PaidSmokeRunner | None = None,
) -> JsonObject | None:
    """Apply spend gates, then optionally execute the exact matrix.

    The confirmation branch occurs before output inspection, directory
    creation, Docker plan construction, credential loading, or paid execution.
    This makes the default behavior both network-free and write-free.
    """

    _validate_prior_failure_receipt(settings)
    total_estimate = _enforce_estimate_budget(
        plans,
        smoke_plan,
        prior_reserve=settings.prior_failed_run_reserve_usd,
    )
    if not confirmed:
        _print_plan(
            settings,
            plans,
            smoke_plan,
            total_estimate,
            stream=stdout,
        )
        return None

    agent_manifest = build_llm_manifest(
        settings.llm_config,
        image_digest=settings.agent_image_digest,
    )
    _preflight_output_layout(settings)
    _preflight_authorization(settings, plans, smoke_plan)
    smoke_evidence = _preflight_smoke(settings, smoke_plan)
    evidence = _preflight_existing(
        settings,
        plans,
        agent_manifest,
        smoke_evidence,
        resume=resume,
    )
    _ensure_directory(settings.output_root)
    _materialize_estimates(settings, plans, smoke_plan)
    if smoke_evidence is None:
        smoke_runner = (
            _run_paid_smoke
            if paid_smoke_runner is None
            else paid_smoke_runner
        )
        print(
            "smoke: starting one confirmed sandboxed Fireworks call",
            file=stdout,
        )
        smoke_evidence = smoke_runner(
            settings,
            smoke_plan,
            agent_manifest,
        )
        if not _expect_bytes(
            _smoke_paths(settings)["attempt"],
            canonical_bytes(
                _smoke_attempt_document(settings, smoke_plan)
            ),
            label="smoke paid-attempt receipt",
        ):
            raise MatrixError(
                "paid smoke runner returned without its pre-call "
                "attempt receipt"
            )
        smoke_evidence = _validated_smoke_evidence(
            settings,
            smoke_plan,
            smoke_evidence,
        )
        _materialize_smoke(settings, smoke_plan, smoke_evidence)
        print(
            "smoke: PASS; provider usage captured and action/v1 validated",
            file=stdout,
        )
    if not _receipt_estimate_within_two_x(
        smoke_evidence.receipt_document,
        "smoke receipt",
    ):
        raise MatrixError(
            "smoke cost estimate is not within 2x of actual; "
            "no full pack will start"
        )
    smoke_actual = _smoke_actual_cost(smoke_evidence.receipt_document)
    runner = _run_paid_bundle if paid_runner is None else paid_runner

    for plan in plans:
        if plan.pack_id in evidence:
            evidence[plan.pack_id] = _materialize_pack_evidence(
                settings,
                plan,
                evidence[plan.pack_id],
            )
            if not _receipt_estimate_within_two_x(
                evidence[plan.pack_id].receipt_document,
                f"{plan.pack_id} receipt",
            ):
                raise MatrixError(
                    f"{plan.pack_id}: pre-run estimate is not within 2x "
                    "of actual"
                )
            continue
        bound = _remaining_bound(
            settings,
            plans,
            evidence,
            smoke_actual=smoke_actual,
        )
        if bound >= MATRIX_COST_LIMIT_USD:
            raise MatrixError(
                f"refusing to start {plan.pack_id}: actual plus remaining "
                f"estimates ${_decimal_text(bound)} is not strictly below "
                f"${MATRIX_COST_LIMIT_TEXT}"
            )
        print(
            f"{plan.pack_id}: starting confirmed sandboxed Fireworks run "
            f"(estimate ${_decimal_text(_estimated_cost(plan))}; "
            f"authorized <=${_decimal_text(_authorization_cost(plan))})",
            file=stdout,
        )
        runner(settings, plan, agent_manifest)
        if not _expect_bytes(
            _paths(settings, plan)["attempt"],
            canonical_bytes(_pack_attempt_document(settings, plan)),
            label=f"{plan.pack_id} paid-attempt receipt",
        ):
            raise MatrixError(
                f"{plan.pack_id}: paid runner returned without its "
                "pre-call attempt receipt"
            )
        item = _verify_bundle_evidence(settings, plan, agent_manifest)
        item = _materialize_pack_evidence(settings, plan, item)
        evidence[plan.pack_id] = item
        if not _receipt_estimate_within_two_x(
            item.receipt_document,
            f"{plan.pack_id} receipt",
        ):
            raise MatrixError(
                f"{plan.pack_id}: pre-run estimate is not within 2x of "
                "actual; no further pack will start"
            )
        known_cost = _receipt_cost(item.receipt_document)
        accounting_cost = _receipt_accounting_cost(
            item.receipt_document
        )
        reserve = accounting_cost - known_cost
        if reserve:
            print(
                f"{plan.pack_id}: COMPLETE + exact replay; known actual "
                f"${_decimal_text(known_cost)}, accounting upper "
                f"${_decimal_text(accounting_cost)}",
                file=stdout,
            )
        else:
            print(
                f"{plan.pack_id}: COMPLETE + exact replay; actual "
                f"${_decimal_text(known_cost)}",
                file=stdout,
            )
        if accounting_cost >= PACK_COST_LIMIT_USD:
            raise MatrixError(
                f"{plan.pack_id}: accounting upper bound "
                f"${_decimal_text(accounting_cost)} is not strictly below "
                f"${PACK_COST_LIMIT_TEXT}; "
                "no further pack will start"
            )
        accounted_so_far = (
            settings.prior_failed_run_reserve_usd
            + smoke_actual
            + sum(
            (
                _receipt_accounting_cost(current.receipt_document)
                for current in evidence.values()
            ),
            start=Decimal(0),
            )
        )
        if accounted_so_far >= MATRIX_COST_LIMIT_USD:
            raise MatrixError(
                "accounted matrix upper bound reached the aggregate limit; "
                "no further pack will start"
            )

    aggregate = _aggregate_receipt(
        settings,
        plans,
        evidence,
        smoke_evidence.receipt_document,
    )
    aggregate_path = settings.output_root / "receipt.json"
    expected = canonical_bytes(aggregate)
    if not _expect_bytes(
        aggregate_path,
        expected,
        label="aggregate receipt",
    ):
        _write_new(aggregate_path, expected)
    print(
        f"13-pack matrix COMPLETE: {aggregate_path}",
        file=stdout,
    )
    return aggregate


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _non_negative_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(
            "must be a finite non-negative decimal"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError(
            "must be a finite non-negative decimal"
        )
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or explicitly authorize the Fireworks LLM baseline over "
            "all 13 hydrated packs."
        )
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--input-usd-per-million", required=True)
    parser.add_argument(
        "--cached-input-usd-per-million",
        required=True,
    )
    parser.add_argument("--output-usd-per-million", required=True)
    parser.add_argument("--agent-image-digest", required=True)
    parser.add_argument("--gateway-image-digest", required=True)
    parser.add_argument("--guard-image-digest", required=True)
    parser.add_argument(
        "--packs-root",
        type=Path,
        default=ROOT / "packs",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="fresh matrix directory, or an existing directory with --resume",
    )
    parser.add_argument("--temperature", default="0")
    parser.add_argument(
        "--max-output-tokens",
        type=_positive_int,
        default=128,
    )
    parser.add_argument(
        "--provider-timeout-seconds",
        type=_positive_int,
        default=600,
    )
    parser.add_argument(
        "--response-deadline-ms",
        type=_positive_int,
        default=660_000,
    )
    parser.add_argument("--lookback-bars", type=_positive_int)
    parser.add_argument("--funding-prints", type=_non_negative_int)
    parser.add_argument(
        "--host-http-port",
        type=_positive_int,
        default=18_080,
    )
    parser.add_argument(
        "--prior-failed-run-reserve-usd",
        type=_non_negative_decimal,
        default=Decimal(0),
        help=(
            "conservative accounting reserve for prior paid attempts whose "
            "provider usage is unavailable"
        ),
    )
    parser.add_argument(
        "--prior-failure-receipt",
        type=Path,
        help=(
            "canonical FAILED_CLOSED incident receipt supporting a non-zero "
            "prior failed-run reserve"
        ),
    )
    parser.add_argument(
        "--prior-failure-receipt-sha256",
        help=(
            "sha256:<hex> commitment to the incident receipt supporting "
            "a non-zero prior failed-run reserve"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse only byte-matching, verified COMPLETE, exact-replay bundles",
    )
    parser.add_argument(
        "--confirm-transmission-and-spend",
        action="store_true",
        help=(
            "explicitly authorize transmitting pack observations to Fireworks "
            "and incurring the displayed bounded spend"
        ),
    )
    return parser


def _settings(args: argparse.Namespace) -> MatrixSettings:
    model = _validate_model(cast(str, args.model))
    agent_digest = _validate_digest(
        cast(str, args.agent_image_digest),
        "agent image digest",
    )
    gateway_digest = _validate_digest(
        cast(str, args.gateway_image_digest),
        "gateway image digest",
    )
    guard_digest = _validate_digest(
        cast(str, args.guard_image_digest),
        "guard image digest",
    )
    host_port = cast(int, args.host_http_port)
    if not 1024 <= host_port <= 65_535:
        raise MatrixError("host HTTP port must be between 1024 and 65535")
    pricing = ModelPricing.from_strings(
        input_usd_per_million=cast(
            str,
            args.input_usd_per_million,
        ),
        cached_input_usd_per_million=cast(
            str,
            args.cached_input_usd_per_million,
        ),
        output_usd_per_million=cast(
            str,
            args.output_usd_per_million,
        ),
    )
    llm_config = LLMConfig(
        base_url=FIREWORKS_BASE_URL,
        model=model,
        temperature=cast(str, args.temperature),
        max_tokens=cast(int, args.max_output_tokens),
        timeout_seconds=cast(int, args.provider_timeout_seconds),
        api_key_env_name="FIREWORKS_API_KEY",
    )
    episode_config = EpisodeConfig(
        lookback_bars=cast(int | None, args.lookback_bars),
        funding_prints=cast(int | None, args.funding_prints),
        response_deadline_ms=cast(int, args.response_deadline_ms),
        parse_failure_retries=1,
    )
    packs_root = cast(Path, args.packs_root).absolute()
    output_root = cast(Path, args.output_root).absolute()
    prior_failure_path_raw = cast(
        Path | None,
        args.prior_failure_receipt,
    )
    prior_failure_path = (
        None
        if prior_failure_path_raw is None
        else prior_failure_path_raw.absolute()
    )
    prior_failure_hash_raw = cast(
        str | None,
        args.prior_failure_receipt_sha256,
    )
    prior_failure_hash = (
        None
        if prior_failure_hash_raw is None
        else _validate_digest(
            prior_failure_hash_raw,
            "prior failure receipt hash",
        )
    )
    return MatrixSettings(
        repo_root=ROOT,
        packs_root=packs_root,
        output_root=output_root,
        llm_config=llm_config,
        pricing=pricing,
        episode_config=episode_config,
        agent_image_digest=agent_digest,
        gateway_image_digest=gateway_digest,
        guard_image_digest=guard_digest,
        host_http_port=host_port,
        prior_failed_run_reserve_usd=cast(
            Decimal,
            args.prior_failed_run_reserve_usd,
        ),
        prior_failure_receipt_path=prior_failure_path,
        prior_failure_receipt_sha256=prior_failure_hash,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings = _settings(args)
        agent_manifest = build_llm_manifest(
            settings.llm_config,
            image_digest=settings.agent_image_digest,
        )
        plans, smoke_plan = _build_plans(settings, agent_manifest)
        execute_matrix(
            settings,
            plans,
            smoke_plan,
            confirmed=cast(bool, args.confirm_transmission_and_spend),
            resume=cast(bool, args.resume),
            stdout=sys.stdout,
        )
    except (MatrixError, ValueError, OSError) as exc:
        print(f"reference matrix refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Unexpected exceptions are named but not interpolated: provider
        # response bodies, environment values, and chained diagnostics must
        # never become terminal output.
        print(
            "reference matrix failed safely: "
            f"{type(exc).__name__}; inspect local immutable evidence",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
