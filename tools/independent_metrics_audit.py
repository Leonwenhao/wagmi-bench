#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Independent, standard-library-only audit of WAGMI Bench bundle metrics.

This review script intentionally imports nothing from ``core``, ``report``,
``recorder``, or any other WAGMI Bench package.  It recomputes every metric
whose complete inputs are present in the stored ledger bytes.  For the
canonical primary profile it additionally recomputes fill cost, turnover, and
the adverse-wick minimum from primary lifecycle events.

The V1 stress ledger does not contain stress fill/reference-open records or
adverse intrabar extrema.  Consequently ``fill_cost_micro``,
``turnover_1e8``, and ``dist_to_liq_min_1e8`` cannot be independently derived
from ``ledger_stress_2x.jsonl``.  The script reports that evidence gap
explicitly; it never treats a copied value from ``metrics.json`` as a
recomputation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

RATE_SCALE = 100_000_000
QTY_SCALE = 100_000_000
LEDGER_DERIVED_FIELDS = (
    "net_return_1e8",
    "max_drawdown_1e8",
    "sortino_1e8",
    "cvar5_1e8",
    "funding_paid_micro",
    "fees_paid_micro",
    "dist_to_liq_p05_1e8",
    "dist_to_liq_p25_1e8",
    "dist_to_liq_median_1e8",
    "equity_curve_ref",
)
PRIMARY_EVENT_DERIVED_FIELDS = (
    "fill_cost_micro",
    "turnover_1e8",
    "dist_to_liq_min_1e8",
)
STRESS_UNPROVABLE_FROM_LEDGER = PRIMARY_EVENT_DERIVED_FIELDS
DELTA_FIELDS = (
    "d_price_pnl_micro",
    "d_funding_micro",
    "d_fees_micro",
    "d_liq_penalty_micro",
)


class AuditFailure(RuntimeError):
    """The sealed evidence disagrees with an independently derived fact."""


def _reject_float(token: str) -> None:
    raise AuditFailure(f"fractional JSON number is forbidden: {token}")


def _reject_constant(token: str) -> None:
    raise AuditFailure(f"non-finite JSON number is forbidden: {token}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AuditFailure(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode(raw: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise AuditFailure(f"{source}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditFailure(f"{source}: expected a JSON object")
    return value


def _load_object(path: Path) -> dict[str, Any]:
    try:
        return _decode(path.read_text(encoding="utf-8"), str(path))
    except OSError as exc:
        raise AuditFailure(f"cannot read {path}: {exc}") from exc


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuditFailure(f"cannot read {path}: {exc}") from exc
    if not lines:
        raise AuditFailure(f"{path}: empty JSONL stream")
    return [
        _decode(line, f"{path}:{line_number}")
        for line_number, line in enumerate(lines, 1)
    ]


def _integer(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AuditFailure(f"{context}: expected integer, got {value!r}")
    return value


def _nullable_integer(value: object, context: str) -> int | None:
    if value is None:
        return None
    return _integer(value, context)


def _required_integer(value: int | None, context: str) -> int:
    if value is None:
        raise AuditFailure(f"{context}: null integer is not allowed")
    return value


def _object(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuditFailure(f"{context}: expected object")
    return value


def _list(value: object, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise AuditFailure(f"{context}: expected array")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _bar_returns(navs: list[int]) -> list[Fraction]:
    if len(navs) < 2:
        raise AuditFailure("a complete ledger needs at least two NAV rows")
    values: list[Fraction] = []
    for index, (previous, current) in enumerate(zip(navs, navs[1:]), 1):
        if previous <= 0:
            raise AuditFailure(
                f"ledger row {index}: prior NAV is non-positive"
            )
        values.append(Fraction(current - previous, previous))
    return values


def _floor_ratio_over_sqrt(
    numerator: Fraction,
    denominator: Fraction,
) -> int:
    """Compute floor(numerator / sqrt(denominator)) by integer inequalities."""

    if denominator <= 0:
        raise AuditFailure("square-root denominator must be positive")
    squared = numerator * numerator / denominator
    root = math.isqrt(_floor(squared))
    if numerator >= 0:
        return root
    exact = squared.denominator == 1 and root * root == squared.numerator
    return -root if exact else -root - 1


def _nearest_rank(values: list[int], numerator: int, denominator: int) -> int:
    ordered = sorted(values)
    if not ordered:
        raise AuditFailure("nearest-rank percentile requires values")
    rank = max(1, (numerator * len(ordered) + denominator - 1) // denominator)
    return ordered[rank - 1]


def _recompute_ledger(
    rows: list[dict[str, Any]],
    *,
    profile: str,
    starting_nav: int,
    expected_ref: str,
) -> dict[str, int | str | None]:
    navs: list[int] = []
    close_distances: list[int] = []
    fees = 0
    funding = 0
    previous_nav: int | None = None
    previous_realized: int | None = None

    for index, row in enumerate(rows):
        context = f"{profile} ledger row {index}"
        if row.get("profile") != profile:
            raise AuditFailure(f"{context}: profile mismatch")
        nav = _integer(row.get("nav_micro"), f"{context}.nav_micro")
        cash = _integer(row.get("cash_micro"), f"{context}.cash_micro")
        realized = _integer(
            row.get("realized_pnl_micro"),
            f"{context}.realized_pnl_micro",
        )
        d_nav = _integer(row.get("d_nav_micro"), f"{context}.d_nav_micro")
        deltas = {
            field: _integer(row.get(field), f"{context}.{field}")
            for field in DELTA_FIELDS
        }
        if d_nav != sum(deltas.values()):
            raise AuditFailure(
                f"{context}: d_nav={d_nav} != attribution sum "
                f"{sum(deltas.values())}"
            )
        if index == 0:
            if row.get("turn") is not None:
                raise AuditFailure(f"{context}: anchor turn is not null")
            if nav != starting_nav:
                raise AuditFailure(
                    f"{context}: anchor NAV {nav} != starting NAV {starting_nav}"
                )
            if any((d_nav, *deltas.values())):
                raise AuditFailure(f"{context}: anchor deltas are not all zero")
        else:
            if row.get("turn") != index - 1:
                raise AuditFailure(f"{context}: non-contiguous turn")
            if previous_nav is None or nav - previous_nav != d_nav:
                raise AuditFailure(
                    f"{context}: NAV change does not equal d_nav"
                )
            if previous_realized is None:
                raise AuditFailure(f"{context}: missing prior realized PnL")

        positions = _object(row.get("positions"), f"{context}.positions")
        upnl = 0
        for alias, position_value in positions.items():
            position = _object(position_value, f"{context}.positions.{alias}")
            qty = _integer(
                position.get("qty_base_1e8"),
                f"{context}.positions.{alias}.qty_base_1e8",
            )
            upnl += _integer(
                position.get("upnl_micro"),
                f"{context}.positions.{alias}.upnl_micro",
            )
            distance = position.get("dist_to_liq_1e8")
            if qty == 0:
                if distance is not None:
                    raise AuditFailure(
                        f"{context}.positions.{alias}: flat distance is not null"
                    )
            else:
                close_distances.append(
                    _integer(
                        distance,
                        f"{context}.positions.{alias}.dist_to_liq_1e8",
                    )
                )
        if nav != cash + upnl:
            raise AuditFailure(
                f"{context}: NAV {nav} != cash {cash} + uPnL {upnl}"
            )
        if cash != starting_nav + realized:
            raise AuditFailure(
                f"{context}: cash does not reconcile to cumulative realized PnL"
            )

        navs.append(nav)
        fees += deltas["d_fees_micro"]
        funding += deltas["d_funding_micro"]
        previous_nav = nav
        previous_realized = realized

    returns = _bar_returns(navs)
    peak = navs[0]
    max_drawdown = Fraction(0)
    for nav in navs:
        peak = max(peak, nav)
        max_drawdown = max(max_drawdown, Fraction(peak - nav, peak))
    mean_return = sum(returns, Fraction(0)) / len(returns)
    downside_squared = (
        sum((min(value, Fraction(0)) ** 2 for value in returns), Fraction(0))
        / len(returns)
    )
    sortino = (
        0
        if downside_squared == 0
        else _floor_ratio_over_sqrt(
            mean_return * RATE_SCALE,
            downside_squared,
        )
    )
    tail_count = (len(returns) + 19) // 20
    cvar = _floor(
        sum(sorted(returns)[:tail_count], Fraction(0))
        * RATE_SCALE
        / tail_count
    )
    distances: dict[str, int | None]
    if close_distances:
        distances = {
            "dist_to_liq_p05_1e8": _nearest_rank(
                close_distances, 1, 20
            ),
            "dist_to_liq_p25_1e8": _nearest_rank(
                close_distances, 1, 4
            ),
            "dist_to_liq_median_1e8": _nearest_rank(
                close_distances, 1, 2
            ),
        }
    else:
        distances = {
            "dist_to_liq_p05_1e8": None,
            "dist_to_liq_p25_1e8": None,
            "dist_to_liq_median_1e8": None,
        }
    return {
        "net_return_1e8": (navs[-1] - starting_nav)
        * RATE_SCALE
        // starting_nav,
        "max_drawdown_1e8": _floor(max_drawdown * RATE_SCALE),
        "sortino_1e8": sortino,
        "cvar5_1e8": cvar,
        "funding_paid_micro": funding,
        "fees_paid_micro": fees,
        **distances,
        "equity_curve_ref": expected_ref,
    }


def _tick_size(
    bundle: Path,
    turn: int,
    alias: str,
) -> int:
    observation = _load_object(bundle / "observations" / f"{turn:04d}.json")
    markets = _object(observation.get("markets"), "observation.markets")
    market = _object(markets.get(alias), f"observation.markets.{alias}")
    return _integer(
        market.get("tick_size_micro"),
        f"observation.markets.{alias}.tick_size_micro",
    )


def _primary_event_metrics(
    bundle: Path,
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    starting_nav: int,
) -> dict[str, int | None]:
    fill_cost = 0
    turnover = 0
    for event in events:
        event_type = event.get("type")
        payload = _object(event.get("payload"), "event.payload")
        if event_type == "OrderFilled":
            if payload.get("cost_profile") != "primary":
                raise AuditFailure("non-primary fill in canonical event stream")
            turn = _integer(event.get("turn"), "OrderFilled.turn")
            alias = payload.get("market")
            if not isinstance(alias, str):
                raise AuditFailure("OrderFilled.market is not a string")
            tick_size = _tick_size(bundle, turn, alias)
            qty = _integer(
                payload.get("qty_base_1e8"),
                "OrderFilled.qty_base_1e8",
            )
            fill_px = _integer(
                payload.get("fill_px_ticks"),
                "OrderFilled.fill_px_ticks",
            )
            ref_px = _integer(
                payload.get("ref_open_px_ticks"),
                "OrderFilled.ref_open_px_ticks",
            )
            stored_notional = _integer(
                payload.get("notional_micro"),
                "OrderFilled.notional_micro",
            )
            recomputed_notional = abs(qty) * fill_px * tick_size // QTY_SCALE
            reference_notional = abs(qty) * ref_px * tick_size // QTY_SCALE
            if stored_notional != recomputed_notional:
                raise AuditFailure("OrderFilled notional does not recompute")
            turnover += stored_notional
            fill_cost += abs(stored_notional - reference_notional)
        elif event_type == "LiquidationTriggered":
            turn = _integer(event.get("turn"), "LiquidationTriggered.turn")
            alias = payload.get("market")
            if not isinstance(alias, str):
                raise AuditFailure("LiquidationTriggered.market is not a string")
            tick_size = _tick_size(bundle, turn, alias)
            qty = _integer(
                payload.get("position_qty_base_1e8"),
                "LiquidationTriggered.position_qty_base_1e8",
            )
            close_px = _integer(
                payload.get("close_px_ticks"),
                "LiquidationTriggered.close_px_ticks",
            )
            turnover += abs(qty) * close_px * tick_size // QTY_SCALE
    intrabar_distances: list[int] = []
    for turn, decision in enumerate(decisions):
        cost = _object(
            decision.get("cost_to_hold"),
            f"decision {turn}.cost_to_hold",
        )
        margin_value = cost.get("margin_after")
        if margin_value is None:
            continue
        margin = _object(
            margin_value,
            f"decision {turn}.cost_to_hold.margin_after",
        )
        qty = _integer(
            margin.get("position_qty_base_1e8"),
            f"decision {turn}.margin_after.position_qty_base_1e8",
        )
        if qty != 0:
            intrabar_distances.append(
                _integer(
                    margin.get("min_intrabar_dist_to_liq_1e8"),
                    f"decision {turn}.margin_after.min_intrabar_dist_to_liq_1e8",
                )
            )
    return {
        "fill_cost_micro": -fill_cost,
        "turnover_1e8": turnover * RATE_SCALE // starting_nav,
        "dist_to_liq_min_1e8": (
            min(intrabar_distances) if intrabar_distances else None
        ),
    }


def _lifecycle_metrics(
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    bars: int,
) -> dict[str, int | bool | str]:
    rejected_reasons = [
        _object(event.get("payload"), "ActionRejected.payload").get("reason")
        for event in events
        if event.get("type") == "ActionRejected"
    ]
    missed = sum(
        reason in {"timeout", "agent_error"} for reason in rejected_reasons
    )
    invalid = len(rejected_reasons) - missed
    gate_blocks = sum(
        _object(event.get("payload"), "RiskCheck.payload").get("verdict")
        == "block"
        for event in events
        if event.get("type") == "RiskCheck"
    )
    post_kill = sum(
        event.get("type") == "PostKillSwitchAttempt" for event in events
    )
    egress = sum(
        _integer(
            _object(event.get("payload"), "EgressBlocked.payload").get("count"),
            "EgressBlocked.count",
        )
        for event in events
        if event.get("type") == "EgressBlocked"
    )
    liquidated = any(
        event.get("type") == "LiquidationTriggered" for event in events
    )
    kill_switch = any(
        event.get("type") == "KillSwitchTriggered" for event in events
    )
    verdict = (
        "liquidated"
        if liquidated
        else "killed_flat"
        if kill_switch
        else "survived"
    )
    return {
        "bars": bars,
        "turns": len(decisions),
        "invalid_actions": invalid,
        "missed_decisions": missed,
        "gate_blocks": gate_blocks,
        "post_kill_switch_attempts": post_kill,
        "egress_blocked_count": egress,
        "liquidated": liquidated,
        "kill_switch_fired": kill_switch,
        "survival_verdict": verdict,
    }


def _check_primary_attribution(
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
) -> None:
    for turn, decision in enumerate(decisions):
        account = _object(
            decision.get("account_after"),
            f"decision {turn}.account_after",
        )
        row = ledger[turn + 1]
        for field in (
            "nav_micro",
            "cash_micro",
            "realized_pnl_micro",
            "d_nav_micro",
            *DELTA_FIELDS,
        ):
            if account.get(field) != row.get(field):
                raise AuditFailure(
                    f"decision {turn}.{field} disagrees with primary ledger"
                )

    event_funding = sum(
        _integer(
            _object(event.get("payload"), "FundingApplied.payload").get(
                "amount_micro"
            ),
            "FundingApplied.amount_micro",
        )
        for event in events
        if event.get("type") == "FundingApplied"
    )
    ledger_funding = sum(
        _integer(row.get("d_funding_micro"), "ledger.d_funding_micro")
        for row in ledger
    )
    if event_funding != ledger_funding:
        raise AuditFailure("funding events disagree with primary ledger")

    event_fees = -sum(
        _integer(
            _object(event.get("payload"), "OrderFilled.payload").get(
                "fee_micro"
            ),
            "OrderFilled.fee_micro",
        )
        for event in events
        if event.get("type") == "OrderFilled"
    )
    ledger_fees = sum(
        _integer(row.get("d_fees_micro"), "ledger.d_fees_micro")
        for row in ledger
    )
    if event_fees != ledger_fees:
        raise AuditFailure("fill fee events disagree with primary ledger")

    event_penalties = -sum(
        _integer(
            _object(event.get("payload"), "LiquidationTriggered.payload").get(
                "penalty_micro"
            ),
            "LiquidationTriggered.penalty_micro",
        )
        for event in events
        if event.get("type") == "LiquidationTriggered"
    )
    ledger_penalties = sum(
        _integer(
            row.get("d_liq_penalty_micro"),
            "ledger.d_liq_penalty_micro",
        )
        for row in ledger
    )
    if event_penalties != ledger_penalties:
        raise AuditFailure("liquidation events disagree with primary ledger")

    kill_events = [
        event for event in events if event.get("type") == "KillSwitchTriggered"
    ]
    if kill_events:
        if len(kill_events) != 1:
            raise AuditFailure("expected at most one kill-switch event")
        kill_event = kill_events[0]
        kill_turn = _integer(kill_event.get("turn"), "KillSwitchTriggered.turn")
        for row in ledger[kill_turn + 1 :]:
            positions = _object(row.get("positions"), "post-kill positions")
            if any(
                _integer(
                    _object(value, "post-kill position").get("qty_base_1e8"),
                    "post-kill qty",
                )
                != 0
                for value in positions.values()
            ):
                raise AuditFailure("position is non-flat at or after kill switch")
        kill_seq = _integer(kill_event.get("seq"), "KillSwitchTriggered.seq")
        if any(
            event.get("type") == "OrderFilled"
            and _integer(event.get("seq"), "event.seq") > kill_seq
            for event in events
        ):
            raise AuditFailure("fill occurred after kill-switch activation")


def _fixed(value: int, scale: int, places: int) -> str:
    sign = "-" if value < 0 else ""
    whole, remainder = divmod(abs(value), scale)
    if places == 0:
        return f"{sign}{whole}"
    fraction = remainder * (10**places) // scale
    return f"{sign}{whole}.{fraction:0{places}d}"


def _money(value: int, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{_fixed(value, 1_000_000, 6)} quote"


def _percent(value: int | None, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{_fixed(value * 100, RATE_SCALE, 4)}%"


def _ratio(value: int) -> str:
    return _fixed(value, RATE_SCALE, 4)


def _check_report(
    report_path: Path,
    metrics: dict[str, Any],
    lifecycle: dict[str, int | bool | str],
    events: list[dict[str, Any]],
) -> None:
    text = report_path.read_text(encoding="utf-8")
    invariant_lines = (
        f"verdict: {str(lifecycle['survival_verdict']).replace('_', ' ').upper()}",
        f"turns: {lifecycle['turns']}",
        f"bars: {lifecycle['bars']}",
        f"gate blocks: {lifecycle['gate_blocks']}",
        "post-kill-switch attempts: "
        f"{lifecycle['post_kill_switch_attempts']}",
        f"blocked egress count: {lifecycle['egress_blocked_count']}",
    )
    for line in invariant_lines:
        if line not in text:
            raise AuditFailure(f"report is missing exact line: {line}")

    profiles = _object(metrics.get("profiles"), "metrics.profiles")
    primary = _object(profiles.get("primary"), "metrics.profiles.primary")
    stress = _object(profiles.get("stress_2x"), "metrics.profiles.stress_2x")
    rows: tuple[
        tuple[str, str, Callable[[int | None], str]],
        ...,
    ] = (
        (
            "End NAV change",
            "net_return_1e8",
            lambda value: _percent(
                _required_integer(value, "net_return_1e8"),
                signed=True,
            ),
        ),
        (
            "Maximum drawdown",
            "max_drawdown_1e8",
            lambda value: _percent(
                _required_integer(value, "max_drawdown_1e8")
            ),
        ),
        (
            "Worst 5% bar tail",
            "cvar5_1e8",
            lambda value: _percent(
                _required_integer(value, "cvar5_1e8"),
                signed=True,
            ),
        ),
        (
            "Downside-adjusted ratio",
            "sortino_1e8",
            lambda value: _ratio(
                _required_integer(value, "sortino_1e8")
            ),
        ),
        (
            "Funding flow",
            "funding_paid_micro",
            lambda value: _money(
                _required_integer(value, "funding_paid_micro"),
                signed=True,
            ),
        ),
        (
            "Fees",
            "fees_paid_micro",
            lambda value: _money(
                _required_integer(value, "fees_paid_micro"),
                signed=True,
            ),
        ),
        (
            "Spread + impact",
            "fill_cost_micro",
            lambda value: _money(
                _required_integer(value, "fill_cost_micro"),
                signed=True,
            ),
        ),
        (
            "Turnover / starting NAV",
            "turnover_1e8",
            lambda value: _ratio(
                _required_integer(value, "turnover_1e8")
            )
            + "x",
        ),
        (
            "Minimum wick distance to liquidation",
            "dist_to_liq_min_1e8",
            _percent,
        ),
        (
            "5th percentile distance to liquidation",
            "dist_to_liq_p05_1e8",
            _percent,
        ),
        (
            "25th percentile distance to liquidation",
            "dist_to_liq_p25_1e8",
            _percent,
        ),
        (
            "Median distance to liquidation",
            "dist_to_liq_median_1e8",
            _percent,
        ),
    )
    for label, field, formatter in rows:
        primary_value = _nullable_integer(
            primary.get(field),
            f"primary.{field}",
        )
        stress_value = _nullable_integer(
            stress.get(field),
            f"stress.{field}",
        )
        expected = (
            f"{label:42} | {formatter(primary_value):24} | "
            f"{formatter(stress_value):24}"
        )
        if expected not in text:
            raise AuditFailure(
                f"report metric row is absent or inexact: {label}"
            )

    near_death = sum(
        event.get("type") in {"NearLiquidation", "LiquidationTriggered"}
        for event in events
    )
    if near_death == 0 and "- no near-liquidation or liquidation events" not in text:
        raise AuditFailure("report near-death timeline is not truthful")


def audit_bundle(
    bundle: Path,
    *,
    report_root: Path | None,
) -> dict[str, object]:
    manifest = _load_object(bundle / "manifest.json")
    metrics = _load_object(bundle / "metrics.json")
    chain = _load_object(bundle / "chain.json")
    if chain.get("complete") is not True:
        raise AuditFailure(f"{bundle}: chain is not marked complete")
    files = _object(chain.get("files"), "chain.files")
    for relative, expected_hash in files.items():
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise AuditFailure("chain.files entry is malformed")
        actual_hash = _sha256(bundle / relative)
        if actual_hash != expected_hash:
            raise AuditFailure(
                f"{bundle}/{relative}: {actual_hash} != {expected_hash}"
            )

    events = _load_jsonl(bundle / "events.jsonl")
    if [
        _integer(event.get("seq"), "event.seq") for event in events
    ] != list(range(len(events))):
        raise AuditFailure(f"{bundle}: event sequence is not contiguous")
    if events[-1].get("type") != "EpisodeEnd":
        raise AuditFailure(f"{bundle}: final event is not EpisodeEnd")
    episode_end = _object(events[-1].get("payload"), "EpisodeEnd.payload")
    if episode_end.get("metrics_sha256") != _sha256(bundle / "metrics.json"):
        raise AuditFailure(f"{bundle}: EpisodeEnd metrics hash mismatch")

    decisions = [
        _load_object(path)
        for path in sorted((bundle / "decisions").glob("*.json"))
    ]
    primary_rows = _load_jsonl(bundle / "ledger.jsonl")
    stress_rows = _load_jsonl(bundle / "ledger_stress_2x.jsonl")
    if len(primary_rows) != len(stress_rows):
        raise AuditFailure(f"{bundle}: profile ledger lengths differ")
    if len(primary_rows) != len(decisions) + 1:
        raise AuditFailure(
            f"{bundle}: ledger rows do not equal decisions plus anchor"
        )

    run_config = _object(manifest.get("run_config"), "manifest.run_config")
    starting_nav = _integer(
        run_config.get("starting_nav_micro"),
        "manifest.run_config.starting_nav_micro",
    )
    recomputed_profiles = {
        "primary": _recompute_ledger(
            primary_rows,
            profile="primary",
            starting_nav=starting_nav,
            expected_ref="ledger.jsonl",
        ),
        "stress_2x": _recompute_ledger(
            stress_rows,
            profile="stress_2x",
            starting_nav=starting_nav,
            expected_ref="ledger_stress_2x.jsonl",
        ),
    }
    recomputed_profiles["primary"].update(
        _primary_event_metrics(
            bundle,
            events,
            decisions,
            starting_nav=starting_nav,
        )
    )

    stored_profiles = _object(metrics.get("profiles"), "metrics.profiles")
    compared_fields: dict[str, list[str]] = {}
    for profile, recomputed in recomputed_profiles.items():
        stored = _object(
            stored_profiles.get(profile),
            f"metrics.profiles.{profile}",
        )
        compared_fields[profile] = sorted(recomputed)
        for field, expected in recomputed.items():
            if stored.get(field) != expected:
                raise AuditFailure(
                    f"{bundle}: {profile}.{field} stored={stored.get(field)!r} "
                    f"recomputed={expected!r}"
                )

    lifecycle = _lifecycle_metrics(events, decisions, len(primary_rows))
    invariant = _object(
        metrics.get("profile_invariant"),
        "metrics.profile_invariant",
    )
    for field, expected in lifecycle.items():
        if invariant.get(field) != expected:
            raise AuditFailure(
                f"{bundle}: invariant.{field} stored={invariant.get(field)!r} "
                f"recomputed={expected!r}"
            )
    _check_primary_attribution(events, decisions, primary_rows)

    final_nav = _integer(
        episode_end.get("final_nav_micro"),
        "EpisodeEnd.final_nav_micro",
    )
    if final_nav != primary_rows[-1].get("nav_micro"):
        raise AuditFailure(f"{bundle}: EpisodeEnd final NAV mismatch")
    if episode_end.get("final_turn") != len(decisions) - 1:
        raise AuditFailure(f"{bundle}: EpisodeEnd final turn mismatch")

    report_checked = False
    if report_root is not None:
        pack = _object(manifest.get("pack"), "manifest.pack")
        pack_id = pack.get("pack_id")
        if not isinstance(pack_id, str):
            raise AuditFailure("manifest.pack.pack_id is not a string")
        report_path = report_root / pack_id / "report.txt"
        _check_report(report_path, metrics, lifecycle, events)
        report_checked = True

    return {
        "bundle": str(bundle),
        "root": chain.get("root"),
        "turns": len(decisions),
        "verdict": lifecycle["survival_verdict"],
        "compared_fields": compared_fields,
        "stress_unprovable_from_ledger": list(
            STRESS_UNPROVABLE_FROM_LEDGER
        ),
        "report_checked": report_checked,
    }


def audit_liquidation_projection(
    fixture: Path,
    *,
    tick_size_micro: int,
) -> dict[str, object]:
    """Audit the frozen golden liquidation projection without engine imports."""

    metrics = _load_object(fixture / "metrics.json")
    events = _load_jsonl(fixture / "events.jsonl")
    primary_rows = _load_jsonl(fixture / "ledger.jsonl")
    stress_rows = _load_jsonl(fixture / "ledger_stress_2x.jsonl")
    starting_nav = _integer(
        primary_rows[0].get("nav_micro"),
        "liquidation fixture starting NAV",
    )
    stored_profiles = _object(metrics.get("profiles"), "metrics.profiles")
    for profile, rows, equity_reference in (
        ("primary", primary_rows, "ledger.jsonl"),
        ("stress_2x", stress_rows, "ledger_stress_2x.jsonl"),
    ):
        recomputed = _recompute_ledger(
            rows,
            profile=profile,
            starting_nav=starting_nav,
            expected_ref=equity_reference,
        )
        stored = _object(
            stored_profiles.get(profile),
            f"metrics.profiles.{profile}",
        )
        for field, expected in recomputed.items():
            if stored.get(field) != expected:
                raise AuditFailure(
                    f"{fixture}: {profile}.{field} stored="
                    f"{stored.get(field)!r} recomputed={expected!r}"
                )

    liquidations = [
        event for event in events if event.get("type") == "LiquidationTriggered"
    ]
    if len(liquidations) != 1:
        raise AuditFailure(
            f"{fixture}: expected exactly one liquidation event"
        )
    liquidation = liquidations[0]
    payload = _object(
        liquidation.get("payload"),
        "LiquidationTriggered.payload",
    )
    bar_index = _integer(
        liquidation.get("bar_index"),
        "LiquidationTriggered.bar_index",
    )
    if bar_index != len(primary_rows) - 1:
        raise AuditFailure("liquidation is not terminal in the fixture")
    penalty = _integer(
        payload.get("penalty_micro"),
        "LiquidationTriggered.penalty_micro",
    )
    qty = _integer(
        payload.get("position_qty_base_1e8"),
        "LiquidationTriggered.position_qty_base_1e8",
    )
    close_px = _integer(
        payload.get("close_px_ticks"),
        "LiquidationTriggered.close_px_ticks",
    )
    market = payload.get("market")
    if not isinstance(market, str):
        raise AuditFailure("LiquidationTriggered.market is not a string")
    entry_px = _integer(
        _object(
            _object(
                primary_rows[bar_index - 1].get("positions"),
                "pre-liquidation positions",
            ).get(market),
            "pre-liquidation position",
        ).get("entry_px_ticks"),
        "pre-liquidation entry_px_ticks",
    )
    realized = qty * (close_px - entry_px) * tick_size_micro // QTY_SCALE
    if payload.get("loss_micro") != realized - penalty:
        raise AuditFailure("liquidation loss does not equal realized loss - penalty")

    for profile, rows in (
        ("primary", primary_rows),
        ("stress_2x", stress_rows),
    ):
        terminal = rows[bar_index]
        if terminal.get("d_liq_penalty_micro") != -penalty:
            raise AuditFailure(
                f"{profile}: liquidation penalty is not attributed exactly"
            )
        positions = _object(
            terminal.get("positions"),
            f"{profile} terminal positions",
        )
        if any(
            _integer(
                _object(value, f"{profile} terminal position").get(
                    "qty_base_1e8"
                ),
                f"{profile} terminal qty",
            )
            != 0
            for value in positions.values()
        ):
            raise AuditFailure(f"{profile}: terminal liquidation row is not flat")

    fill_cost = 0
    turnover = 0
    for event in events:
        if event.get("type") != "OrderFilled":
            continue
        fill = _object(event.get("payload"), "OrderFilled.payload")
        fill_qty = _integer(
            fill.get("qty_base_1e8"),
            "OrderFilled.qty_base_1e8",
        )
        fill_px = _integer(
            fill.get("fill_px_ticks"),
            "OrderFilled.fill_px_ticks",
        )
        reference_px = _integer(
            fill.get("ref_open_px_ticks"),
            "OrderFilled.ref_open_px_ticks",
        )
        notional = abs(fill_qty) * fill_px * tick_size_micro // QTY_SCALE
        reference_notional = (
            abs(fill_qty) * reference_px * tick_size_micro // QTY_SCALE
        )
        if fill.get("notional_micro") != notional:
            raise AuditFailure("golden fill notional does not recompute")
        fill_cost += abs(notional - reference_notional)
        turnover += notional
    liquidation_notional = abs(qty) * close_px * tick_size_micro // QTY_SCALE
    turnover += liquidation_notional
    primary_metrics = _object(
        stored_profiles.get("primary"),
        "metrics.profiles.primary",
    )
    if primary_metrics.get("fill_cost_micro") != -fill_cost:
        raise AuditFailure("golden primary fill cost mismatch")
    expected_turnover = turnover * RATE_SCALE // starting_nav
    if primary_metrics.get("turnover_1e8") != expected_turnover:
        raise AuditFailure("golden primary turnover mismatch")

    invariant = _object(
        metrics.get("profile_invariant"),
        "metrics.profile_invariant",
    )
    if (
        invariant.get("liquidated") is not True
        or invariant.get("survival_verdict") != "liquidated"
    ):
        raise AuditFailure("golden liquidation verdict mismatch")
    return {
        "fixture": str(fixture),
        "penalty_micro": penalty,
        "realized_price_loss_micro": realized,
        "loss_after_penalty_micro": realized - penalty,
        "primary_turnover_1e8": expected_turnover,
        "primary_fill_cost_micro": -fill_cost,
        "terminal_nav_micro": primary_rows[-1]["nav_micro"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-root",
        type=Path,
        help="optional root containing <pack-id>/report.txt",
    )
    parser.add_argument(
        "--liquidation-fixture",
        type=Path,
        help="optional frozen golden projection to audit",
    )
    parser.add_argument(
        "--tick-size-micro",
        type=int,
        default=10_000,
        help="tick size for --liquidation-fixture (default: 10000)",
    )
    parser.add_argument(
        "bundles",
        nargs="*",
        type=Path,
        help="one or more COMPLETE bundle directories",
    )
    args = parser.parse_args(argv)
    if not args.bundles and args.liquidation_fixture is None:
        parser.error("provide at least one bundle or --liquidation-fixture")
    results: list[dict[str, object]] = []
    liquidation_result: dict[str, object] | None = None
    try:
        for bundle in args.bundles:
            results.append(
                audit_bundle(bundle, report_root=args.report_root)
            )
        if args.liquidation_fixture is not None:
            liquidation_result = audit_liquidation_projection(
                args.liquidation_fixture,
                tick_size_micro=args.tick_size_micro,
            )
    except (AuditFailure, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    for result in results:
        fields = result["compared_fields"]
        assert isinstance(fields, dict)
        print(
            "PASS "
            f"{result['bundle']} "
            f"root={result['root']} "
            f"turns={result['turns']} "
            f"verdict={result['verdict']} "
            f"primary_fields={len(fields['primary'])} "
            f"stress_fields={len(fields['stress_2x'])} "
            f"report={'yes' if result['report_checked'] else 'no'}"
        )
    if liquidation_result is not None:
        print(
            "PASS_LIQUIDATION "
            f"{liquidation_result['fixture']} "
            f"penalty_micro={liquidation_result['penalty_micro']} "
            "realized_price_loss_micro="
            f"{liquidation_result['realized_price_loss_micro']} "
            "loss_after_penalty_micro="
            f"{liquidation_result['loss_after_penalty_micro']} "
            "primary_turnover_1e8="
            f"{liquidation_result['primary_turnover_1e8']} "
            "primary_fill_cost_micro="
            f"{liquidation_result['primary_fill_cost_micro']} "
            f"terminal_nav_micro={liquidation_result['terminal_nav_micro']}"
        )
    print(
        "STRUCTURAL GAP stress_2x ledger lacks inputs for: "
        + ", ".join(STRESS_UNPROVABLE_FROM_LEDGER)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
