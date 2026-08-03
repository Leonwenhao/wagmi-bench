# SPDX-License-Identifier: Apache-2.0
"""Leakage-safe construction of the frozen ``observation/v1`` surface."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping, cast

from core.config import (
    EpisodeConfig,
    VirtualClock,
    rebase_timestamp,
)
from core.pack import (
    BarRow,
    FundingRow,
    MarketData,
    PackData,
    RowProvenance,
)

DAY_MS: Final = 86_400_000
RATE_SCALE_1E8: Final = 100_000_000
LEV_SCALE_1E4: Final = 10_000
EPISODE_ID_PATTERN: Final = re.compile(r"^ep_[0-9a-f]{16}$")


class ObservationError(ValueError):
    """Raised when an observation cannot be built without violating IC-2."""


@dataclass(frozen=True, slots=True)
class PositionState:
    """Engine state needed to expose one market position."""

    qty_base_1e8: int = 0
    entry_px_ticks: int | None = None
    margin_micro: int = 0
    liq_px_ticks: int | None = None

    def __post_init__(self) -> None:
        if not _is_int(self.qty_base_1e8):
            raise ObservationError("qty_base_1e8 must be an integer")
        if not _is_int(self.margin_micro) or self.margin_micro < 0:
            raise ObservationError("margin_micro must be an integer >= 0")
        if self.qty_base_1e8 == 0:
            if self.entry_px_ticks is not None or self.liq_px_ticks is not None:
                raise ObservationError("flat positions must use null prices")
            if self.margin_micro != 0:
                raise ObservationError("flat positions must use zero margin")
        else:
            if (
                not _is_int(self.entry_px_ticks)
                or cast(int, self.entry_px_ticks) < 0
                or not _is_int(self.liq_px_ticks)
                or cast(int, self.liq_px_ticks) < 0
            ):
                raise ObservationError(
                    "non-flat positions require non-negative entry and liquidation prices"
                )


@dataclass(frozen=True, slots=True)
class AccountState:
    """Non-derived account fields; NAV is derived from the selected mark rows."""

    cash_micro: int
    realized_pnl_micro: int

    def __post_init__(self) -> None:
        if not _is_int(self.cash_micro):
            raise ObservationError("cash_micro must be an integer")
        if not _is_int(self.realized_pnl_micro):
            raise ObservationError("realized_pnl_micro must be an integer")


@dataclass(frozen=True, slots=True)
class RiskState:
    """Current consumption of the episode-level risk budgets."""

    drawdown_used_1e8: int = 0
    turnover_used_1e8: int = 0
    kill_switch_active: bool = False

    def __post_init__(self) -> None:
        if not _is_int(self.drawdown_used_1e8) or self.drawdown_used_1e8 < 0:
            raise ObservationError("drawdown_used_1e8 must be an integer >= 0")
        if not _is_int(self.turnover_used_1e8) or self.turnover_used_1e8 < 0:
            raise ObservationError("turnover_used_1e8 must be an integer >= 0")
        if not isinstance(self.kill_switch_active, bool):
            raise ObservationError("kill_switch_active must be boolean")


@dataclass(frozen=True, slots=True)
class MarketObservationProvenance:
    """Stored rows behind every emitted market-data surface."""

    trade_rows: tuple[RowProvenance, ...]
    mark_row: RowProvenance
    index_row: RowProvenance
    funding_rows: tuple[RowProvenance, ...]


@dataclass(frozen=True, slots=True)
class ObservationBuild:
    """Schema-shaped document plus its non-agent-visible provenance proof."""

    document: dict[str, object]
    real_clock_ts: int
    rebase_offset_ms: int
    provenance: Mapping[str, MarketObservationProvenance]

    def assert_no_future_sources(self) -> None:
        """Recheck the load-bearing ISO-1 property against stored row bytes."""

        for alias, sources in self.provenance.items():
            rows = (
                sources.trade_rows
                + (sources.mark_row, sources.index_row)
                + sources.funding_rows
            )
            for source in rows:
                if source.available_at > self.real_clock_ts:
                    raise ObservationError(
                        f"{alias}/{source.role}[{source.row_index}] is from the future"
                    )

    def assert_single_mark_invariant(self) -> None:
        """Recompute every mark-derived scalar from the in-band mark."""

        markets = cast(dict[str, object], self.document["markets"])
        positions = cast(dict[str, object], self.document["position"])
        account = cast(dict[str, object], self.document["account"])
        expected_nav = cast(int, account["cash_micro"])
        for alias in sorted(markets):
            market = cast(dict[str, object], markets[alias])
            position = cast(dict[str, object], positions[alias])
            qty = cast(int, position["qty_base_1e8"])
            entry = cast(int | None, position["entry_px_ticks"])
            mark = cast(int, market["last_mark_px_ticks"])
            tick_size = cast(int, market["tick_size_micro"])
            if qty == 0:
                expected_upnl = 0
                expected_distance: int | None = None
            else:
                if entry is None:
                    raise ObservationError(
                        f"{alias}: non-flat position has no entry price"
                    )
                expected_upnl = (
                    qty * (mark - entry) * tick_size // RATE_SCALE_1E8
                )
                liquidation = cast(int | None, position["liq_px_ticks"])
                if liquidation is None or mark <= 0:
                    raise ObservationError(
                        f"{alias}: non-flat position has invalid mark/liquidation"
                    )
                expected_distance = (
                    abs(mark - liquidation) * RATE_SCALE_1E8 // mark
                )
            if position["upnl_micro"] != expected_upnl:
                raise ObservationError(
                    f"{alias}: uPnL does not derive from last_mark_px_ticks"
                )
            if position["dist_to_liq_1e8"] != expected_distance:
                raise ObservationError(
                    f"{alias}: liquidation distance uses a different mark"
                )
            expected_nav += cast(int, position["margin_micro"]) + expected_upnl
        if account["nav_micro"] != expected_nav:
            raise ObservationError("account NAV violates the single-mark invariant")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _upnl_micro(
    position: PositionState,
    *,
    mark_px_ticks: int,
    tick_size_micro: int,
) -> int:
    if position.qty_base_1e8 == 0:
        return 0
    entry = cast(int, position.entry_px_ticks)
    numerator = (
        position.qty_base_1e8
        * (mark_px_ticks - entry)
        * tick_size_micro
    )
    # Frozen golden/oracle rule: signed PnL floors, including negative values.
    return numerator // RATE_SCALE_1E8


def _distance_to_liquidation_1e8(
    position: PositionState,
    *,
    mark_px_ticks: int,
) -> int | None:
    if position.qty_base_1e8 == 0:
        return None
    if mark_px_ticks <= 0:
        raise ObservationError(
            "non-flat position cannot be observed at a non-positive mark"
        )
    liquidation = cast(int, position.liq_px_ticks)
    return abs(mark_px_ticks - liquidation) * RATE_SCALE_1E8 // mark_px_ticks


def _notional_micro(
    position: PositionState,
    *,
    mark_px_ticks: int,
    tick_size_micro: int,
) -> int:
    numerator = (
        abs(position.qty_base_1e8)
        * mark_px_ticks
        * tick_size_micro
    )
    return numerator // RATE_SCALE_1E8


def _next_settlement_real_ts(
    clock_ts: int,
    settlement_offsets_ms: tuple[int, ...],
) -> int:
    if not settlement_offsets_ms:
        raise ObservationError("funding settlement schedule is empty")
    day_start = (clock_ts // DAY_MS) * DAY_MS
    for offset in settlement_offsets_ms:
        candidate = day_start + offset
        if candidate > clock_ts:
            return candidate
    return day_start + DAY_MS + settlement_offsets_ms[0]


def _last_available_bar(
    rows: tuple[BarRow, ...],
    *,
    alias: str,
    role: str,
) -> BarRow:
    if not rows:
        raise ObservationError(f"{alias} has no available {role} row")
    return rows[-1]


def _position_document(
    position: PositionState,
    *,
    mark_px_ticks: int,
    tick_size_micro: int,
) -> tuple[dict[str, object], int]:
    upnl = _upnl_micro(
        position,
        mark_px_ticks=mark_px_ticks,
        tick_size_micro=tick_size_micro,
    )
    document: dict[str, object] = {
        "qty_base_1e8": position.qty_base_1e8,
        "entry_px_ticks": position.entry_px_ticks,
        "upnl_micro": upnl,
        "margin_micro": position.margin_micro,
        "liq_px_ticks": position.liq_px_ticks,
        "dist_to_liq_1e8": _distance_to_liquidation_1e8(
            position,
            mark_px_ticks=mark_px_ticks,
        ),
    }
    return document, upnl


def _market_document(
    market: MarketData,
    *,
    clock: VirtualClock,
    bars_lookback: int,
    funding_lookback: int,
) -> tuple[
    dict[str, object],
    BarRow,
    BarRow,
    tuple[BarRow, ...],
    tuple[FundingRow, ...],
]:
    trade_available = market.available_trade(clock.real_ts)
    mark_available = market.available_mark(clock.real_ts)
    index_available = market.available_index(clock.real_ts)
    funding_available = market.available_funding(clock.real_ts)

    trade_rows = trade_available[-bars_lookback:]
    mark_row = _last_available_bar(
        mark_available,
        alias=market.spec.alias,
        role="mark",
    )
    index_row = _last_available_bar(
        index_available,
        alias=market.spec.alias,
        role="index",
    )
    funding_rows = (
        funding_available[-funding_lookback:]
        if funding_lookback
        else ()
    )
    if not trade_rows:
        raise ObservationError(
            f"{market.spec.alias} has no available trade lookback"
        )

    offset = clock.rebase_offset_ms
    next_settlement = _next_settlement_real_ts(
        clock.real_ts,
        market.spec.funding.settlement_offsets_ms,
    )
    document: dict[str, object] = {
        "tick_size_micro": market.spec.tick_size_micro,
        "qty_step_base_1e8": market.spec.qty_step_base_1e8,
        "min_notional_micro": market.spec.min_notional_micro,
        "taker_fee_rate_1e8": market.spec.taker_fee_rate_1e8,
        "leverage_cap_lev_1e4": market.spec.leverage_cap_lev_1e4,
        "last_mark_px_ticks": mark_row.c,
        "last_index_px_ticks": index_row.c,
        "bars": [
            row.observation_mapping(offset_ms=offset) for row in trade_rows
        ],
        "funding": {
            "interval_ms": market.spec.funding.interval_ms,
            "next_settlement_ts": rebase_timestamp(
                next_settlement,
                offset_ms=offset,
            ),
            "cap_1e8": market.spec.funding.cap_1e8,
            "floor_1e8": market.spec.funding.floor_1e8,
            "prints": [
                row.observation_mapping(offset_ms=offset)
                for row in funding_rows
            ],
        },
    }
    return document, mark_row, index_row, trade_rows, funding_rows


def _risk_constraints(
    *,
    config: EpisodeConfig,
    pack: PackData,
    notionals_micro: Mapping[str, int],
    nav_micro: int,
    risk: RiskState,
) -> list[dict[str, object]]:
    if nav_micro <= 0:
        raise ObservationError("NAV must be positive while observations continue")
    used_by_market = {
        alias: notional * LEV_SCALE_1E4 // nav_micro
        for alias, notional in notionals_micro.items()
    }
    constraints: list[dict[str, object]] = [
        {
            "constraint_id": "lev-gross",
            "type": "leverage_cap_gross",
            "scope": "account",
            "limit": config.leverage_cap_gross_lev_1e4,
            "used": sum(notionals_micro.values()) * LEV_SCALE_1E4 // nav_micro,
            "unit": "lev_1e4",
        }
    ]
    for alias in pack.market_aliases:
        constraints.append(
            {
                "constraint_id": f"lev-{alias}",
                "type": "leverage_cap_market",
                "scope": alias,
                "limit": pack.market(alias).spec.leverage_cap_lev_1e4,
                "used": used_by_market[alias],
                "unit": "lev_1e4",
            }
        )
    if config.turnover_cap_1e8 is not None:
        constraints.append(
            {
                "constraint_id": "turnover",
                "type": "turnover_cap",
                "scope": "account",
                "limit": config.turnover_cap_1e8,
                "used": risk.turnover_used_1e8,
                "unit": "1e8",
            }
        )
    constraints.append(
        {
            "constraint_id": "drawdown-ks",
            "type": "drawdown_kill_switch",
            "scope": "account",
            "limit": config.drawdown_kill_switch_1e8,
            "used": risk.drawdown_used_1e8,
            "unit": "1e8",
        }
    )
    return constraints


def build_observation(
    pack: PackData,
    config: EpisodeConfig,
    *,
    episode_id: str,
    turn: int,
    account: AccountState,
    positions: Mapping[str, PositionState] | None = None,
    risk: RiskState | None = None,
) -> ObservationBuild:
    """Build one schema-shaped observation and its provenance proof."""

    if EPISODE_ID_PATTERN.fullmatch(episode_id) is None:
        raise ObservationError("episode_id must match ep_<16 lowercase hex>")
    if not _is_int(turn) or turn < 0 or turn >= pack.bars_total:
        raise ObservationError("turn is outside the episode")

    position_states = positions if positions is not None else {}
    unknown_positions = set(position_states) - set(pack.market_aliases)
    if unknown_positions:
        raise ObservationError(
            "positions contain unknown markets: "
            + ", ".join(sorted(unknown_positions))
        )
    current_risk = risk if risk is not None else RiskState()
    effective = config.effective_lookback(
        pack_bars=pack.default_lookback.bars,
        pack_funding_prints=pack.default_lookback.funding_prints,
    )
    for alias in pack.market_aliases:
        config.validate_cost_profiles(
            pack.market(alias).spec.cost_profile_multipliers_1e4
        )
    clock = VirtualClock.for_window(
        window_start_ts=pack.window_start_ts,
        real_ts=pack.clock_real_ts(turn),
    )

    market_documents: dict[str, object] = {}
    position_documents: dict[str, object] = {}
    provenance: dict[str, MarketObservationProvenance] = {}
    notionals: dict[str, int] = {}
    margin_and_upnl = 0

    for alias in pack.market_aliases:
        market = pack.market(alias)
        (
            market_document,
            mark_row,
            index_row,
            trade_rows,
            funding_rows,
        ) = _market_document(
            market,
            clock=clock,
            bars_lookback=effective.bars,
            funding_lookback=effective.funding_prints,
        )
        position = position_states.get(alias, PositionState())
        position_document, upnl = _position_document(
            position,
            mark_px_ticks=mark_row.c,
            tick_size_micro=market.spec.tick_size_micro,
        )
        market_documents[alias] = market_document
        position_documents[alias] = position_document
        notionals[alias] = _notional_micro(
            position,
            mark_px_ticks=mark_row.c,
            tick_size_micro=market.spec.tick_size_micro,
        )
        margin_and_upnl += position.margin_micro + upnl
        provenance[alias] = MarketObservationProvenance(
            trade_rows=tuple(row.provenance for row in trade_rows),
            mark_row=mark_row.provenance,
            index_row=index_row.provenance,
            funding_rows=tuple(row.provenance for row in funding_rows),
        )

    nav_micro = account.cash_micro + margin_and_upnl
    document: dict[str, object] = {
        "schema": "observation/v1",
        "episode": {
            "episode_id": episode_id,
            "turn": turn,
            "bar_index": turn,
            "bars_total": pack.bars_total,
            "bars_remaining": pack.bars_total - turn - 1,
            "decision_bar_ms": pack.decision_bar_ms,
            "response_deadline_ms": config.response_deadline_ms,
        },
        "clock_ts": clock.rebased_ts,
        "markets": market_documents,
        "position": position_documents,
        "account": {
            "nav_micro": nav_micro,
            "cash_micro": account.cash_micro,
            "realized_pnl_micro": account.realized_pnl_micro,
        },
        "risk": {
            "constraints": _risk_constraints(
                config=config,
                pack=pack,
                notionals_micro=notionals,
                nav_micro=nav_micro,
                risk=current_risk,
            ),
            "kill_switch_active": current_risk.kill_switch_active,
        },
    }
    result = ObservationBuild(
        document=document,
        real_clock_ts=clock.real_ts,
        rebase_offset_ms=clock.rebase_offset_ms,
        provenance=MappingProxyType(provenance),
    )
    result.assert_no_future_sources()
    result.assert_single_mark_invariant()
    return result
