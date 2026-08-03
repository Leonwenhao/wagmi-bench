# SPDX-License-Identifier: Apache-2.0
"""Deterministic M5 survival reports generated exclusively from IC-5 bundles.

This module deliberately imports no engine, pack, or live-run state.  The
sealed bundle is the sole input: recorder verification first establishes a
``COMPLETE`` evidence root, then the renderers read only files committed by
that bundle.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path, PurePosixPath
from typing import TypeGuard, cast

from recorder.verify import verify_bundle

JsonObject = dict[str, object]

CLAIM_LABEL = "survival-stress"
MEMORIZATION_CAVEAT = (
    "Memorization caveat: historical scenarios may be recognized from "
    "pretrained knowledge, price action, and venue-constant era "
    "fingerprinting (including funding caps or floors, fee tier, tick size, "
    "quantity step, and minimum notional)."
)
EVIDENCE_LIMIT = (
    "This artifact is survival/stress evidence only; it does not establish "
    "predictive capability or future performance."
)
FLAT_HOLD_NOTE = (
    "Zero executed fills: the agent never took a position, so this verdict "
    "reflects sitting out the episode, not surviving market exposure."
)

_PROFILE_ORDER = ("primary", "stress_2x")
_PROFILE_LABELS = {
    "primary": "Primary costs",
    "stress_2x": "Stress 2x costs",
}
_PROFILE_FIELDS = (
    "net_return_1e8",
    "max_drawdown_1e8",
    "sortino_1e8",
    "cvar5_1e8",
    "funding_paid_micro",
    "fees_paid_micro",
    "fill_cost_micro",
    "turnover_1e8",
    "dist_to_liq_min_1e8",
    "dist_to_liq_p05_1e8",
    "dist_to_liq_p25_1e8",
    "dist_to_liq_median_1e8",
    "equity_curve_ref",
)
_SAFETY_EVENT_TYPES = frozenset(
    {
        "EgressBlocked",
        "KillSwitchTriggered",
        "LiquidationTriggered",
        "NearLiquidation",
        "PostKillSwitchAttempt",
    }
)
_DELTA_FIELDS = (
    ("d_price_pnl_micro", "price"),
    ("d_funding_micro", "funding"),
    ("d_fees_micro", "fees"),
    ("d_liq_penalty_micro", "liquidation penalty"),
)


class ReportError(RuntimeError):
    """A trustworthy report cannot be rendered from the supplied bundle."""


@dataclass(frozen=True, slots=True)
class ReportArtifacts:
    """All deterministic reader surfaces for one complete bundle."""

    terminal_text: str
    html: str
    share_card_svg: str


@dataclass(frozen=True, slots=True)
class ReportFiles:
    """Paths written by :func:`write_report_files`."""

    terminal_text: Path
    html: Path
    share_card_svg: Path


@dataclass(frozen=True, slots=True)
class _Evidence:
    root_dir: Path
    bundle_root: str
    manifest: JsonObject
    agent_manifest: JsonObject
    metrics: JsonObject
    profiles: Mapping[str, JsonObject]
    invariant: JsonObject
    events: tuple[JsonObject, ...]
    decisions: tuple[JsonObject, ...]
    observations: tuple[JsonObject, ...]
    raw_text: Mapping[str, str | None]
    primary_ledger: tuple[JsonObject, ...]
    stress_ledger: tuple[JsonObject, ...]


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_float(token: str) -> None:
    raise ReportError(f"fractional JSON number is forbidden: {token}")


def _reject_constant(token: str) -> None:
    raise ReportError(f"non-finite JSON number is forbidden: {token}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ReportError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_object(raw: bytes, *, source: str) -> JsonObject:
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
        raise ReportError(f"{source}: invalid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ReportError(
            f"{source}: invalid JSON at byte {exc.pos}"
        ) from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ReportError(f"{source}: expected a JSON object")
    return cast(JsonObject, value)


def _load_object(path: Path, *, label: str | None = None) -> JsonObject:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReportError(f"cannot read bundle file: {path.name}") from exc
    return _decode_object(raw, source=label or path.name)


def _load_jsonl(path: Path) -> tuple[JsonObject, ...]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReportError(f"cannot read bundle file: {path.name}") from exc
    rows: list[JsonObject] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line:
            raise ReportError(f"{path.name}:{line_number}: blank JSONL line")
        rows.append(
            _decode_object(
                line,
                source=f"{path.name}:{line_number}",
            )
        )
    return tuple(rows)


def _as_object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ReportError(f"{context} must be an object")
    return cast(JsonObject, value)


def _as_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ReportError(f"{context} must be an array")
    return value


def _as_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ReportError(f"{context} must be a string")
    return value


def _as_int(value: object, context: str) -> int:
    if not _is_int(value):
        raise ReportError(f"{context} must be an integer")
    return value


def _as_bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise ReportError(f"{context} must be a boolean")
    return value


def _safe_ref(
    root: Path,
    relative: str,
    *,
    required: bool,
) -> Path | None:
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ReportError(f"unsafe bundle reference: {relative!r}")
    candidate = root.joinpath(*pure.parts)
    if candidate.is_symlink():
        raise ReportError(f"bundle reference is a symlink: {relative}")
    try:
        resolved = candidate.resolve(strict=required)
    except OSError as exc:
        if not required:
            return None
        raise ReportError(f"missing bundle reference: {relative}") from exc
    if not resolved.is_relative_to(root.resolve()):
        raise ReportError(f"bundle reference escapes root: {relative}")
    if not resolved.exists():
        return None
    if not resolved.is_file():
        raise ReportError(f"bundle reference is not a file: {relative}")
    return resolved


def _redacted_raw_paths(root: Path) -> frozenset[str]:
    redaction_path = root / "redaction.json"
    if not redaction_path.exists():
        return frozenset()
    redaction = _load_object(redaction_path)
    removals = _as_list(redaction.get("removals"), "redaction.removals")
    paths: set[str] = set()
    for index, removal_value in enumerate(removals):
        removal = _as_object(
            removal_value,
            f"redaction.removals[{index}]",
        )
        paths.add(
            _as_str(
                removal.get("path"),
                f"redaction.removals[{index}].path",
            )
        )
    return frozenset(paths)


def _load_evidence(bundle_dir: str | Path) -> _Evidence:
    root = Path(bundle_dir)
    verification = verify_bundle(root)
    if not verification.is_complete:
        raise ReportError(
            "report requires a COMPLETE bundle; verifier returned "
            f"{verification.verdict}: {verification.message}"
        )
    if verification.root is None:
        raise ReportError("complete bundle verification returned no root")

    manifest = _load_object(root / "manifest.json")
    agent_manifest = _load_object(root / "agent_manifest.json")
    metrics = _load_object(root / "metrics.json")
    run_id = _as_str(manifest.get("run_id"), "manifest.run_id")
    if metrics.get("run_id") != run_id:
        raise ReportError("metrics.run_id does not match manifest.run_id")
    if metrics.get("claim_label") != CLAIM_LABEL:
        raise ReportError(
            f"metrics.claim_label must be {CLAIM_LABEL!r}"
        )

    profiles_value = _as_object(metrics.get("profiles"), "metrics.profiles")
    profiles: dict[str, JsonObject] = {}
    for profile_name in _PROFILE_ORDER:
        profile = _as_object(
            profiles_value.get(profile_name),
            f"metrics.profiles.{profile_name}",
        )
        if tuple(sorted(profile)) != tuple(sorted(_PROFILE_FIELDS)):
            raise ReportError(
                f"metrics.profiles.{profile_name} has an unexpected key set"
            )
        profiles[profile_name] = profile
    if set(profiles["primary"]) != set(profiles["stress_2x"]):
        raise ReportError("cost-profile metric key sets differ")

    invariant = _as_object(
        metrics.get("profile_invariant"),
        "metrics.profile_invariant",
    )
    events = _load_jsonl(root / "events.jsonl")
    decisions_count = _as_int(
        invariant.get("turns"),
        "metrics.profile_invariant.turns",
    )
    decisions: list[JsonObject] = []
    observations: list[JsonObject] = []
    redacted_paths = _redacted_raw_paths(root)
    raw_text: dict[str, str | None] = {}

    for turn in range(decisions_count):
        decision = _load_object(
            root / "decisions" / f"{turn:04d}.json",
            label=f"decisions/{turn:04d}.json",
        )
        if decision.get("run_id") != run_id or decision.get("turn") != turn:
            raise ReportError(f"decision {turn} identity mismatch")
        saw = _as_object(decision.get("saw"), f"decision {turn}.saw")
        observation_ref = _as_str(
            saw.get("observation_ref"),
            f"decision {turn}.saw.observation_ref",
        )
        if observation_ref != f"observations/{turn:04d}.json":
            raise ReportError(
                f"decision {turn} has non-canonical observation_ref"
            )
        observation_path = _safe_ref(
            root,
            observation_ref,
            required=True,
        )
        if observation_path is None:
            raise ReportError(f"missing observation for turn {turn}")
        observation = _load_object(
            observation_path,
            label=observation_ref,
        )
        episode = _as_object(
            observation.get("episode"),
            f"observation {turn}.episode",
        )
        if episode.get("turn") != turn:
            raise ReportError(f"observation {turn} turn mismatch")

        said = _as_object(decision.get("said"), f"decision {turn}.said")
        attempts = _as_list(
            said.get("attempts"),
            f"decision {turn}.said.attempts",
        )
        for attempt_index, attempt_value in enumerate(attempts):
            attempt = _as_object(
                attempt_value,
                f"decision {turn}.said.attempts[{attempt_index}]",
            )
            raw_ref = _as_str(
                attempt.get("raw_ref"),
                f"decision {turn}.attempt[{attempt_index}].raw_ref",
            )
            if raw_ref in raw_text:
                continue
            raw_path = _safe_ref(root, raw_ref, required=False)
            if raw_path is None:
                if raw_ref not in redacted_paths:
                    raise ReportError(
                        f"raw response missing without disclosure: {raw_ref}"
                    )
                raw_text[raw_ref] = None
            else:
                try:
                    raw_text[raw_ref] = raw_path.read_bytes().decode(
                        "utf-8",
                        errors="replace",
                    )
                except OSError as exc:
                    raise ReportError(
                        f"cannot read raw response: {raw_ref}"
                    ) from exc
        decisions.append(decision)
        observations.append(observation)

    primary = _load_jsonl(root / "ledger.jsonl")
    stress = _load_jsonl(root / "ledger_stress_2x.jsonl")
    if len(primary) != len(stress):
        raise ReportError("cost-profile ledger lengths differ")
    if len(primary) != decisions_count + 1:
        raise ReportError(
            "ledger rows must equal decision turns plus the opening anchor"
        )

    return _Evidence(
        root_dir=root,
        bundle_root=verification.root,
        manifest=manifest,
        agent_manifest=agent_manifest,
        metrics=metrics,
        profiles=profiles,
        invariant=invariant,
        events=events,
        decisions=tuple(decisions),
        observations=tuple(observations),
        raw_text=raw_text,
        primary_ledger=primary,
        stress_ledger=stress,
    )


def _fixed(value: int, scale: int, places: int) -> str:
    sign = "-" if value < 0 else ""
    whole, remainder = divmod(abs(value), scale)
    if places == 0:
        return f"{sign}{whole}"
    fraction = remainder * (10**places) // scale
    return f"{sign}{whole}.{fraction:0{places}d}"


def _money(value: int, *, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{_fixed(value, 1_000_000, 6)} quote"


def _percent(value: int | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{_fixed(value * 100, 100_000_000, 4)}%"


def _ratio(value: int) -> str:
    return _fixed(value, 100_000_000, 4)


def _leverage(value: int) -> str:
    return f"{_fixed(value, 10_000, 4)}x"


def _sanitize_text(value: str) -> str:
    safe: list[str] = []
    for character in value.replace("\r\n", "\n").replace("\r", "\n"):
        codepoint = ord(character)
        if character == "\n":
            safe.append("\\n")
        elif character == "\t":
            safe.append("\\t")
        elif codepoint < 32 or codepoint == 127:
            safe.append(f"\\x{codepoint:02x}")
        else:
            safe.append(character)
    return "".join(safe)


def _preview(value: str, limit: int = 280) -> str:
    sanitized = _sanitize_text(value)
    if len(sanitized) <= limit:
        return sanitized
    return sanitized[: limit - 3] + "..."


def _json_pretty(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    )


def _json_compact(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pack_id(evidence: _Evidence) -> str:
    pack = _as_object(evidence.manifest.get("pack"), "manifest.pack")
    return _as_str(pack.get("pack_id"), "manifest.pack.pack_id")


def _run_id(evidence: _Evidence) -> str:
    return _as_str(evidence.manifest.get("run_id"), "manifest.run_id")


def _agent_label(evidence: _Evidence) -> str:
    name = _as_str(evidence.agent_manifest.get("name"), "agent_manifest.name")
    model = _as_str(
        evidence.agent_manifest.get("model_id"),
        "agent_manifest.model_id",
    )
    return f"{name} ({model})"


def _verdict(evidence: _Evidence) -> str:
    value = _as_str(
        evidence.invariant.get("survival_verdict"),
        "metrics.profile_invariant.survival_verdict",
    )
    return value.replace("_", " ").upper()


def _is_flat_hold(evidence: _Evidence) -> bool:
    fills, _ = _execution_counts(evidence)
    return fills == 0 and _verdict(evidence) == "SURVIVED"


def _display_verdict(evidence: _Evidence) -> str:
    verdict = _verdict(evidence)
    if _is_flat_hold(evidence):
        return f"{verdict} — FLAT-HOLD"
    return verdict


def _svg_pack_line(evidence: _Evidence) -> str:
    pack = _pack_id(evidence)
    if _is_flat_hold(evidence):
        return f"{pack} · 0 fills — never took a position"
    return pack


def _profile_metric(
    evidence: _Evidence,
    profile_name: str,
    field: str,
) -> int | str | None:
    profile = evidence.profiles[profile_name]
    value = profile.get(field)
    if value is None or isinstance(value, str) or _is_int(value):
        return value
    raise ReportError(f"metrics.profiles.{profile_name}.{field} is invalid")


def _profile_rows(
    evidence: _Evidence,
) -> tuple[tuple[str, str, str], ...]:
    def metric_int(profile: str, field: str) -> int:
        return _as_int(
            _profile_metric(evidence, profile, field),
            f"metrics.profiles.{profile}.{field}",
        )

    def metric_nullable(profile: str, field: str) -> int | None:
        value = _profile_metric(evidence, profile, field)
        if value is None:
            return None
        return _as_int(value, f"metrics.profiles.{profile}.{field}")

    values: list[tuple[str, str, str]] = []
    formatters: tuple[tuple[str, str, Callable[[int], str]], ...] = (
        (
            "End NAV change",
            "net_return_1e8",
            lambda value: _percent(value, signed=True),
        ),
        (
            "Maximum drawdown",
            "max_drawdown_1e8",
            lambda value: _percent(value),
        ),
        (
            "Worst 5% bar tail",
            "cvar5_1e8",
            lambda value: _percent(value, signed=True),
        ),
        (
            "Downside-adjusted ratio",
            "sortino_1e8",
            lambda value: _ratio(value),
        ),
        (
            "Funding flow",
            "funding_paid_micro",
            lambda value: _money(value, signed=True),
        ),
        (
            "Fees",
            "fees_paid_micro",
            lambda value: _money(value, signed=True),
        ),
        (
            "Spread + impact",
            "fill_cost_micro",
            lambda value: _money(value, signed=True),
        ),
        (
            "Turnover / starting NAV",
            "turnover_1e8",
            lambda value: _ratio(value) + "x",
        ),
    )
    for label, field, formatter in formatters:
        primary_value = metric_int("primary", field)
        stress_value = metric_int("stress_2x", field)
        values.append(
            (
                label,
                formatter(primary_value),
                formatter(stress_value),
            )
        )
    for label, field in (
        ("Minimum wick distance to liquidation", "dist_to_liq_min_1e8"),
        ("5th percentile distance to liquidation", "dist_to_liq_p05_1e8"),
        ("25th percentile distance to liquidation", "dist_to_liq_p25_1e8"),
        ("Median distance to liquidation", "dist_to_liq_median_1e8"),
    ):
        values.append(
            (
                label,
                _percent(metric_nullable("primary", field)),
                _percent(metric_nullable("stress_2x", field)),
            )
        )
    return tuple(values)


def _discipline_events(evidence: _Evidence) -> int:
    fields = (
        "invalid_actions",
        "missed_decisions",
        "gate_blocks",
        "post_kill_switch_attempts",
        "egress_blocked_count",
    )
    return sum(
        _as_int(
            evidence.invariant.get(field),
            f"metrics.profile_invariant.{field}",
        )
        for field in fields
    )


def _execution_counts(evidence: _Evidence) -> tuple[int, int]:
    fills = 0
    cancels = 0
    for turn, decision in enumerate(evidence.decisions):
        happened = _as_object(
            decision.get("happened"),
            f"decision {turn}.happened",
        )
        fills += len(
            _as_list(
                happened.get("fills"),
                f"decision {turn}.happened.fills",
            )
        )
        cancels += len(
            _as_list(
                happened.get("cancels"),
                f"decision {turn}.happened.cancels",
            )
        )
    return fills, cancels


def _observation_summary(
    observation: JsonObject,
    *,
    context: str,
) -> str:
    episode = _as_object(observation.get("episode"), f"{context}.episode")
    account = _as_object(observation.get("account"), f"{context}.account")
    risk = _as_object(observation.get("risk"), f"{context}.risk")
    markets = _as_object(observation.get("markets"), f"{context}.markets")
    positions = _as_object(
        observation.get("position"),
        f"{context}.position",
    )
    parts = [
        "clock_ts="
        + str(_as_int(observation.get("clock_ts"), f"{context}.clock_ts")),
        "bars_remaining="
        + str(
            _as_int(
                episode.get("bars_remaining"),
                f"{context}.episode.bars_remaining",
            )
        ),
        "nav="
        + _money(
            _as_int(
                account.get("nav_micro"),
                f"{context}.account.nav_micro",
            )
        ),
        "kill_switch="
        + (
            "active"
            if _as_bool(
                risk.get("kill_switch_active"),
                f"{context}.risk.kill_switch_active",
            )
            else "inactive"
        ),
    ]
    for alias in sorted(markets):
        market = _as_object(markets[alias], f"{context}.markets.{alias}")
        bars = _as_list(market.get("bars"), f"{context}.markets.{alias}.bars")
        close = "n/a"
        if bars:
            last = _as_object(
                bars[-1],
                f"{context}.markets.{alias}.bars[-1]",
            )
            close = str(
                _as_int(last.get("c"), f"{context}.markets.{alias}.bars[-1].c")
            )
        position = _as_object(
            positions.get(alias),
            f"{context}.position.{alias}",
        )
        distance_value = position.get("dist_to_liq_1e8")
        distance = (
            None
            if distance_value is None
            else _as_int(
                distance_value,
                f"{context}.position.{alias}.dist_to_liq_1e8",
            )
        )
        parts.append(
            f"{alias}[trade_close_ticks={close},"
            f"qty_base_1e8={_as_int(position.get('qty_base_1e8'), f'{context}.position.{alias}.qty_base_1e8')},"
            f"distance_to_liquidation={_percent(distance)}]"
        )
    return "; ".join(parts)


def _attempt_lines(
    evidence: _Evidence,
    decision: JsonObject,
    *,
    turn: int,
    full_text: bool,
) -> tuple[str, ...]:
    said = _as_object(decision.get("said"), f"decision {turn}.said")
    attempts = _as_list(
        said.get("attempts"),
        f"decision {turn}.said.attempts",
    )
    if not attempts:
        return ("no response bytes recorded",)
    lines: list[str] = []
    for index, value in enumerate(attempts):
        attempt = _as_object(value, f"decision {turn}.attempt[{index}]")
        raw_ref = _as_str(
            attempt.get("raw_ref"),
            f"decision {turn}.attempt[{index}].raw_ref",
        )
        raw_hash = _as_str(
            attempt.get("raw_sha256"),
            f"decision {turn}.attempt[{index}].raw_sha256",
        )
        attempt_number = _as_int(
            attempt.get("attempt"),
            f"decision {turn}.attempt[{index}].attempt",
        )
        latency = _as_int(
            attempt.get("latency_ms"),
            f"decision {turn}.attempt[{index}].latency_ms",
        )
        transport = _as_str(
            attempt.get("transport"),
            f"decision {turn}.attempt[{index}].transport",
        )
        response = evidence.raw_text.get(raw_ref)
        if response is None:
            rendered = "[raw model text absent under disclosed share redaction]"
        elif full_text:
            rendered = _sanitize_text(response)
        else:
            rendered = _preview(response)
        lines.append(
            f"attempt {attempt_number}; {transport}; {latency} ms; "
            f"{raw_ref}; {raw_hash}; response={rendered}"
        )
    return tuple(lines)


def _meant_line(decision: JsonObject, *, turn: int) -> str:
    meant = _as_object(decision.get("meant"), f"decision {turn}.meant")
    status = _as_str(meant.get("status"), f"decision {turn}.meant.status")
    if status == "rejected":
        rejected = _as_object(
            meant.get("rejected"),
            f"decision {turn}.meant.rejected",
        )
        return (
            "rejected: "
            + _as_str(
                rejected.get("reason"),
                f"decision {turn}.meant.rejected.reason",
            )
            + " — "
            + _as_str(
                rejected.get("detail"),
                f"decision {turn}.meant.rejected.detail",
            )
        )
    action = _as_object(
        meant.get("action"),
        f"decision {turn}.meant.action",
    )
    targets = _as_object(
        action.get("target_lev_1e4"),
        f"decision {turn}.meant.action.target_lev_1e4",
    )
    target_text = ", ".join(
        f"{alias}={_leverage(_as_int(targets[alias], f'decision {turn}.target.{alias}'))}"
        for alias in sorted(targets)
    )
    slippage = action.get("max_slippage_bps")
    slippage_text = (
        "none"
        if slippage is None
        else str(
            _as_int(
                slippage,
                f"decision {turn}.meant.action.max_slippage_bps",
            )
        )
        + " bps"
    )
    return f"parsed leverage target: {target_text}; max slippage={slippage_text}"


def _rule_lines(decision: JsonObject, *, turn: int) -> tuple[str, ...]:
    values = _as_list(decision.get("rules"), f"decision {turn}.rules")
    if not values:
        return ("no risk gates ran because no action was parsed",)
    lines: list[str] = []
    for index, value in enumerate(values):
        rule = _as_object(value, f"decision {turn}.rules[{index}]")
        lines.append(
            f"{_as_str(rule.get('constraint_id'), f'decision {turn}.rules[{index}].constraint_id')}"
            f" [{_as_str(rule.get('constraint_type'), f'decision {turn}.rules[{index}].constraint_type')}]"
            f" scope={_as_str(rule.get('scope'), f'decision {turn}.rules[{index}].scope')}"
            f" observed={_as_int(rule.get('observed'), f'decision {turn}.rules[{index}].observed')}"
            f" limit={_as_int(rule.get('limit'), f'decision {turn}.rules[{index}].limit')}"
            f" {_as_str(rule.get('unit'), f'decision {turn}.rules[{index}].unit')}"
            f" => {_as_str(rule.get('verdict'), f'decision {turn}.rules[{index}].verdict').upper()}"
        )
    return tuple(lines)


def _happened_lines(
    decision: JsonObject,
    *,
    turn: int,
) -> tuple[str, ...]:
    happened = _as_object(
        decision.get("happened"),
        f"decision {turn}.happened",
    )
    lines: list[str] = []
    fills = _as_list(
        happened.get("fills"),
        f"decision {turn}.happened.fills",
    )
    for index, value in enumerate(fills):
        fill = _as_object(
            value,
            f"decision {turn}.happened.fills[{index}]",
        )
        lines.append(
            "fill: "
            f"{_as_str(fill.get('market'), f'decision {turn}.fill[{index}].market')} "
            f"{_as_str(fill.get('side'), f'decision {turn}.fill[{index}].side')} "
            f"qty_base_1e8={_as_int(fill.get('qty_base_1e8'), f'decision {turn}.fill[{index}].qty_base_1e8')} "
            f"at { _as_int(fill.get('fill_px_ticks'), f'decision {turn}.fill[{index}].fill_px_ticks')} ticks; "
            f"fee={_money(-_as_int(fill.get('fee_micro'), f'decision {turn}.fill[{index}].fee_micro'))}"
        )
    cancels = _as_list(
        happened.get("cancels"),
        f"decision {turn}.happened.cancels",
    )
    for index, value in enumerate(cancels):
        cancel = _as_object(
            value,
            f"decision {turn}.happened.cancels[{index}]",
        )
        lines.append(
            "cancel: "
            f"{_as_str(cancel.get('market'), f'decision {turn}.cancel[{index}].market')} "
            f"{_as_str(cancel.get('reason'), f'decision {turn}.cancel[{index}].reason')}; "
            f"qty_base_1e8={_as_int(cancel.get('cancelled_qty_base_1e8'), f'decision {turn}.cancel[{index}].cancelled_qty_base_1e8')}"
        )
    liquidation = happened.get("liquidation")
    if liquidation is not None:
        liquidation_value = _as_object(
            liquidation,
            f"decision {turn}.happened.liquidation",
        )
        lines.append(
            "liquidation: "
            + _json_compact(liquidation_value)
        )
    post_kill = happened.get("post_kill_switch_attempt")
    if post_kill is not None:
        lines.append(
            "post-kill-switch attempt: "
            + _json_compact(
                _as_object(
                    post_kill,
                    f"decision {turn}.happened.post_kill_switch_attempt",
                )
            )
        )
    if not lines:
        lines.append("no fill, cancellation, liquidation, or post-kill attempt")
    return tuple(lines)


def _cost_lines(decision: JsonObject, *, turn: int) -> tuple[str, ...]:
    cost = _as_object(
        decision.get("cost_to_hold"),
        f"decision {turn}.cost_to_hold",
    )
    lines: list[str] = []
    funding = _as_list(
        cost.get("funding"),
        f"decision {turn}.cost_to_hold.funding",
    )
    if not funding:
        lines.append("funding: no settlement crossed")
    for index, value in enumerate(funding):
        row = _as_object(
            value,
            f"decision {turn}.cost_to_hold.funding[{index}]",
        )
        lines.append(
            "funding: "
            f"{_as_str(row.get('market'), f'decision {turn}.funding[{index}].market')} "
            f"rate={_percent(_as_int(row.get('rate_1e8'), f'decision {turn}.funding[{index}].rate_1e8'), signed=True)} "
            f"flow={_money(_as_int(row.get('amount_micro'), f'decision {turn}.funding[{index}].amount_micro'), signed=True)}"
        )
    margin = cost.get("margin_after")
    if margin is None:
        lines.append("margin after: flat for the holding bar")
    else:
        margin_value = _as_object(
            margin,
            f"decision {turn}.cost_to_hold.margin_after",
        )
        distance_value = margin_value.get("min_intrabar_dist_to_liq_1e8")
        distance = (
            None
            if distance_value is None
            else _as_int(
                distance_value,
                f"decision {turn}.margin.min_intrabar_dist_to_liq_1e8",
            )
        )
        lines.append(
            "margin after: "
            f"{_as_str(margin_value.get('market'), f'decision {turn}.margin.market')} "
            f"qty_base_1e8={_as_int(margin_value.get('position_qty_base_1e8'), f'decision {turn}.margin.position_qty_base_1e8')}; "
            f"closest wick distance to liquidation={_percent(distance)}"
        )
    return tuple(lines)


def _account_line(decision: JsonObject, *, turn: int) -> str:
    account = _as_object(
        decision.get("account_after"),
        f"decision {turn}.account_after",
    )
    d_nav = _as_int(
        account.get("d_nav_micro"),
        f"decision {turn}.account_after.d_nav_micro",
    )
    values = {
        field: _as_int(
            account.get(field),
            f"decision {turn}.account_after.{field}",
        )
        for field, _ in _DELTA_FIELDS
    }
    if d_nav != sum(values.values()):
        raise ReportError(f"decision {turn} violates the NAV attribution identity")
    largest_field, largest_label = max(
        _DELTA_FIELDS,
        key=lambda item: (abs(values[item[0]]), item[0]),
    )
    direction = "largest drag" if d_nav < 0 else "largest move"
    return (
        f"NAV={_money(_as_int(account.get('nav_micro'), f'decision {turn}.account_after.nav_micro'))}; "
        f"delta NAV={_money(d_nav, signed=True)} = "
        f"price {_money(values['d_price_pnl_micro'], signed=True)} + "
        f"funding {_money(values['d_funding_micro'], signed=True)} + "
        f"fees {_money(values['d_fees_micro'], signed=True)} + "
        f"liquidation penalty {_money(values['d_liq_penalty_micro'], signed=True)}; "
        f"{direction}: {largest_label} {_money(values[largest_field], signed=True)}"
    )


def _holding_bar_index(evidence: _Evidence, *, turn: int) -> int:
    row = evidence.primary_ledger[turn + 1]
    if row.get("turn") != turn:
        raise ReportError(f"primary ledger turn {turn} is not total")
    return _as_int(
        row.get("bar_index"),
        f"primary ledger turn {turn}.bar_index",
    )


def _event_summary(event: JsonObject, *, index: int) -> str:
    event_type = _as_str(event.get("type"), f"events[{index}].type")
    seq = _as_int(event.get("seq"), f"events[{index}].seq")
    turn_value = event.get("turn")
    bar_value = event.get("bar_index")
    turn = "run" if turn_value is None else str(_as_int(turn_value, "event.turn"))
    bar = "n/a" if bar_value is None else str(_as_int(bar_value, "event.bar_index"))
    payload = _as_object(event.get("payload"), f"events[{index}].payload")
    if event_type == "NearLiquidation":
        return (
            f"seq {seq}; turn {turn}; bar {bar}; NEAR LIQUIDATION; "
            f"{_as_str(payload.get('market'), 'NearLiquidation.market')} "
            f"{_as_str(payload.get('trigger'), 'NearLiquidation.trigger')}; "
            f"wick distance={_percent(_as_int(payload.get('min_intrabar_dist_to_liq_1e8'), 'NearLiquidation.distance'))}; "
            f"threshold={_percent(_as_int(payload.get('threshold_1e8'), 'NearLiquidation.threshold'))}"
        )
    if event_type == "EgressBlocked":
        return (
            f"seq {seq}; turn {turn}; bar {bar}; EGRESS BLOCKED; "
            f"{_as_str(payload.get('protocol'), 'EgressBlocked.protocol')} "
            f"{_as_str(payload.get('destination'), 'EgressBlocked.destination')}; "
            f"count={_as_int(payload.get('count'), 'EgressBlocked.count')}"
        )
    if event_type == "KillSwitchTriggered":
        return (
            f"seq {seq}; turn {turn}; bar {bar}; KILL SWITCH; "
            f"drawdown={_percent(_as_int(payload.get('drawdown_1e8'), 'KillSwitch.drawdown'))}; "
            f"limit={_percent(_as_int(payload.get('limit_1e8'), 'KillSwitch.limit'))}"
        )
    if event_type == "LiquidationTriggered":
        return (
            f"seq {seq}; turn {turn}; bar {bar}; LIQUIDATION; "
            f"{_as_str(payload.get('market'), 'Liquidation.market')}; "
            f"penalty={_money(-_as_int(payload.get('penalty_micro'), 'Liquidation.penalty'))}"
        )
    if event_type == "PostKillSwitchAttempt":
        return (
            f"seq {seq}; turn {turn}; bar {bar}; POST-KILL-SWITCH ATTEMPT; "
            + _json_compact(payload)
        )
    return f"seq {seq}; turn {turn}; bar {bar}; {event_type}; {_json_compact(payload)}"


def _safety_event_lines(evidence: _Evidence) -> tuple[str, ...]:
    lines = tuple(
        _event_summary(event, index=index)
        for index, event in enumerate(evidence.events)
        if event.get("type") in _SAFETY_EVENT_TYPES
    )
    return lines or ("no near-liquidation, liquidation, kill-switch, post-kill, or blocked-egress events",)


def _near_death_lines(evidence: _Evidence) -> tuple[str, ...]:
    lines = tuple(
        _event_summary(event, index=index)
        for index, event in enumerate(evidence.events)
        if event.get("type") in {"NearLiquidation", "LiquidationTriggered"}
    )
    return lines or ("no near-liquidation or liquidation events",)


def _terminal_report(evidence: _Evidence) -> str:
    invariant = evidence.invariant
    fills, cancels = _execution_counts(evidence)
    lines = [
        "WAGMI BENCH SURVIVAL REPORT",
        f"claim_label: {CLAIM_LABEL}",
        f"verdict: {_display_verdict(evidence)}",
        *([f"note: {FLAT_HOLD_NOTE}"] if _is_flat_hold(evidence) else []),
        f"pack: {_pack_id(evidence)}",
        f"agent: {_agent_label(evidence)}",
        f"run_id: {_run_id(evidence)}",
        f"bundle_root: {evidence.bundle_root}",
        "",
        "SURVIVAL-NATIVE SUMMARY",
        f"turns: {_as_int(invariant.get('turns'), 'invariant.turns')}",
        f"bars: {_as_int(invariant.get('bars'), 'invariant.bars')}",
        f"discipline events: {_discipline_events(evidence)}",
        f"invalid actions: {_as_int(invariant.get('invalid_actions'), 'invariant.invalid_actions')}",
        f"missed decisions: {_as_int(invariant.get('missed_decisions'), 'invariant.missed_decisions')}",
        f"gate blocks: {_as_int(invariant.get('gate_blocks'), 'invariant.gate_blocks')}",
        f"post-kill-switch attempts: {_as_int(invariant.get('post_kill_switch_attempts'), 'invariant.post_kill_switch_attempts')}",
        f"blocked egress count: {_as_int(invariant.get('egress_blocked_count'), 'invariant.egress_blocked_count')}",
        f"executed fills: {fills}",
        f"cancellations: {cancels}",
        "",
        "BOTH COST PROFILES — FUNDING / CHURN DECOMPOSITION",
        f"{'Metric':42} | {'Primary costs':24} | {'Stress 2x costs':24}",
        "-" * 98,
    ]
    for label, primary, stress in _profile_rows(evidence):
        lines.append(f"{label:42} | {primary:24} | {stress:24}")

    lines.extend(["", "EQUITY VERSUS PRICE"])
    lines.extend(_terminal_chart_lines(evidence))
    lines.extend(["", "NEAR-DEATH TIMELINE"])
    lines.extend(f"- {line}" for line in _near_death_lines(evidence))
    lines.extend(["", "SAFETY EVENT LOG"])
    lines.extend(f"- {line}" for line in _safety_event_lines(evidence))
    lines.extend(
        [
            "",
            "DECISION TIMELINE",
            "Each turn reads: SAW -> SAID -> MEANT -> RULES -> HAPPENED -> COST TO HOLD -> NAV ATTRIBUTION.",
        ]
    )
    for turn, (decision, observation) in enumerate(
        zip(evidence.decisions, evidence.observations, strict=True)
    ):
        bar_index = _as_int(
            decision.get("bar_index"),
            f"decision {turn}.bar_index",
        )
        holding_bar_index = _holding_bar_index(evidence, turn=turn)
        event_range = _as_object(
            decision.get("event_seq_range"),
            f"decision {turn}.event_seq_range",
        )
        lines.extend(
            [
                "",
                f"TURN {turn:04d} | DECISION BAR {bar_index} -> "
                f"NAV BAR {holding_bar_index} | events "
                f"{_as_int(event_range.get('first_seq'), 'event_range.first_seq')}"
                "-"
                f"{_as_int(event_range.get('last_seq'), 'event_range.last_seq')}",
                "  SAW: "
                + _observation_summary(
                    observation,
                    context=f"observation {turn}",
                ),
            ]
        )
        lines.extend(
            "  SAID: " + line
            for line in _attempt_lines(
                evidence,
                decision,
                turn=turn,
                full_text=False,
            )
        )
        lines.append("  MEANT: " + _meant_line(decision, turn=turn))
        lines.extend(
            "  RULE: " + line
            for line in _rule_lines(decision, turn=turn)
        )
        lines.extend(
            "  HAPPENED: " + line
            for line in _happened_lines(decision, turn=turn)
        )
        lines.extend(
            "  COST TO HOLD: " + line
            for line in _cost_lines(decision, turn=turn)
        )
        lines.append("  NAV ATTRIBUTION: " + _account_line(decision, turn=turn))

    lines.extend(
        [
            "",
            "EVIDENCE FOOTER",
            f"claim_label: {CLAIM_LABEL}",
            MEMORIZATION_CAVEAT,
            EVIDENCE_LIMIT,
            f"bundle_root: {evidence.bundle_root}",
        ]
    )
    return "\n".join(lines) + "\n"


def _normalized_series(values: Sequence[int]) -> tuple[int, ...]:
    if not values or values[0] <= 0:
        raise ReportError("chart series requires a positive opening value")
    opening = values[0]
    return tuple(value * 10_000 // opening for value in values)


def _chart_series(
    evidence: _Evidence,
) -> tuple[str, tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    primary_nav = tuple(
        _as_int(row.get("nav_micro"), "primary ledger nav_micro")
        for row in evidence.primary_ledger
    )
    stress_nav = tuple(
        _as_int(row.get("nav_micro"), "stress ledger nav_micro")
        for row in evidence.stress_ledger
    )
    first_positions = _as_object(
        evidence.primary_ledger[0].get("positions"),
        "primary ledger positions",
    )
    if not first_positions:
        raise ReportError("equity-vs-price chart requires a market position map")
    market = sorted(first_positions)[0]
    prices: list[int] = []
    for row_number, row in enumerate(evidence.primary_ledger):
        positions = _as_object(
            row.get("positions"),
            f"primary ledger row {row_number}.positions",
        )
        position = _as_object(
            positions.get(market),
            f"primary ledger row {row_number}.positions.{market}",
        )
        prices.append(
            _as_int(
                position.get("mark_px_ticks"),
                f"primary ledger row {row_number}.mark_px_ticks",
            )
        )
    return (
        market,
        _normalized_series(primary_nav),
        _normalized_series(stress_nav),
        _normalized_series(prices),
    )


def _terminal_chart_lines(evidence: _Evidence) -> tuple[str, ...]:
    market, primary, stress, price = _chart_series(evidence)
    count = len(primary)
    sample_count = min(16, count)
    indexes: tuple[int, ...]
    if sample_count == 1:
        indexes = (0,)
    else:
        indexes = tuple(
            sorted(
                {
                    index * (count - 1) // (sample_count - 1)
                    for index in range(sample_count)
                }
            )
        )
    lines = [
        f"All series normalized to 100.00 at the opening anchor; price is {market} mark.",
        f"{'Bar':>6} | {'Primary NAV':>12} | {'Stress NAV':>12} | {market + ' mark':>12}",
        "-" * 53,
    ]
    for index in indexes:
        bar_index = _as_int(
            evidence.primary_ledger[index].get("bar_index"),
            f"primary ledger row {index}.bar_index",
        )
        lines.append(
            f"{bar_index:6} | "
            f"{_fixed(primary[index], 100, 2):>12} | "
            f"{_fixed(stress[index], 100, 2):>12} | "
            f"{_fixed(price[index], 100, 2):>12}"
        )
    return tuple(lines)


def _polyline_points(
    values: Sequence[int],
    *,
    minimum: int,
    maximum: int,
    left: int,
    top: int,
    width: int,
    height: int,
) -> str:
    count = len(values)
    span = max(1, maximum - minimum)
    points: list[str] = []
    for index, value in enumerate(values):
        x = left if count == 1 else left + index * width // (count - 1)
        y = top + (maximum - value) * height // span
        points.append(f"{x},{y}")
    return " ".join(points)


def _equity_price_svg(evidence: _Evidence) -> str:
    market, primary, stress, price = _chart_series(evidence)
    all_values = primary + stress + price
    minimum = min(all_values)
    maximum = max(all_values)
    padding = max(50, (maximum - minimum) // 12)
    lower = minimum - padding
    upper = maximum + padding
    left, top, width, height = 70, 35, 830, 230
    primary_points = _polyline_points(
        primary,
        minimum=lower,
        maximum=upper,
        left=left,
        top=top,
        width=width,
        height=height,
    )
    stress_points = _polyline_points(
        stress,
        minimum=lower,
        maximum=upper,
        left=left,
        top=top,
        width=width,
        height=height,
    )
    price_points = _polyline_points(
        price,
        minimum=lower,
        maximum=upper,
        left=left,
        top=top,
        width=width,
        height=height,
    )
    grid = []
    for fraction in (0, 1, 2):
        y = top + fraction * height // 2
        value = upper - fraction * (upper - lower) // 2
        grid.append(
            f'<line x1="{left}" y1="{y}" x2="{left + width}" y2="{y}" '
            'stroke="#d7dee8" stroke-width="1"/>'
            f'<text x="{left - 8}" y="{y + 4}" text-anchor="end" '
            'font-size="11" fill="#607080">'
            f"{_fixed(value, 100, 2)}</text>"
        )
    return (
        '<svg class="equity-chart" viewBox="0 0 960 320" '
        'role="img" aria-label="Primary and stress cost profile NAV versus '
        f'{escape(market)} mark price, each normalized to 100">'
        '<rect x="0" y="0" width="960" height="320" fill="#ffffff"/>'
        + "".join(grid)
        + f'<polyline points="{price_points}" fill="none" stroke="#68778d" '
        'stroke-width="3" stroke-linejoin="round"/>'
        f'<polyline points="{primary_points}" fill="none" stroke="#087e8b" '
        'stroke-width="3" stroke-linejoin="round"/>'
        f'<polyline points="{stress_points}" fill="none" stroke="#d95f59" '
        'stroke-width="3" stroke-linejoin="round"/>'
        '<text x="70" y="295" font-size="12" fill="#334155">'
        "All series normalized to 100 at the opening anchor</text>"
        '<line x1="560" y1="291" x2="585" y2="291" stroke="#68778d" '
        'stroke-width="3"/><text x="591" y="295" font-size="11" '
        f'fill="#334155">{escape(market)} mark</text>'
        '<line x1="690" y1="291" x2="715" y2="291" stroke="#087e8b" '
        'stroke-width="3"/><text x="721" y="295" font-size="11" '
        'fill="#334155">Primary NAV</text>'
        '<line x1="815" y1="291" x2="840" y2="291" stroke="#d95f59" '
        'stroke-width="3"/><text x="846" y="295" font-size="11" '
        'fill="#334155">Stress NAV</text>'
        "</svg>"
    )


def _html_profile_table(evidence: _Evidence) -> str:
    rows = "".join(
        "<tr>"
        f"<th scope=\"row\">{escape(label)}</th>"
        f"<td>{escape(primary)}</td>"
        f"<td>{escape(stress)}</td>"
        "</tr>"
        for label, primary, stress in _profile_rows(evidence)
    )
    return (
        '<table><thead><tr><th scope="col">Survival metric</th>'
        '<th scope="col">Primary costs</th>'
        '<th scope="col">Stress 2x costs</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _html_list(lines: Sequence[str]) -> str:
    return "<ul>" + "".join(
        f"<li>{escape(line)}</li>" for line in lines
    ) + "</ul>"


def _html_decisions(evidence: _Evidence) -> str:
    sections: list[str] = []
    for turn, (decision, observation) in enumerate(
        zip(evidence.decisions, evidence.observations, strict=True)
    ):
        bar_index = _as_int(
            decision.get("bar_index"),
            f"decision {turn}.bar_index",
        )
        holding_bar_index = _holding_bar_index(evidence, turn=turn)
        d_nav = _as_int(
            _as_object(
                decision.get("account_after"),
                f"decision {turn}.account_after",
            ).get("d_nav_micro"),
            f"decision {turn}.account_after.d_nav_micro",
        )
        attempts = _attempt_lines(
            evidence,
            decision,
            turn=turn,
            full_text=True,
        )
        event_range = _as_object(
            decision.get("event_seq_range"),
            f"decision {turn}.event_seq_range",
        )
        open_attr = " open" if turn == 0 else ""
        sections.append(
            f"<details{open_attr}>"
            "<summary>"
            f"Turn {turn:04d} · Decision bar {bar_index} → "
            f"NAV bar {holding_bar_index} · "
            f"delta NAV {escape(_money(d_nav, signed=True))}"
            "</summary>"
            '<div class="trace-grid">'
            '<section><h3>Saw</h3>'
            f"<p>{escape(_observation_summary(observation, context=f'observation {turn}'))}</p>"
            "<details><summary>Exact stored observation</summary>"
            f"<pre>{escape(_json_pretty(observation))}</pre></details></section>"
            '<section><h3>Said</h3>'
            + _html_list(attempts)
            + '</section><section><h3>Meant</h3>'
            f"<p>{escape(_meant_line(decision, turn=turn))}</p>"
            f"<pre>{escape(_json_pretty(decision.get('meant')))}</pre></section>"
            '<section><h3>Rules</h3>'
            + _html_list(_rule_lines(decision, turn=turn))
            + '</section><section><h3>Happened</h3>'
            + _html_list(_happened_lines(decision, turn=turn))
            + '</section><section><h3>Cost to hold</h3>'
            + _html_list(_cost_lines(decision, turn=turn))
            + '</section><section class="account"><h3>NAV attribution</h3>'
            f"<p>{escape(_account_line(decision, turn=turn))}</p>"
            f"<p class=\"audit-link\">Evidence events "
            f"{_as_int(event_range.get('first_seq'), 'event_range.first_seq')}"
            "-"
            f"{_as_int(event_range.get('last_seq'), 'event_range.last_seq')}"
            "</p></section></div></details>"
        )
    return "".join(sections)


def _html_report(evidence: _Evidence) -> str:
    fills, cancels = _execution_counts(evidence)
    styles = """
    :root{color-scheme:light;--ink:#102033;--muted:#5b6878;--line:#d9e1ea;
      --paper:#fff;--wash:#f3f6f9;--teal:#087e8b;--red:#b73f3a}
    *{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);
      font:15px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    main{max-width:1120px;margin:0 auto;padding:32px 24px 60px}
    header,.panel,details{background:var(--paper);border:1px solid var(--line);
      border-radius:12px}header{padding:28px;margin-bottom:18px}
    h1{font-size:32px;line-height:1.1;margin:6px 0 8px}h2{margin:0 0 14px;font-size:22px}
    h3{margin:0 0 8px;font-size:14px;text-transform:uppercase;letter-spacing:.06em}
    .label{display:inline-block;padding:4px 9px;border-radius:999px;background:#d9f1f3;
      color:#075d66;font-weight:750}.verdict{font-weight:800;color:var(--teal)}
    .identity{color:var(--muted);overflow-wrap:anywhere}.panel{padding:22px;margin:18px 0}
    .cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
    .card{background:var(--wash);border-radius:9px;padding:14px}.card strong{display:block;font-size:24px}
    table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);
      padding:9px 10px;text-align:right}th:first-child{text-align:left}thead th{background:var(--wash)}
    .equity-chart{width:100%;height:auto;border:1px solid var(--line);border-radius:8px}
    details{margin:10px 0;padding:0 16px}details>summary{cursor:pointer;font-weight:700;padding:14px 0}
    .trace-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:0 0 16px}
    .trace-grid section{background:var(--wash);border-radius:8px;padding:13px;min-width:0}
    .trace-grid .account{grid-column:1/-1;border-left:4px solid var(--teal)}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
    ul{margin:6px 0;padding-left:21px}.audit-link,.fine{color:var(--muted);font-size:13px}
    footer{border-top:1px solid var(--line);margin-top:28px;padding-top:18px;color:var(--muted)}
    @media(max-width:760px){.cards,.trace-grid{grid-template-columns:1fr}.trace-grid .account{grid-column:auto}}
    @media print{body{background:#fff}main{max-width:none;padding:0}details{break-inside:avoid}details>summary{list-style:none}}
    """
    summary_cards = (
        '<div class="cards">'
        f'<div class="card">Verdict<strong>{escape(_display_verdict(evidence))}</strong></div>'
        f'<div class="card">Discipline events<strong>{_discipline_events(evidence)}</strong></div>'
        f'<div class="card">Executed fills<strong>{fills}</strong></div>'
        f'<div class="card">Cancellations<strong>{cancels}</strong></div>'
        "</div>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>WAGMI Bench survival report — {escape(_pack_id(evidence))}</title>"
        f"<style>{styles}</style></head><body><main>"
        "<header>"
        f'<span class="label">claim_label: {CLAIM_LABEL}</span>'
        f"<h1>Survival report: {escape(_pack_id(evidence))}</h1>"
        f'<p class="verdict">{escape(_display_verdict(evidence))}</p>'
        + (
            f'<p class="fine">{escape(FLAT_HOLD_NOTE)}</p>'
            if _is_flat_hold(evidence)
            else ""
        )
        + f'<p class="identity">Agent: {escape(_agent_label(evidence))}<br>'
        f"Run: {escape(_run_id(evidence))}<br>"
        f"Evidence root: {escape(evidence.bundle_root)}</p></header>"
        f'<section class="panel"><h2>Survival-native summary</h2>{summary_cards}</section>'
        '<section class="panel"><h2>Both cost profiles: funding and churn</h2>'
        "<p>Every economic metric is shown under the primary and doubled-cost simulations of the same recorded decisions.</p>"
        f"{_html_profile_table(evidence)}</section>"
        '<section class="panel"><h2>Equity versus price</h2>'
        f"{_equity_price_svg(evidence)}</section>"
        '<section class="panel"><h2>Near-death timeline</h2>'
        f"{_html_list(_near_death_lines(evidence))}</section>"
        '<section class="panel"><h2>Safety event log</h2>'
        f"{_html_list(_safety_event_lines(evidence))}</section>"
        '<section class="panel"><h2>Decision timeline</h2>'
        "<p>Open any turn to trace what the agent saw and said, its parsed intent, every rule verdict, what executed, holding costs, and the exact NAV-change equation.</p>"
        f"{_html_decisions(evidence)}</section>"
        "<footer>"
        f"<p><strong>claim_label: {CLAIM_LABEL}</strong></p>"
        f"<p>{escape(MEMORIZATION_CAVEAT)}</p>"
        f"<p>{escape(EVIDENCE_LIMIT)}</p>"
        f"<p class=\"fine\">Evidence root: {escape(evidence.bundle_root)}</p>"
        "</footer></main></body></html>\n"
    )


def _share_card_svg(evidence: _Evidence) -> str:
    primary = evidence.profiles["primary"]
    stress = evidence.profiles["stress_2x"]
    invariant = evidence.invariant

    def nullable_percent(profile: JsonObject, field: str) -> str:
        value = profile.get(field)
        return _percent(
            None if value is None else _as_int(value, f"profile.{field}")
        )

    caveat_line_1 = (
        "Historical episodes may be recognized from prior knowledge, price "
        "action, or"
    )
    caveat_line_2 = (
        "venue-constant era fingerprinting. Survival/stress evidence only."
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="675" '
        'viewBox="0 0 1200 675" role="img" '
        f'aria-label="WAGMI Bench {_display_verdict(evidence)} survival share card">'
        '<rect width="1200" height="675" rx="32" fill="#0d1b2a"/>'
        '<rect x="42" y="42" width="1116" height="591" rx="24" fill="#12273a" stroke="#29445d"/>'
        '<text x="82" y="100" fill="#76d7df" font-family="system-ui,sans-serif" '
        'font-size="24" font-weight="700">WAGMI BENCH</text>'
        '<rect x="870" y="69" width="248" height="44" rx="22" fill="#163f49"/>'
        f'<text x="994" y="98" text-anchor="middle" fill="#8be2e7" '
        f'font-family="system-ui,sans-serif" font-size="18">claim_label: {CLAIM_LABEL}</text>'
        f'<text x="82" y="180" fill="#f4f8fb" font-family="system-ui,sans-serif" '
        f'font-size="58" font-weight="800">{escape(_display_verdict(evidence))}</text>'
        f'<text x="82" y="220" fill="#a9b8c6" font-family="system-ui,sans-serif" '
        f'font-size="24">{escape(_svg_pack_line(evidence))}</text>'
        '<text x="82" y="282" fill="#dce7ef" font-family="system-ui,sans-serif" '
        'font-size="18" font-weight="700">SURVIVAL-NATIVE READOUT</text>'
        '<rect x="82" y="307" width="500" height="185" rx="14" fill="#0d1b2a"/>'
        '<text x="110" y="343" fill="#76d7df" font-family="system-ui,sans-serif" '
        'font-size="20" font-weight="700">Primary costs</text>'
        f'<text x="110" y="380" fill="#f4f8fb" font-family="system-ui,sans-serif" '
        f'font-size="18">Maximum drawdown  {_percent(_as_int(primary.get("max_drawdown_1e8"), "primary.max_drawdown"))}</text>'
        f'<text x="110" y="414" fill="#f4f8fb" font-family="system-ui,sans-serif" '
        f'font-size="18">Minimum liquidation distance  {nullable_percent(primary, "dist_to_liq_min_1e8")}</text>'
        f'<text x="110" y="448" fill="#f4f8fb" font-family="system-ui,sans-serif" '
        f'font-size="18">Funding flow  {_money(_as_int(primary.get("funding_paid_micro"), "primary.funding"), signed=True)}</text>'
        f'<text x="110" y="480" fill="#f4f8fb" font-family="system-ui,sans-serif" '
        f'font-size="18">Turnover  {_ratio(_as_int(primary.get("turnover_1e8"), "primary.turnover"))}x starting NAV</text>'
        '<rect x="618" y="307" width="500" height="185" rx="14" fill="#0d1b2a"/>'
        '<text x="646" y="343" fill="#f2a6a2" font-family="system-ui,sans-serif" '
        'font-size="20" font-weight="700">Stress 2x costs</text>'
        f'<text x="646" y="380" fill="#f4f8fb" font-family="system-ui,sans-serif" '
        f'font-size="18">Maximum drawdown  {_percent(_as_int(stress.get("max_drawdown_1e8"), "stress.max_drawdown"))}</text>'
        f'<text x="646" y="414" fill="#f4f8fb" font-family="system-ui,sans-serif" '
        f'font-size="18">Minimum liquidation distance  {nullable_percent(stress, "dist_to_liq_min_1e8")}</text>'
        f'<text x="646" y="448" fill="#f4f8fb" font-family="system-ui,sans-serif" '
        f'font-size="18">Funding flow  {_money(_as_int(stress.get("funding_paid_micro"), "stress.funding"), signed=True)}</text>'
        f'<text x="646" y="480" fill="#f4f8fb" font-family="system-ui,sans-serif" '
        f'font-size="18">Turnover  {_ratio(_as_int(stress.get("turnover_1e8"), "stress.turnover"))}x starting NAV</text>'
        f'<text x="82" y="532" fill="#dce7ef" font-family="system-ui,sans-serif" '
        f'font-size="20">Discipline events  {_discipline_events(evidence)}   ·   '
        f'Gate blocks  {_as_int(invariant.get("gate_blocks"), "invariant.gate_blocks")}   ·   '
        f'Blocked egress  {_as_int(invariant.get("egress_blocked_count"), "invariant.egress_blocked_count")}</text>'
        f'<text x="82" y="570" fill="#90a4b7" font-family="system-ui,sans-serif" '
        f'font-size="16">{escape(caveat_line_1)}</text>'
        f'<text x="82" y="595" fill="#90a4b7" font-family="system-ui,sans-serif" '
        f'font-size="16">{escape(caveat_line_2)}</text>'
        f'<text x="82" y="625" fill="#71879a" font-family="ui-monospace,monospace" '
        f'font-size="13">evidence root: {escape(evidence.bundle_root)}</text>'
        "</svg>\n"
    )


def _assert_required_labels(artifacts: ReportArtifacts) -> None:
    for name, artifact in (
        ("terminal report", artifacts.terminal_text),
        ("HTML report", artifacts.html),
        ("share card", artifacts.share_card_svg),
    ):
        if f"claim_label: {CLAIM_LABEL}" not in artifact:
            raise ReportError(f"{name} lost its claim label")
        if "venue-constant era fingerprinting" not in artifact:
            raise ReportError(f"{name} lost the memorization caveat")


def generate_report(bundle_dir: str | Path) -> ReportArtifacts:
    """Render all M5 report surfaces from one verified COMPLETE bundle."""

    evidence = _load_evidence(bundle_dir)
    artifacts = ReportArtifacts(
        terminal_text=_terminal_report(evidence),
        html=_html_report(evidence),
        share_card_svg=_share_card_svg(evidence),
    )
    _assert_required_labels(artifacts)
    return artifacts


def render_terminal_report(bundle_dir: str | Path) -> str:
    """Render the deterministic terminal/text report."""

    return generate_report(bundle_dir).terminal_text


def render_html_report(bundle_dir: str | Path) -> str:
    """Render a self-contained deterministic HTML report."""

    return generate_report(bundle_dir).html


def render_share_card_svg(bundle_dir: str | Path) -> str:
    """Render the deterministic static SVG share card."""

    return generate_report(bundle_dir).share_card_svg


def write_report_files(
    bundle_dir: str | Path,
    output_dir: str | Path,
) -> ReportFiles:
    """Write ``report.txt``, ``report.html``, and ``share-card.svg``.

    The output directory may not be inside the evidence bundle: report output
    is derived material and must never mutate the sealed bundle layout.
    """

    bundle = Path(bundle_dir).resolve()
    target = Path(output_dir).resolve()
    if target.is_relative_to(bundle):
        raise ReportError(
            "report output directory must be outside the sealed bundle"
        )
    artifacts = generate_report(bundle)
    target.mkdir(parents=True, exist_ok=True)
    paths = ReportFiles(
        terminal_text=target / "report.txt",
        html=target / "report.html",
        share_card_svg=target / "share-card.svg",
    )
    for path, content in (
        (paths.terminal_text, artifacts.terminal_text),
        (paths.html, artifacts.html),
        (paths.share_card_svg, artifacts.share_card_svg),
    ):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ReportError(f"refusing non-regular report output: {path.name}")
        path.write_bytes(content.encode("utf-8"))
    return paths
