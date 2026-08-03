# SPDX-License-Identifier: Apache-2.0
"""Deterministic BTC-perpetual episode engine.

The engine is deliberately a pure economic state machine once its explicit
inputs are loaded: pack bytes, :class:`EpisodeConfig`, and agent response
bytes. It does not read wall time, randomness, locale, or environment state.

V1 ships BTC-only scenario packs, but all persisted surfaces retain the
frozen multi-market map shape. This implementation rejects a multi-market
pack loudly until atomic portfolio fill/margin ordering is specified rather
than silently inventing semantics that are absent from the golden oracle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final, Literal, Mapping, cast

from core.action import ActionParseResult, ParsedAction, parse_action
from core.config import EpisodeConfig
from core.math import (
    RATE_SCALE,
    apply_cost_multiplier,
    calculate_fill,
    clamp_funding_rate_1e8,
    distance_to_liquidation_1e8,
    fee_micro,
    floor_fraction,
    funding_cash_flow_micro,
    liquidation_crossed,
    liquidation_penalty_micro,
    liquidation_price_ticks,
    maintenance_margin_micro,
    margin_used_micro,
    notional_micro,
    raw_target_quantity_base_1e8,
    realized_pnl_micro,
    target_quantity_base_1e8,
    unrealized_pnl_micro,
)
from core.metrics import (
    UndefinedMetricError,
    bar_returns,
    cvar_5_1e8,
    distance_statistics,
    max_drawdown_1e8,
    net_return_1e8,
    sortino_1e8,
    turnover_1e8,
)
from core.observation import (
    AccountState,
    PositionState,
    RiskState,
    build_observation,
)
from core.pack import (
    FundingRow,
    MarketData,
    MarketSpec,
    PackData,
    load_pack,
)
from harness.protocol import (
    AgentReply,
    DecisionTimeout,
    HarnessEvent,
    HarnessEventSource,
    InProcessAgent,
)
from spec.canonical import canonical_bytes, sha256_prefixed

JsonObject = dict[str, object]

ENGINE_VERSION: Final = "0.1.0"
NEAR_LIQ_THRESHOLD_1E8: Final = 5_000_000
DETAIL_INVALID_JSON: Final = "response is not a single valid JSON document"
MAX_IJSON_INT: Final = 2**53 - 1
GOLDEN_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "ActionParsed",
        "ActionRejected",
        "RiskCheck",
        "OrderFilled",
        "OrderCancelled",
        "FundingApplied",
        "NearLiquidation",
        "LiquidationTriggered",
        "EpisodeEnd",
    }
)


class EngineError(RuntimeError):
    """The explicit episode inputs cannot be simulated."""


@dataclass(frozen=True, slots=True)
class EpisodeResult:
    """All deterministic runtime products needed by M1 and the M2 recorder."""

    events: list[JsonObject]
    observations: list[JsonObject]
    raw_blobs: Mapping[str, bytes]
    ledger_primary: list[JsonObject]
    ledger_stress_2x: list[JsonObject]
    metrics: JsonObject
    pack: PackData
    config: EpisodeConfig
    run_id: str
    episode_id: str

    def golden_event_projection(self) -> list[JsonObject]:
        """Return the fixture-defined economic projection, renumbered.

        The frozen golden ``events.jsonl`` intentionally omits lifecycle and
        margin events for which the M0 fixture has no raw/observation blobs.
        See ``fixtures/golden-mini/scenario.md``.
        """

        projected: list[JsonObject] = []
        for event in self.events:
            event_type = event.get("type")
            if event_type not in GOLDEN_EVENT_TYPES:
                continue
            copy = dict(event)
            # The frozen C0 economic projection timestamps wick-derived
            # events at the triggering bar's open because the hand oracle has
            # no lifecycle emission clock.  The full C2 stream emits them at
            # the bar close, when the complete high/low is actually known.
            if event_type in {"NearLiquidation", "LiquidationTriggered"}:
                bar_index = copy.get("bar_index")
                if isinstance(bar_index, int) and not isinstance(
                    bar_index, bool
                ):
                    alias = self.pack.market_aliases[0]
                    absolute_index = self.pack.warmup_bars + bar_index
                    copy["ts"] = self.pack.market(alias).trade[
                        absolute_index
                    ].ts
            copy["seq"] = len(projected)
            projected.append(copy)
        return projected


@dataclass(slots=True)
class _Account:
    position_qty_base_1e8: int = 0
    entry_px_ticks: int = 0
    target_leverage_1e4: int = 0
    cash_micro: int = 0
    fees_cum_micro: int = 0
    funding_cum_micro: int = 0
    realized_price_cum_micro: int = 0
    liquidation_penalty_cum_micro: int = 0
    turnover_notional_micro: int = 0
    fill_cost_cum_micro: int = 0
    kill_switch_active: bool = False


@dataclass(frozen=True, slots=True)
class _Snapshot:
    bar_index: int
    absolute_bar_index: int
    close_ts: int
    position_qty_base_1e8: int
    entry_px_ticks: int | None
    mark_close_ticks: int
    upnl_micro: int
    cash_micro: int
    nav_micro: int
    fees_cum_micro: int
    funding_cum_micro: int
    realized_price_cum_micro: int
    liquidation_penalty_cum_micro: int
    margin_micro: int
    maintenance_margin_micro: int
    liquidation_px_ticks: int | None
    distance_to_liquidation_1e8: int | None
    min_intrabar_distance_1e8: int | None


@dataclass(frozen=True, slots=True)
class _TurnDecision:
    responses: tuple[AgentReply, ...]
    harness_events: tuple[HarnessEvent, ...]
    parsed: ParsedAction | None
    rejection: JsonObject | None
    raw_sha256: str | None


@dataclass(slots=True)
class _Counters:
    turns: int = 0
    invalid_actions: int = 0
    missed_decisions: int = 0
    gate_blocks: int = 0
    post_kill_switch_attempts: int = 0
    liquidations: int = 0
    kill_switch_fired: bool = False


@dataclass(frozen=True, slots=True)
class _Totals:
    reason: str
    final_turn: int
    final_nav_micro: int
    turnover_notional_micro: int
    fill_cost_cum_micro: int
    fees_cum_micro: int
    funding_cum_micro: int
    liquidation_penalty_cum_micro: int


@dataclass(slots=True)
class _ProfileRun:
    snapshots: list[_Snapshot]
    events: list[JsonObject]
    decisions: list[_TurnDecision]
    observations: list[JsonObject]
    raw_blobs: dict[str, bytes]
    counters: _Counters
    totals: _Totals | None = None


def _emit(
    events: list[JsonObject],
    *,
    event_type: str,
    payload: JsonObject,
    ts: int,
    turn: int | None,
    bar_index: int | None,
    source: str = "engine",
) -> int:
    seq = len(events)
    events.append(
        {
            "schema": "event/v1",
            "seq": seq,
            "ts": ts,
            "turn": turn,
            "bar_index": bar_index,
            "source": source,
            "type": event_type,
            "payload": payload,
        }
    )
    return seq


def _drain_harness_events(agent: InProcessAgent) -> tuple[HarnessEvent, ...]:
    """Drain and validate an adapter's optional trusted-harness side channel."""

    if not isinstance(agent, HarnessEventSource):
        return ()
    try:
        drained = agent.drain_harness_events()
    except Exception as exc:
        raise EngineError(
            "trusted harness event drain failed: "
            f"{type(exc).__name__}"
        ) from exc
    if not isinstance(drained, tuple):
        raise EngineError("drain_harness_events must return a tuple")
    validated: list[HarnessEvent] = []
    for index, event in enumerate(drained):
        if not isinstance(event, HarnessEvent):
            raise EngineError(
                "drain_harness_events returned a non-HarnessEvent at "
                f"index {index}"
            )
        if event.type != "EgressBlocked":
            raise EngineError(
                f"unsupported harness event type {event.type!r}"
            )
        payload = event.payload
        if set(payload) != {"destination", "port", "protocol", "count"}:
            raise EngineError(
                "EgressBlocked payload must contain exactly destination, "
                "port, protocol, count"
            )
        destination = payload.get("destination")
        port = payload.get("port")
        protocol = payload.get("protocol")
        count = payload.get("count")
        if not isinstance(destination, str):
            raise EngineError("EgressBlocked.destination must be a string")
        if port is not None and (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 0 <= port <= 65_535
        ):
            raise EngineError(
                "EgressBlocked.port must be null or an integer from 0 to 65535"
            )
        if protocol not in {"https", "dns", "tcp", "udp", "other"}:
            raise EngineError("EgressBlocked.protocol is unsupported")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 1 <= count <= MAX_IJSON_INT
        ):
            raise EngineError(
                "EgressBlocked.count must be a positive I-JSON-safe integer"
            )
        validated.append(
            HarnessEvent(
                type="EgressBlocked",
                payload={
                    "destination": destination,
                    "port": port,
                    "protocol": protocol,
                    "count": count,
                },
            )
        )
    return tuple(validated)


def _coalesce_harness_events(
    events: tuple[HarnessEvent, ...],
) -> tuple[HarnessEvent, ...]:
    """Coalesce identical block facts and impose a stable payload order."""

    counts: dict[tuple[str, int | None, str], int] = {}
    for event in events:
        payload = event.payload
        key = (
            cast(str, payload["destination"]),
            cast(int | None, payload["port"]),
            cast(str, payload["protocol"]),
        )
        count = cast(int, payload["count"])
        combined = counts.get(key, 0) + count
        if combined > MAX_IJSON_INT:
            raise EngineError(
                "coalesced EgressBlocked.count exceeds the I-JSON safe range"
            )
        counts[key] = combined
    ordered = sorted(
        counts,
        key=lambda key: (
            key[0],
            -1 if key[1] is None else key[1],
            key[2],
        ),
    )
    return tuple(
        HarnessEvent(
            type="EgressBlocked",
            payload={
                "destination": destination,
                "port": port,
                "protocol": protocol,
                "count": counts[(destination, port, protocol)],
            },
        )
        for destination, port, protocol in ordered
    )


def _emit_harness_events(
    events: list[JsonObject],
    drained: tuple[HarnessEvent, ...],
    *,
    ts: int,
    turn: int | None,
    bar_index: int | None,
) -> None:
    for event in _coalesce_harness_events(drained):
        _emit(
            events,
            event_type=event.type,
            payload=dict(event.payload),
            ts=ts,
            turn=turn,
            bar_index=bar_index,
            source="harness",
        )


def _sign(value: int) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _maintenance_rate(spec: MarketSpec, notional: int) -> int:
    for tier in spec.margin_tiers:
        if notional <= tier.notional_cap_micro:
            return tier.maintenance_rate_1e8
    return spec.margin_tiers[-1].maintenance_rate_1e8


def _upnl(account: _Account, mark_ticks: int, spec: MarketSpec) -> int:
    if account.position_qty_base_1e8 == 0:
        return 0
    return unrealized_pnl_micro(
        account.position_qty_base_1e8,
        account.entry_px_ticks,
        mark_ticks,
        spec.tick_size_micro,
    )


def _snapshot(
    *,
    account: _Account,
    market: MarketData,
    absolute_bar_index: int,
    relative_bar_index: int,
    close_ts: int | None = None,
) -> _Snapshot:
    mark = market.mark[absolute_bar_index]
    upnl = _upnl(account, mark.c, market.spec)
    liquidation: int | None = None
    distance: int | None = None
    min_distance: int | None = None
    margin = 0
    maintenance = 0
    entry: int | None = None
    if account.position_qty_base_1e8 != 0:
        entry = account.entry_px_ticks
        position_sign = _sign(account.position_qty_base_1e8)
        mark_notional = notional_micro(
            account.position_qty_base_1e8,
            mark.c,
            market.spec.tick_size_micro,
        )
        maintenance_rate = _maintenance_rate(market.spec, mark_notional)
        liquidation = liquidation_price_ticks(
            account.entry_px_ticks,
            position_sign,
            account.target_leverage_1e4,
            maintenance_rate,
        )
        distance = distance_to_liquidation_1e8(
            mark.c,
            liquidation,
            position_sign,
        )
        adverse = mark.l if position_sign > 0 else mark.h
        min_distance = distance_to_liquidation_1e8(
            adverse,
            liquidation,
            position_sign,
        )
        margin = margin_used_micro(
            account.position_qty_base_1e8,
            account.entry_px_ticks,
            market.spec.tick_size_micro,
            account.target_leverage_1e4,
        )
        maintenance = maintenance_margin_micro(
            account.position_qty_base_1e8,
            mark.c,
            market.spec.tick_size_micro,
            maintenance_rate,
        )
    return _Snapshot(
        bar_index=relative_bar_index,
        absolute_bar_index=absolute_bar_index,
        close_ts=(
            market.trade[absolute_bar_index].available_at
            if close_ts is None
            else close_ts
        ),
        position_qty_base_1e8=account.position_qty_base_1e8,
        entry_px_ticks=entry,
        mark_close_ticks=mark.c,
        upnl_micro=upnl,
        cash_micro=account.cash_micro,
        nav_micro=account.cash_micro + upnl,
        fees_cum_micro=account.fees_cum_micro,
        funding_cum_micro=account.funding_cum_micro,
        realized_price_cum_micro=account.realized_price_cum_micro,
        liquidation_penalty_cum_micro=account.liquidation_penalty_cum_micro,
        margin_micro=margin,
        maintenance_margin_micro=maintenance,
        liquidation_px_ticks=liquidation,
        distance_to_liquidation_1e8=distance,
        min_intrabar_distance_1e8=min_distance,
    )


def _drawdown_1e8(snapshots: list[_Snapshot]) -> int:
    peak = max(row.nav_micro for row in snapshots)
    if peak <= 0:
        raise EngineError("episode NAV has no positive peak")
    return floor_fraction(
        Fraction((peak - snapshots[-1].nav_micro) * RATE_SCALE, peak)
    )


def _realized_pnl(account: _Account) -> int:
    return (
        account.realized_price_cum_micro
        - account.fees_cum_micro
        - account.funding_cum_micro
        - account.liquidation_penalty_cum_micro
    )


def _position_for_observation(
    account: _Account,
    market: MarketData,
    snapshot: _Snapshot,
) -> PositionState:
    if account.position_qty_base_1e8 == 0:
        return PositionState()
    return PositionState(
        qty_base_1e8=account.position_qty_base_1e8,
        entry_px_ticks=account.entry_px_ticks,
        margin_micro=snapshot.margin_micro,
        liq_px_ticks=snapshot.liquidation_px_ticks,
    )


def _parse_detail(reason: str) -> str:
    details = {
        "oversize": "response exceeds 65536 bytes or is not valid UTF-8",
        "invalid_json": DETAIL_INVALID_JSON,
        "unknown_schema": "response schema is missing or not action/v1",
        "schema_invalid": "response does not validate against action/v1",
        "unknown_field": "response contains an unknown top-level field",
        "unknown_market": "target names a market not declared by the pack",
        "invalid_target_format": "target leverage must be a decimal string or integer",
        "float_target": "fractional JSON targets are forbidden; send a decimal string",
        "target_out_of_range": "target leverage exceeds the structural sanity bound",
        "invalid_slippage": "max_slippage_bps must be an integer from 0 through 10000",
    }
    return details.get(reason, reason)


def _extract_token_usage(raw: bytes) -> JsonObject | None:
    try:
        # Telemetry shares the agent's untrusted response body.  Preserve the
        # core-path no-float invariant even when a response contains a
        # fractional or non-finite JSON number outside the action payload.
        value = json.loads(raw, parse_float=str, parse_constant=str)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or input_tokens < 0
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 0
    ):
        # action/v1 treats usage as advisory telemetry and permits either
        # member to be absent.  AgentResponded/v1 is stricter: its token_usage
        # object is either complete or null.
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _applied_funding(
    account: _Account,
    market: MarketData,
    funding_row: FundingRow,
) -> tuple[int, int, int]:
    """Apply one venue settlement and return rate, index price, cash flow."""

    applied_rate = clamp_funding_rate_1e8(
        funding_row.rate_1e8,
        market.spec.funding.floor_1e8,
        market.spec.funding.cap_1e8,
    )
    index_px = next(
        (row.o for row in market.index if row.ts == funding_row.ts),
        None,
    )
    if index_px is None:
        raise EngineError(
            "funding settlement has no exact index-price row: "
            f"market={market.spec.alias} ts={funding_row.ts}"
        )
    cash_flow = funding_cash_flow_micro(
        account.position_qty_base_1e8,
        index_px,
        market.spec.tick_size_micro,
        applied_rate,
    )
    account.cash_micro += cash_flow
    account.funding_cum_micro -= cash_flow
    return applied_rate, index_px, cash_flow


def _request_decision(
    *,
    agent: InProcessAgent,
    observation: JsonObject,
    config: EpisodeConfig,
    market_aliases: tuple[str, ...],
) -> _TurnDecision:
    responses: list[AgentReply] = []
    harness_events: list[HarnessEvent] = []
    first_failure: ActionParseResult | None = None
    first_raw_sha: str | None = None
    for attempt in (1, 2):
        retry: JsonObject | None = None
        if attempt == 2:
            if first_failure is None or first_failure.reason is None or first_raw_sha is None:
                raise EngineError("retry requested without a first parse failure")
            retry = {
                "reason": first_failure.reason,
                "detail": _parse_detail(first_failure.reason),
                "prior_raw_sha256": first_raw_sha,
            }
        request: JsonObject = {
            "schema": "runner_request/v1",
            "attempt": attempt,
            "observation": observation,
            "retry": retry,
        }
        try:
            reply = agent.decide(request)
        except DecisionTimeout:
            harness_events.extend(_drain_harness_events(agent))
            prior_error = (
                None
                if first_failure is None or first_failure.reason is None
                else (
                    f"{first_failure.reason}: "
                    f"{_parse_detail(first_failure.reason)}"
                )
            )
            return _TurnDecision(
                responses=tuple(responses),
                harness_events=tuple(harness_events),
                parsed=None,
                rejection={
                    "reason": "timeout",
                    "detail": (
                        "no response bytes within response_deadline_ms="
                        f"{config.response_deadline_ms}"
                    ),
                    "validator_error": prior_error,
                    "attempts": attempt,
                },
                raw_sha256=first_raw_sha,
            )
        except Exception as exc:
            harness_events.extend(_drain_harness_events(agent))
            prior_error = (
                None
                if first_failure is None or first_failure.reason is None
                else (
                    f"{first_failure.reason}: "
                    f"{_parse_detail(first_failure.reason)}"
                )
            )
            return _TurnDecision(
                responses=tuple(responses),
                harness_events=tuple(harness_events),
                parsed=None,
                rejection={
                    "reason": "agent_error",
                    "detail": f"agent transport failed: {type(exc).__name__}",
                    "validator_error": prior_error,
                    "attempts": attempt,
                },
                raw_sha256=first_raw_sha,
            )

        responses.append(reply)
        harness_events.extend(_drain_harness_events(agent))
        raw_sha = sha256_prefixed(reply.body)
        if (
            reply.http_status is not None
            and 400 <= reply.http_status <= 499
        ):
            # IC-6 says any HTTP 4xx is malformed even if the response body
            # happens to be a valid action/v1 document.  The body remains
            # verbatim evidence in AgentResponded/raw; interpretation alone
            # is forced through the existing frozen malformed retry path.
            result = ActionParseResult(action=None, reason="schema_invalid")
        else:
            result = parse_action(
                reply.body,
                market_aliases,
                from_attempt=attempt,
            )
        if result.accepted:
            return _TurnDecision(
                responses=tuple(responses),
                harness_events=tuple(harness_events),
                parsed=result.action,
                rejection=None,
                raw_sha256=raw_sha,
            )
        if attempt == 1 and config.parse_failure_retries:
            first_failure = result
            first_raw_sha = raw_sha
            continue
        reason = cast(str, result.reason)
        detail = _parse_detail(reason)
        first_reason = (
            cast(str, first_failure.reason)
            if first_failure is not None and first_failure.reason is not None
            else reason
        )
        return _TurnDecision(
            responses=tuple(responses),
            harness_events=tuple(harness_events),
            parsed=None,
            rejection={
                "reason": reason,
                "detail": f"attempt {attempt}: {detail}" if attempt == 2 else detail,
                "validator_error": (
                    None
                    if attempt == 1
                    else f"{first_reason}: {_parse_detail(first_reason)}"
                ),
                "attempts": attempt,
            },
            raw_sha256=raw_sha,
        )
    raise EngineError("unreachable agent-attempt state")


def _margin_payload(
    account: _Account,
    market: MarketData,
    absolute_bar_index: int,
) -> JsonObject:
    mark = market.mark[absolute_bar_index]
    if account.position_qty_base_1e8 == 0:
        return {
            "market": market.spec.alias,
            "mark_px_ticks": mark.c,
            "mark_high_ticks": mark.h,
            "mark_low_ticks": mark.l,
            "position_qty_base_1e8": 0,
            "entry_px_ticks": None,
            "margin_micro": 0,
            "maintenance_margin_micro": 0,
            "upnl_micro": 0,
            "liq_px_ticks": None,
            "dist_to_liq_1e8": None,
            "min_intrabar_dist_to_liq_1e8": None,
        }
    snapshot = _snapshot(
        account=account,
        market=market,
        absolute_bar_index=absolute_bar_index,
        relative_bar_index=0,
    )
    return {
        "market": market.spec.alias,
        "mark_px_ticks": mark.c,
        "mark_high_ticks": mark.h,
        "mark_low_ticks": mark.l,
        "position_qty_base_1e8": account.position_qty_base_1e8,
        "entry_px_ticks": account.entry_px_ticks,
        "margin_micro": snapshot.margin_micro,
        "maintenance_margin_micro": snapshot.maintenance_margin_micro,
        "upnl_micro": snapshot.upnl_micro,
        "liq_px_ticks": snapshot.liquidation_px_ticks,
        "dist_to_liq_1e8": snapshot.distance_to_liquidation_1e8,
        "min_intrabar_dist_to_liq_1e8": snapshot.min_intrabar_distance_1e8,
    }


def _apply_position_fill(
    *,
    account: _Account,
    signed_fill_qty: int,
    fill_px_ticks: int,
    requested_target_leverage_1e4: int,
    fee: int,
    fill_notional: int,
    reference_notional: int,
    request_fully_filled: bool,
    spec: MarketSpec,
) -> None:
    old_position = account.position_qty_base_1e8
    new_position = old_position + signed_fill_qty
    realized = 0
    if old_position != 0 and (
        new_position == 0
        or _sign(new_position) != _sign(old_position)
        or abs(new_position) < abs(old_position)
    ):
        closed = min(abs(old_position), abs(signed_fill_qty))
        realized = realized_pnl_micro(
            _sign(old_position),
            closed,
            account.entry_px_ticks,
            fill_px_ticks,
            spec.tick_size_micro,
        )

    account.cash_micro += realized - fee
    account.fees_cum_micro += fee
    account.realized_price_cum_micro += realized
    account.turnover_notional_micro += fill_notional
    account.fill_cost_cum_micro += abs(fill_notional - reference_notional)

    if new_position == 0:
        account.position_qty_base_1e8 = 0
        account.entry_px_ticks = 0
        account.target_leverage_1e4 = 0
        return
    if old_position == 0 or _sign(new_position) != _sign(old_position):
        account.position_qty_base_1e8 = new_position
        account.entry_px_ticks = fill_px_ticks
        account.target_leverage_1e4 = abs(requested_target_leverage_1e4)
        return
    if abs(new_position) > abs(old_position):
        numerator = (
            account.entry_px_ticks * abs(old_position)
            + fill_px_ticks * abs(signed_fill_qty)
        )
        account.entry_px_ticks = numerator // abs(new_position)
    account.position_qty_base_1e8 = new_position
    if abs(new_position) > abs(old_position) or request_fully_filled:
        account.target_leverage_1e4 = abs(requested_target_leverage_1e4)


def _run_profile(
    *,
    pack: PackData,
    config: EpisodeConfig,
    profile: str,
    run_id: str,
    episode_id: str,
    agent: InProcessAgent | None,
    replay_decisions: list[_TurnDecision] | None,
    collect_evidence: bool,
) -> _ProfileRun:
    if len(pack.market_aliases) != 1:
        raise EngineError("V1 engine supports exactly one market per scenario pack")
    alias = pack.market_aliases[0]
    market = pack.market(alias)
    multiplier = market.spec.cost_profile_multipliers_1e4.get(profile)
    if multiplier is None:
        raise EngineError(f"pack does not declare cost profile {profile!r}")

    account = _Account(cash_micro=config.starting_nav_micro)
    run = _ProfileRun(
        snapshots=[],
        events=[],
        decisions=[],
        observations=[],
        raw_blobs={},
        counters=_Counters(),
    )
    start_abs = pack.warmup_bars
    run.snapshots.append(
        _snapshot(
            account=account,
            market=market,
            absolute_bar_index=start_abs,
            relative_bar_index=0,
            close_ts=pack.clock_real_ts(0),
        )
    )
    terminal = False
    last_turn = -1

    for turn in range(pack.bars_total):
        if terminal:
            break
        decision_abs = start_abs + turn
        fill_abs = decision_abs + 1
        decision_ts = pack.clock_real_ts(turn)
        current_snapshot = run.snapshots[-1]
        drawdown_used = _drawdown_1e8(run.snapshots)
        turnover_used = turnover_1e8(
            account.turnover_notional_micro,
            config.starting_nav_micro,
        )

        if replay_decisions is None:
            if agent is None:
                raise EngineError("primary simulation requires an agent")
            free_cash = account.cash_micro - current_snapshot.margin_micro
            observation_build = build_observation(
                pack,
                config,
                episode_id=episode_id,
                turn=turn,
                account=AccountState(
                    cash_micro=free_cash,
                    realized_pnl_micro=_realized_pnl(account),
                ),
                positions={
                    alias: _position_for_observation(
                        account,
                        market,
                        current_snapshot,
                    )
                },
                risk=RiskState(
                    drawdown_used_1e8=drawdown_used,
                    turnover_used_1e8=turnover_used,
                    kill_switch_active=account.kill_switch_active,
                ),
            )
            observation = observation_build.document
            decision = _request_decision(
                agent=agent,
                observation=observation,
                config=config,
                market_aliases=pack.market_aliases,
            )
            run.decisions.append(decision)
            if collect_evidence:
                run.observations.append(observation)
                observation_ref = f"observations/{turn:04d}.json"
                _emit(
                    run.events,
                    event_type="ObservationEmitted",
                    payload={
                        "observation_sha256": sha256_prefixed(
                            canonical_bytes(observation)
                        ),
                        "observation_ref": observation_ref,
                    },
                    ts=decision_ts,
                    turn=turn,
                    bar_index=turn,
                )
                for attempt, reply in enumerate(decision.responses, 1):
                    raw_ref = f"raw/{turn:04d}-a{attempt}.txt"
                    run.raw_blobs[raw_ref] = reply.body
                    _emit(
                        run.events,
                        event_type="AgentResponded",
                        payload={
                            "attempt": attempt,
                            "raw_ref": raw_ref,
                            "raw_sha256": sha256_prefixed(reply.body),
                            "raw_bytes": len(reply.body),
                            "latency_ms": reply.latency_ms,
                            "http_status": reply.http_status,
                            "token_usage": _extract_token_usage(reply.body),
                            "transport": reply.transport,
                        },
                        ts=decision_ts,
                        turn=turn,
                        bar_index=turn,
                        source="harness",
                    )
                _emit_harness_events(
                    run.events,
                    decision.harness_events,
                    ts=decision_ts,
                    turn=turn,
                    bar_index=turn,
                )
        else:
            if turn >= len(replay_decisions):
                raise EngineError("recorded decision trace ended before the episode")
            decision = replay_decisions[turn]

        run.counters.turns += 1
        if decision.parsed is not None:
            if collect_evidence:
                _emit(
                    run.events,
                    event_type="ActionParsed",
                    payload=decision.parsed.to_mapping(),
                    ts=decision_ts,
                    turn=turn,
                    bar_index=turn,
                )
        else:
            rejection = decision.rejection
            if rejection is None:
                raise EngineError("decision has neither parsed action nor rejection")
            reason = rejection.get("reason")
            if reason in {"timeout", "agent_error"}:
                run.counters.missed_decisions += 1
            else:
                run.counters.invalid_actions += 1
            if collect_evidence:
                _emit(
                    run.events,
                    event_type="ActionRejected",
                    payload=rejection,
                    ts=decision_ts,
                    turn=turn,
                    bar_index=turn,
                )

        # A settlement exactly at the fill instant applies to the position
        # carried into that instant, before gates and sizing.
        funding_row = next(
            (row for row in market.funding if row.ts == decision_ts),
            None,
        )
        if funding_row is not None:
            applied_rate, index_px, cash_flow = _applied_funding(
                account,
                market,
                funding_row,
            )
            if collect_evidence:
                _emit(
                    run.events,
                    event_type="FundingApplied",
                    payload={
                        "market": alias,
                        "settlement_ts": decision_ts,
                        "rate_1e8": applied_rate,
                        "index_px_ticks": index_px,
                        "position_qty_base_1e8": account.position_qty_base_1e8,
                        "amount_micro": cash_flow,
                    },
                    ts=decision_ts,
                    turn=turn,
                    bar_index=turn + 1,
                )

        parsed = decision.parsed
        if parsed is not None:
            target_lev = parsed.target_lev_1e4.get(alias, 0)
            gross_target = sum(abs(value) for value in parsed.target_lev_1e4.values())
            nav_at_decision = account.cash_micro + _upnl(
                account,
                market.mark[decision_abs].c,
                market.spec,
            )
            requested_target_qty = target_quantity_base_1e8(
                target_lev,
                nav_at_decision,
                market.trade[fill_abs].o,
                market.spec.tick_size_micro,
                market.spec.qty_step_base_1e8,
            )
            raw_target_qty = raw_target_quantity_base_1e8(
                target_lev,
                nav_at_decision,
                market.trade[fill_abs].o,
                market.spec.tick_size_micro,
            )
            requested_delta = requested_target_qty - account.position_qty_base_1e8
            raw_requested_delta = (
                raw_target_qty - account.position_qty_base_1e8
            )
            projected_notional = notional_micro(
                requested_delta,
                market.trade[fill_abs].o,
                market.spec.tick_size_micro,
            )
            projected_turnover = turnover_1e8(
                account.turnover_notional_micro + projected_notional,
                config.starting_nav_micro,
            )

            checks: list[tuple[str, str, str, int, int, str, bool]] = [
                (
                    f"lev-{alias}",
                    "leverage_cap_market",
                    alias,
                    abs(target_lev),
                    market.spec.leverage_cap_lev_1e4,
                    "lev_1e4",
                    abs(target_lev) <= market.spec.leverage_cap_lev_1e4,
                ),
                (
                    "lev-gross",
                    "leverage_cap_gross",
                    "account",
                    gross_target,
                    config.leverage_cap_gross_lev_1e4,
                    "lev_1e4",
                    gross_target <= config.leverage_cap_gross_lev_1e4,
                ),
            ]
            if config.turnover_cap_1e8 is not None:
                checks.append(
                    (
                        "turnover",
                        "turnover_cap",
                        "account",
                        projected_turnover,
                        config.turnover_cap_1e8,
                        "1e8",
                        projected_turnover <= config.turnover_cap_1e8,
                    )
                )
            kill_pass = not account.kill_switch_active or target_lev == 0
            checks.append(
                (
                    "drawdown-ks",
                    "drawdown_kill_switch",
                    "account",
                    drawdown_used,
                    config.drawdown_kill_switch_1e8,
                    "1e8",
                    kill_pass,
                )
            )
            blocked = False
            for constraint_id, constraint_type, scope, observed, limit, unit, passed in checks:
                if not passed:
                    blocked = True
                    run.counters.gate_blocks += 1
                if collect_evidence:
                    _emit(
                        run.events,
                        event_type="RiskCheck",
                        payload={
                            "constraint_id": constraint_id,
                            "constraint_type": constraint_type,
                            "scope": scope,
                            "observed": observed,
                            "limit": limit,
                            "unit": unit,
                            "verdict": "pass" if passed else "block",
                        },
                        ts=decision_ts,
                        turn=turn,
                        bar_index=turn,
                    )
            if blocked and account.kill_switch_active and target_lev != 0:
                run.counters.post_kill_switch_attempts += 1
                if collect_evidence:
                    _emit(
                        run.events,
                        event_type="PostKillSwitchAttempt",
                        payload={
                            "target_lev_1e4": dict(parsed.target_lev_1e4),
                            "raw_sha256": decision.raw_sha256,
                        },
                        ts=decision_ts,
                        turn=turn,
                        bar_index=turn,
                    )
            if not blocked and requested_delta != 0:
                side: Literal["buy", "sell"] = (
                    "buy" if requested_delta > 0 else "sell"
                )
                fill = calculate_fill(
                    side=side,
                    requested_qty_base_1e8=abs(requested_delta),
                    bar_volume_base_1e8=market.trade[fill_abs].v_base_1e8,
                    participation_cap_1e8=market.spec.participation_cap_1e8,
                    qty_step_base_1e8=market.spec.qty_step_base_1e8,
                    ref_px_ticks=market.trade[fill_abs].o,
                    tick_size_micro=market.spec.tick_size_micro,
                    half_spread_1e8=market.spec.half_spread_1e8,
                    impact_coeff_1e8=market.spec.impact_coeff_1e8,
                    impact_model=market.spec.impact_model,
                    taker_fee_rate_1e8=market.spec.taker_fee_rate_1e8,
                    cost_multiplier_1e4=multiplier,
                )
                cancel_reason: str | None = None
                if fill.quantities.filled_qty_base_1e8 == 0:
                    cancel_reason = "participation_cap"
                elif fill.notional_micro < market.spec.min_notional_micro:
                    cancel_reason = "min_notional"
                elif (
                    parsed.max_slippage_bps is not None
                    and abs(fill.slippage_1e8)
                    > parsed.max_slippage_bps * 10_000
                ):
                    cancel_reason = "max_slippage_exceeded"

                if cancel_reason is not None:
                    if collect_evidence:
                        _emit(
                            run.events,
                            event_type="OrderCancelled",
                            payload={
                                "market": alias,
                                "reason": cancel_reason,
                                "requested_qty_base_1e8": abs(requested_delta),
                                "cancelled_qty_base_1e8": abs(requested_delta),
                                "detail": (
                                    f"{cancel_reason}: requested_qty "
                                    f"{abs(requested_delta)} not executed"
                                ),
                            },
                            ts=decision_ts,
                            turn=turn,
                            bar_index=turn + 1,
                        )
                else:
                    filled_qty = fill.quantities.filled_qty_base_1e8
                    signed_fill = filled_qty if side == "buy" else -filled_qty
                    _apply_position_fill(
                        account=account,
                        signed_fill_qty=signed_fill,
                        fill_px_ticks=fill.fill_px_ticks,
                        requested_target_leverage_1e4=target_lev,
                        fee=fill.fee_micro,
                        fill_notional=fill.notional_micro,
                        reference_notional=fill.reference_notional_micro,
                        request_fully_filled=(
                            fill.quantities.cancelled_qty_base_1e8 == 0
                        ),
                        spec=market.spec,
                    )
                    if collect_evidence:
                        _emit(
                            run.events,
                            event_type="OrderFilled",
                            payload={
                                "market": alias,
                                "side": side,
                                "requested_qty_base_1e8": abs(requested_delta),
                                "qty_base_1e8": filled_qty,
                                "ref_open_px_ticks": fill.ref_open_px_ticks,
                                "half_spread_ticks": fill.half_spread_ticks,
                                "impact_ticks": fill.impact_ticks,
                                "fill_px_ticks": fill.fill_px_ticks,
                                "notional_micro": fill.notional_micro,
                                "fee_micro": fill.fee_micro,
                                "slippage_1e8": fill.slippage_1e8,
                                "cost_profile": "primary",
                            },
                            ts=decision_ts,
                            turn=turn,
                            bar_index=turn + 1,
                        )
                    if fill.quantities.cancelled_qty_base_1e8:
                        cap_qty = fill.quantities.capacity_qty_base_1e8
                        cancelled = fill.quantities.cancelled_qty_base_1e8
                        if collect_evidence:
                            _emit(
                                run.events,
                                event_type="OrderCancelled",
                                payload={
                                    "market": alias,
                                    "reason": "participation_cap",
                                    "requested_qty_base_1e8": abs(requested_delta),
                                    "cancelled_qty_base_1e8": cancelled,
                                    "detail": (
                                        "participation_cap: requested_qty "
                                        f"{abs(requested_delta)} > cap_qty {cap_qty} "
                                        "(participation_cap_1e8 "
                                        f"{market.spec.participation_cap_1e8} x fill-bar "
                                        f"volume {market.trade[fill_abs].v_base_1e8}, "
                                        "floored to qty_step "
                                        f"{market.spec.qty_step_base_1e8})"
                                    ),
                                },
                                ts=decision_ts,
                                turn=turn,
                                bar_index=turn + 1,
                            )
            elif not blocked and requested_delta == 0:
                if raw_requested_delta != 0 and collect_evidence:
                    _emit(
                        run.events,
                        event_type="OrderCancelled",
                        payload={
                            "market": alias,
                            "reason": "qty_rounding",
                            "requested_qty_base_1e8": abs(
                                raw_requested_delta
                            ),
                            "cancelled_qty_base_1e8": abs(
                                raw_requested_delta
                            ),
                            "detail": (
                                "qty_rounding: raw target delta "
                                f"{abs(raw_requested_delta)} rounded to zero "
                                "at qty_step "
                                f"{market.spec.qty_step_base_1e8}"
                            ),
                        },
                        ts=decision_ts,
                        turn=turn,
                        bar_index=turn + 1,
                    )
                elif account.position_qty_base_1e8:
                    account.target_leverage_1e4 = abs(target_lev)

        last_turn = turn

        # A decision bar may be wider than the venue's funding interval.
        # Settlements strictly inside the holding bar apply after its opening
        # fill and before the closing margin/liquidation state.  A settlement
        # at the close belongs to the next turn's coincident-open rule.
        holding_close_ts = decision_ts + pack.decision_bar_ms
        for inside_funding in market.funding:
            if not decision_ts < inside_funding.ts < holding_close_ts:
                continue
            applied_rate, index_px, cash_flow = _applied_funding(
                account,
                market,
                inside_funding,
            )
            if collect_evidence:
                _emit(
                    run.events,
                    event_type="FundingApplied",
                    payload={
                        "market": alias,
                        "settlement_ts": inside_funding.ts,
                        "rate_1e8": applied_rate,
                        "index_px_ticks": index_px,
                        "position_qty_base_1e8": (
                            account.position_qty_base_1e8
                        ),
                        "amount_micro": cash_flow,
                    },
                    ts=inside_funding.ts,
                    turn=turn,
                    bar_index=turn + 1,
                )

        position_before_liquidation = account.position_qty_base_1e8
        if collect_evidence and position_before_liquidation != 0:
                _emit(
                    run.events,
                    event_type="MarginUpdate",
                    payload=_margin_payload(account, market, fill_abs),
                    ts=holding_close_ts,
                turn=turn,
                bar_index=turn + 1,
            )

        if account.position_qty_base_1e8 != 0:
            mark = market.mark[fill_abs]
            mark_notional = notional_micro(
                account.position_qty_base_1e8,
                mark.c,
                market.spec.tick_size_micro,
            )
            maintenance_rate = _maintenance_rate(market.spec, mark_notional)
            liq_px = liquidation_price_ticks(
                account.entry_px_ticks,
                _sign(account.position_qty_base_1e8),
                account.target_leverage_1e4,
                maintenance_rate,
            )
            if liquidation_crossed(
                account.position_qty_base_1e8,
                liq_px,
                mark.h,
                mark.l,
            ):
                close_px = mark.l if account.position_qty_base_1e8 > 0 else mark.h
                old_position = account.position_qty_base_1e8
                realized = realized_pnl_micro(
                    _sign(old_position),
                    abs(old_position),
                    account.entry_px_ticks,
                    close_px,
                    market.spec.tick_size_micro,
                )
                penalty = liquidation_penalty_micro(
                    old_position,
                    close_px,
                    market.spec.tick_size_micro,
                    market.spec.liquidation_penalty_1e8,
                )
                close_notional = notional_micro(
                    old_position,
                    close_px,
                    market.spec.tick_size_micro,
                )
                account.cash_micro += realized - penalty
                account.realized_price_cum_micro += realized
                account.liquidation_penalty_cum_micro += penalty
                account.turnover_notional_micro += close_notional
                run.counters.liquidations += 1
                if collect_evidence:
                    _emit(
                        run.events,
                        event_type="LiquidationTriggered",
                        payload={
                            "market": alias,
                            "trigger": "mark_low" if old_position > 0 else "mark_high",
                            "trigger_px_ticks": close_px,
                            "liq_px_ticks": liq_px,
                            "close_px_ticks": close_px,
                            "position_qty_base_1e8": old_position,
                            "penalty_micro": penalty,
                            "loss_micro": realized - penalty,
                        },
                        ts=holding_close_ts,
                        turn=turn,
                        bar_index=turn + 1,
                    )
                account.position_qty_base_1e8 = 0
                account.entry_px_ticks = 0
                account.target_leverage_1e4 = 0
                terminal = True
            else:
                adverse = mark.l if account.position_qty_base_1e8 > 0 else mark.h
                minimum_distance = distance_to_liquidation_1e8(
                    adverse,
                    liq_px,
                    _sign(account.position_qty_base_1e8),
                )
                if minimum_distance < NEAR_LIQ_THRESHOLD_1E8 and collect_evidence:
                    _emit(
                        run.events,
                        event_type="NearLiquidation",
                        payload={
                            "market": alias,
                            "trigger": (
                                "mark_low"
                                if account.position_qty_base_1e8 > 0
                                else "mark_high"
                            ),
                            "mark_extreme_px_ticks": adverse,
                            "liq_px_ticks": liq_px,
                            "min_intrabar_dist_to_liq_1e8": minimum_distance,
                            "threshold_1e8": NEAR_LIQ_THRESHOLD_1E8,
                        },
                        ts=holding_close_ts,
                        turn=turn,
                        bar_index=turn + 1,
                    )

        run.snapshots.append(
            _snapshot(
                account=account,
                market=market,
                absolute_bar_index=fill_abs,
                relative_bar_index=turn + 1,
                close_ts=holding_close_ts,
            )
        )

        # A liquidation is terminal and preempts the drawdown kill switch.
        trigger_drawdown = _drawdown_1e8(run.snapshots)
        if (
            not terminal
            and not account.kill_switch_active
            and trigger_drawdown >= config.drawdown_kill_switch_1e8
        ):
            trigger_peak_nav = max(row.nav_micro for row in run.snapshots)
            trigger_nav = run.snapshots[-1].nav_micro
            flatten_order_seqs: list[int] = []
            if account.position_qty_base_1e8 != 0:
                mark = market.mark[fill_abs]
                old_position = account.position_qty_base_1e8
                close_px = mark.c
                close_notional = notional_micro(
                    old_position,
                    close_px,
                    market.spec.tick_size_micro,
                )
                close_fee = fee_micro(
                    close_notional,
                    apply_cost_multiplier(
                        market.spec.taker_fee_rate_1e8,
                        multiplier,
                    ),
                )
                realized = realized_pnl_micro(
                    _sign(old_position),
                    abs(old_position),
                    account.entry_px_ticks,
                    close_px,
                    market.spec.tick_size_micro,
                )
                account.cash_micro += realized - close_fee
                account.realized_price_cum_micro += realized
                account.fees_cum_micro += close_fee
                account.turnover_notional_micro += close_notional
                if collect_evidence:
                    flatten_order_seqs.append(
                        _emit(
                            run.events,
                            event_type="OrderFilled",
                            payload={
                                "market": alias,
                                "side": (
                                    "sell" if old_position > 0 else "buy"
                                ),
                                "requested_qty_base_1e8": abs(old_position),
                                "qty_base_1e8": abs(old_position),
                                "ref_open_px_ticks": close_px,
                                "half_spread_ticks": 0,
                                "impact_ticks": 0,
                                "fill_px_ticks": close_px,
                                "notional_micro": close_notional,
                                "fee_micro": close_fee,
                                "slippage_1e8": 0,
                                "cost_profile": "primary",
                            },
                            ts=holding_close_ts,
                            turn=turn,
                            bar_index=turn + 1,
                        )
                    )
                account.position_qty_base_1e8 = 0
                account.entry_px_ticks = 0
                account.target_leverage_1e4 = 0
                run.snapshots[-1] = _snapshot(
                    account=account,
                    market=market,
                    absolute_bar_index=fill_abs,
                    relative_bar_index=turn + 1,
                    close_ts=holding_close_ts,
                )
                if collect_evidence:
                    _emit(
                        run.events,
                        event_type="MarginUpdate",
                        payload=_margin_payload(account, market, fill_abs),
                        ts=holding_close_ts,
                        turn=turn,
                        bar_index=turn + 1,
                    )

            account.kill_switch_active = True
            run.counters.kill_switch_fired = True
            if collect_evidence:
                _emit(
                    run.events,
                    event_type="KillSwitchTriggered",
                    payload={
                        "drawdown_1e8": trigger_drawdown,
                        "limit_1e8": config.drawdown_kill_switch_1e8,
                        "peak_nav_micro": trigger_peak_nav,
                        "nav_micro": trigger_nav,
                        "flatten_order_seqs": flatten_order_seqs,
                    },
                    ts=holding_close_ts,
                    turn=turn,
                    bar_index=turn + 1,
                )

        if terminal and collect_evidence:
            _emit(
                run.events,
                event_type="MarginUpdate",
                payload=_margin_payload(account, market, fill_abs),
                ts=holding_close_ts,
                turn=turn,
                bar_index=turn + 1,
            )

    reason = "liquidated" if terminal else "completed"
    run.totals = _Totals(
        reason=reason,
        final_turn=last_turn,
        final_nav_micro=run.snapshots[-1].nav_micro,
        turnover_notional_micro=account.turnover_notional_micro,
        fill_cost_cum_micro=account.fill_cost_cum_micro,
        fees_cum_micro=account.fees_cum_micro,
        funding_cum_micro=account.funding_cum_micro,
        liquidation_penalty_cum_micro=account.liquidation_penalty_cum_micro,
    )
    return run


def _ledger_rows(
    snapshots: list[_Snapshot],
    *,
    profile: str,
    alias: str,
) -> list[JsonObject]:
    rows: list[JsonObject] = []
    previous: _Snapshot | None = None
    for snapshot in snapshots:
        if previous is None:
            d_nav = d_price = d_funding = d_fees = d_penalty = 0
        else:
            d_nav = snapshot.nav_micro - previous.nav_micro
            d_funding = -(
                snapshot.funding_cum_micro - previous.funding_cum_micro
            )
            d_fees = -(snapshot.fees_cum_micro - previous.fees_cum_micro)
            d_penalty = -(
                snapshot.liquidation_penalty_cum_micro
                - previous.liquidation_penalty_cum_micro
            )
            d_price = (
                snapshot.upnl_micro
                - previous.upnl_micro
                + snapshot.realized_price_cum_micro
                - previous.realized_price_cum_micro
            )
        if d_nav != d_price + d_funding + d_fees + d_penalty:
            raise EngineError(
                f"MATH-2 ledger invariant failed at bar {snapshot.bar_index}"
            )
        realized = (
            snapshot.realized_price_cum_micro
            - snapshot.fees_cum_micro
            - snapshot.funding_cum_micro
            - snapshot.liquidation_penalty_cum_micro
        )
        rows.append(
            {
                "ts": snapshot.close_ts,
                "bar_index": snapshot.bar_index,
                "turn": None if snapshot.bar_index == 0 else snapshot.bar_index - 1,
                "profile": profile,
                "nav_micro": snapshot.nav_micro,
                "cash_micro": snapshot.cash_micro,
                "realized_pnl_micro": realized,
                "d_nav_micro": d_nav,
                "d_price_pnl_micro": d_price,
                "d_funding_micro": d_funding,
                "d_fees_micro": d_fees,
                "d_liq_penalty_micro": d_penalty,
                "positions": {
                    alias: {
                        "qty_base_1e8": snapshot.position_qty_base_1e8,
                        "entry_px_ticks": snapshot.entry_px_ticks,
                        "mark_px_ticks": snapshot.mark_close_ticks,
                        "upnl_micro": snapshot.upnl_micro,
                        "margin_micro": snapshot.margin_micro,
                        "maintenance_margin_micro": snapshot.maintenance_margin_micro,
                        "liq_px_ticks": snapshot.liquidation_px_ticks,
                        "dist_to_liq_1e8": snapshot.distance_to_liquidation_1e8,
                    }
                },
            }
        )
        previous = snapshot
    return rows


def _profile_metrics(
    snapshots: list[_Snapshot],
    totals: _Totals,
    *,
    starting_nav_micro: int,
    equity_curve_ref: str,
) -> JsonObject:
    navs = [snapshot.nav_micro for snapshot in snapshots]
    returns = bar_returns(navs)
    in_position = [
        snapshot
        for snapshot in snapshots
        if snapshot.position_qty_base_1e8 != 0
    ]
    close_distances = [
        cast(int, snapshot.distance_to_liquidation_1e8)
        for snapshot in in_position
    ]
    intrabar_distances = [
        cast(int, snapshot.min_intrabar_distance_1e8)
        for snapshot in in_position
    ]
    distances = distance_statistics(close_distances, intrabar_distances)
    try:
        sortino = sortino_1e8(returns)
    except UndefinedMetricError:
        sortino = 0
    return {
        "net_return_1e8": net_return_1e8(
            totals.final_nav_micro,
            starting_nav_micro,
        ),
        "max_drawdown_1e8": max_drawdown_1e8(navs),
        "sortino_1e8": sortino,
        "cvar5_1e8": cvar_5_1e8(returns),
        "funding_paid_micro": -totals.funding_cum_micro,
        "fees_paid_micro": -totals.fees_cum_micro,
        "fill_cost_micro": -totals.fill_cost_cum_micro,
        "turnover_1e8": turnover_1e8(
            totals.turnover_notional_micro,
            starting_nav_micro,
        ),
        "dist_to_liq_min_1e8": distances.min_intrabar_1e8,
        "dist_to_liq_p05_1e8": distances.p05_close_1e8,
        "dist_to_liq_p25_1e8": distances.p25_close_1e8,
        "dist_to_liq_median_1e8": distances.median_close_1e8,
        "equity_curve_ref": equity_curve_ref,
    }


def _profile_invariant(run: _ProfileRun) -> JsonObject:
    totals = run.totals
    if totals is None:
        raise EngineError("profile totals are unavailable")
    liquidated = run.counters.liquidations > 0
    if liquidated:
        verdict = "liquidated"
    elif run.counters.kill_switch_fired:
        verdict = "killed_flat"
    else:
        verdict = "survived"
    egress_blocked_count = 0
    for event in run.events:
        if event.get("type") != "EgressBlocked":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise EngineError("EgressBlocked event payload is not an object")
        count = payload.get("count")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
        ):
            raise EngineError("EgressBlocked event count is invalid")
        egress_blocked_count += count
        if egress_blocked_count > MAX_IJSON_INT:
            raise EngineError(
                "egress_blocked_count exceeds the I-JSON safe range"
            )
    return {
        "bars": len(run.snapshots),
        "turns": run.counters.turns,
        "invalid_actions": run.counters.invalid_actions,
        "missed_decisions": run.counters.missed_decisions,
        "gate_blocks": run.counters.gate_blocks,
        "post_kill_switch_attempts": run.counters.post_kill_switch_attempts,
        "egress_blocked_count": egress_blocked_count,
        "liquidated": liquidated,
        "kill_switch_fired": run.counters.kill_switch_fired,
        "survival_verdict": verdict,
    }


def run_episode(
    *,
    pack_dir: str | Path,
    agent: InProcessAgent,
    config: EpisodeConfig,
    run_id: str,
    episode_id: str,
) -> EpisodeResult:
    """Run one pack under primary and stress costs from one action trace."""

    pack = load_pack(pack_dir)
    config.validate_cost_profiles(
        pack.market(pack.market_aliases[0]).spec.cost_profile_multipliers_1e4
    )
    if tuple(config.cost_profiles) != ("primary", "stress_2x"):
        raise EngineError("V1 requires primary and stress_2x cost profiles")

    primary = _run_profile(
        pack=pack,
        config=config,
        profile="primary",
        run_id=run_id,
        episode_id=episode_id,
        agent=agent,
        replay_decisions=None,
        collect_evidence=True,
    )
    stress = _run_profile(
        pack=pack,
        config=config,
        profile="stress_2x",
        run_id=run_id,
        episode_id=episode_id,
        agent=None,
        replay_decisions=primary.decisions,
        collect_evidence=False,
    )
    _emit_harness_events(
        primary.events,
        _drain_harness_events(agent),
        ts=primary.snapshots[-1].close_ts,
        turn=None,
        bar_index=None,
    )
    primary_totals = primary.totals
    stress_totals = stress.totals
    if primary_totals is None or stress_totals is None:
        raise EngineError("profile simulation did not finalize")
    invariant = _profile_invariant(primary)
    metrics: JsonObject = {
        "schema": "metrics/v1",
        "run_id": run_id,
        "claim_label": "survival-stress",
        "profiles": {
            "primary": _profile_metrics(
                primary.snapshots,
                primary_totals,
                starting_nav_micro=config.starting_nav_micro,
                equity_curve_ref="ledger.jsonl",
            ),
            "stress_2x": _profile_metrics(
                stress.snapshots,
                stress_totals,
                starting_nav_micro=config.starting_nav_micro,
                equity_curve_ref="ledger_stress_2x.jsonl",
            ),
        },
        "profile_invariant": invariant,
    }
    metrics_sha = sha256_prefixed(canonical_bytes(metrics) + b"\n")
    _emit(
        primary.events,
        event_type="EpisodeEnd",
        payload={
            "reason": primary_totals.reason,
            "final_turn": primary_totals.final_turn,
            "final_nav_micro": primary_totals.final_nav_micro,
            "metrics_sha256": metrics_sha,
        },
        ts=primary.snapshots[-1].close_ts,
        turn=None,
        bar_index=None,
    )
    alias = pack.market_aliases[0]
    return EpisodeResult(
        events=primary.events,
        observations=primary.observations,
        raw_blobs=primary.raw_blobs,
        ledger_primary=_ledger_rows(
            primary.snapshots,
            profile="primary",
            alias=alias,
        ),
        ledger_stress_2x=_ledger_rows(
            stress.snapshots,
            profile="stress_2x",
            alias=alias,
        ),
        metrics=metrics,
        pack=pack,
        config=config,
        run_id=run_id,
        episode_id=episode_id,
    )
