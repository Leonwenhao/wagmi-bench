# SPDX-License-Identifier: Apache-2.0
"""Named source definitions and offline-testable pack acquisition.

This module is the data-layer entry point behind the future
``wagmibench fetch-data --pack <id>`` command.  Definitions contain only
operator metadata and exact upstream archive URLs; raw archives, checksum
files, and derived JSONL series remain local and ignored by Git.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping

from data.binance import BinanceBulkFetcher
from data.builder import (
    BuiltPack,
    PackBuildConfig,
    RawSeriesArchive,
    SeriesRole,
    TimestampUnit,
    build_pack,
)

HOUR_MS = 3_600_000
FOUR_HOURS_MS = 14_400_000
DAY_MS = 86_400_000


def _utc_midnight_ms(value: date) -> int:
    """Convert a UTC calendar boundary to epoch milliseconds exactly."""

    return (value - date(1970, 1, 1)).days * DAY_MS


COVID_WINDOW_START_TS = _utc_midnight_ms(date(2020, 3, 5))
COVID_WINDOW_END_TS = _utc_midnight_ms(date(2020, 3, 21))


class PackCatalogError(ValueError):
    """Raised when a requested named pack is not in the source catalog."""


@dataclass(frozen=True, slots=True)
class ArchiveDefinition:
    """One exact Binance bulk archive required by a named pack."""

    role: SeriesRole
    interval_ms: int
    url: str
    timestamp_unit: TimestampUnit = "ms"


@dataclass(frozen=True, slots=True)
class PackDefinition:
    """Committed source recipe for a deterministically built pack."""

    config: PackBuildConfig
    archives: tuple[ArchiveDefinition, ...]

    @property
    def pack_id(self) -> str:
        return self.config.pack_id


def _btc_market_descriptor(calibration_note: str) -> Mapping[str, object]:
    """Return the scope-pinned conservative BTCUSDT replay descriptor."""

    return {
        "instrument": "binance-um:BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "tick_size_micro": 10_000,
        "qty_step_base_1e8": 100_000,
        "min_notional_micro": 10_000_000,
        "leverage_cap_lev_1e4": 30_000,
        "margin": {
            "tiers": [
                {
                    "notional_cap_micro": 9_007_199_254_740_991,
                    "initial_rate_1e8": 33_333_334,
                    "maintenance_rate_1e8": 16_666_667,
                }
            ],
            "liquidation_penalty_1e8": 500_000,
        },
        "funding": {
            "interval_ms": 28_800_000,
            "settlement_offsets_ms": [0, 28_800_000, 57_600_000],
            "cap_1e8": 300_000,
            "floor_1e8": -300_000,
        },
        "fees": {
            "maker_rate_1e8": 20_000,
            "taker_rate_1e8": 40_000,
        },
        "execution": {
            "half_spread_1e8": 50_000,
            "impact_model": "sqrt",
            "impact_coeff_1e8": 30_000,
            "participation_cap_1e8": 1_000_000,
            "cost_profile_multipliers_1e4": {
                "primary": 10_000,
                "stress_2x": 20_000,
            },
        },
        "calibration_note": calibration_note,
    }


# Historical exchangeInfo snapshots are not part of the checksummed bulk
# corpus. Keep separate era entries so a later primary-source audit can change
# one era without silently changing every pack. Until that audit lands, every
# entry deliberately uses the single conservative venue baseline pinned by the
# V1 scope (8h funding, one fee schedule, +/-0.30% cap, 10 USDT min notional).
# The manifest-compatible calibration note flags that limitation; no
# non-contract evidence fields are smuggled into the descriptor.
_BTCUSDT_MARKETS_BY_ERA: Mapping[str, Mapping[str, object]] = {
    era: _btc_market_descriptor(
        "Conservative V1 Binance BTCUSDT descriptor for "
        f"{era}; historical exchangeInfo parameters remain pending "
        "primary-source verification before public release. Funding clock, "
        "fee schedule, funding cap, 10 USDT minimum notional, 3x protocol "
        "leverage cap, and execution costs use the scope-pinned baseline."
    )
    for era in (
        "2020-covid",
        "2020-late",
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    )
}

_ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly"
_BAR_ARCHIVE_DIRECTORIES: Mapping[SeriesRole, str] = {
    "trade": "klines",
    "mark": "markPriceKlines",
    "index": "indexPriceKlines",
}
_BAR_ROLES: tuple[SeriesRole, ...] = ("trade", "mark", "index")


def _covered_months(start: date, end_exclusive: date) -> tuple[str, ...]:
    """Return calendar months intersecting an end-exclusive UTC window."""

    if end_exclusive <= start:
        raise ValueError("catalog window must be non-empty")
    final_day = end_exclusive - timedelta(days=1)
    cursor = date(start.year, start.month, 1)
    final_month = date(final_day.year, final_day.month, 1)
    months: list[str] = []
    while cursor <= final_month:
        months.append(f"{cursor.year:04d}-{cursor.month:02d}")
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return tuple(months)


def _monthly_archives(
    *,
    start: date,
    end_exclusive: date,
    decision_bar_ms: int,
) -> tuple[ArchiveDefinition, ...]:
    """Build exact role-major Binance monthly archive definitions."""

    interval_label = {
        HOUR_MS: "1h",
        FOUR_HOURS_MS: "4h",
    }[decision_bar_ms]
    months = _covered_months(start, end_exclusive)
    archives: list[ArchiveDefinition] = []
    for role in _BAR_ROLES:
        directory = _BAR_ARCHIVE_DIRECTORIES[role]
        for month in months:
            archives.append(
                ArchiveDefinition(
                    role=role,
                    interval_ms=decision_bar_ms,
                    url=(
                        f"{_ARCHIVE_ROOT}/{directory}/BTCUSDT/"
                        f"{interval_label}/BTCUSDT-{interval_label}-"
                        f"{month}.zip"
                    ),
                    timestamp_unit=_archive_timestamp_unit(month),
                )
            )
    for month in months:
        archives.append(
            ArchiveDefinition(
                role="funding",
                interval_ms=0,
                url=(
                    f"{_ARCHIVE_ROOT}/fundingRate/BTCUSDT/"
                    f"BTCUSDT-fundingRate-{month}.zip"
                ),
                timestamp_unit=_archive_timestamp_unit(month),
            )
        )
    return tuple(archives)


def _archive_timestamp_unit(month: str) -> TimestampUnit:
    """Return the observed Binance USDT-M monthly timestamp unit.

    Checksum-verified October 2025 BTCUSDT kline, mark, index, and funding
    archives remain millisecond-stamped. Keep ``month`` explicit so a future
    per-archive transition can be represented without ambient inference.
    """

    del month
    return "ms"


def _pack_definition(
    *,
    pack_id: str,
    start: date,
    end_exclusive: date,
    decision_bar_ms: int,
    market_era: str,
    regime_description: str,
) -> PackDefinition:
    return PackDefinition(
        config=PackBuildConfig(
            pack_id=pack_id,
            market_alias="BTC",
            market_descriptor=_BTCUSDT_MARKETS_BY_ERA[market_era],
            window_start_ts=_utc_midnight_ms(start),
            window_end_ts=_utc_midnight_ms(end_exclusive),
            decision_bar_ms=decision_bar_ms,
            warmup_bars=64,
            default_lookback_bars=64,
            default_funding_prints=6,
            regime_description=regime_description,
            created_by_version="0.1.0",
        ),
        archives=_monthly_archives(
            start=start,
            end_exclusive=end_exclusive,
            decision_bar_ms=decision_bar_ms,
        ),
    )


COVID_BLACK_THURSDAY = _pack_definition(
    pack_id="covid-black-thursday",
    start=date(2020, 3, 5),
    end_exclusive=date(2020, 3, 21),
    decision_bar_ms=HOUR_MS,
    market_era="2020-covid",
    regime_description=(
        "March 5-20, 2020 UTC: the COVID Black Thursday cascade and "
        "initial recovery on Binance BTCUSDT perpetual futures. This is a "
        "survival-stress scenario for liquidation, gap handling, funding, "
        "and kill-switch discipline; the description and real dates are "
        "never serialized into agent observations."
    ),
)

CHINA_MINING_BAN = _pack_definition(
    pack_id="china-mining-ban",
    start=date(2021, 5, 12),
    end_exclusive=date(2021, 5, 25),
    decision_bar_ms=HOUR_MS,
    market_era="2021",
    regime_description=(
        "May 12-24, 2021 UTC: the China mining-ban selloff after Bitcoin's "
        "blow-off top. This survival-stress scenario tests controlled "
        "de-levering into a sharp multi-day decline versus repeated "
        "knife-catching."
    ),
)

LUNA_COLLAPSE = _pack_definition(
    pack_id="luna-collapse",
    start=date(2022, 5, 5),
    end_exclusive=date(2022, 5, 17),
    decision_bar_ms=HOUR_MS,
    market_era="2022",
    regime_description=(
        "May 5-16, 2022 UTC: the LUNA contagion grind and volatility spike "
        "on Binance BTCUSDT perpetual futures. This survival-stress "
        "scenario tests sustained drawdown management as market stress "
        "compounds across several days."
    ),
)

FTX_2022 = _pack_definition(
    pack_id="ftx-2022",
    start=date(2022, 11, 5),
    end_exclusive=date(2022, 11, 16),
    decision_bar_ms=HOUR_MS,
    market_era="2022",
    regime_description=(
        "November 5-15, 2022 UTC: the FTX insolvency slow bleed on Binance "
        "BTCUSDT perpetual futures. This survival-stress scenario tests "
        "position sizing and drawdown control through multi-day uncertainty "
        "rather than a single flash crash."
    ),
)

YEN_CARRY_UNWIND = _pack_definition(
    pack_id="yen-carry-unwind",
    start=date(2024, 7, 29),
    end_exclusive=date(2024, 8, 9),
    decision_bar_ms=HOUR_MS,
    market_era="2024",
    regime_description=(
        "July 29-August 8, 2024 UTC: the yen-carry unwind, weekend gap, and "
        "V-shaped recovery on Binance BTCUSDT perpetual futures. This "
        "survival-stress scenario tests gap risk and the temptation to "
        "panic-sell the recovery low."
    ),
)

OCTOBER_10_CASCADE = _pack_definition(
    pack_id="10-10-cascade",
    start=date(2025, 10, 9),
    end_exclusive=date(2025, 10, 14),
    decision_bar_ms=HOUR_MS,
    market_era="2025",
    regime_description=(
        "October 9-13, 2025 UTC: the 10/10 liquidation cascade on Binance "
        "BTCUSDT perpetual futures. This survival-stress scenario uses 1h "
        "bars to retain the extreme wick and tests liquidation discipline "
        "during the event's most violent phase."
    ),
)

SPOT_ETF_APPROVAL = _pack_definition(
    pack_id="spot-etf-approval",
    start=date(2024, 1, 8),
    end_exclusive=date(2024, 1, 26),
    decision_bar_ms=FOUR_HOURS_MS,
    market_era="2024",
    regime_description=(
        "January 8-25, 2024 UTC: the spot-ETF approval pop, sell-the-news "
        "dump, and recovery on Binance BTCUSDT perpetual futures. This "
        "melt-up scenario tests momentum traps and chasing a headline move."
    ),
)

ETF_RUMOR_WHIPSAW = _pack_definition(
    pack_id="etf-rumor-whipsaw",
    start=date(2023, 10, 13),
    end_exclusive=date(2023, 11, 1),
    decision_bar_ms=FOUR_HOURS_MS,
    market_era="2023",
    regime_description=(
        "October 13-31, 2023 UTC: the false ETF-approval spike, retrace, and "
        "subsequent rally on Binance BTCUSDT perpetual futures. This "
        "melt-up scenario tests whipsaw discipline around unconfirmed moves."
    ),
)

ELECTION_RUN = _pack_definition(
    pack_id="election-run",
    start=date(2024, 11, 4),
    end_exclusive=date(2024, 12, 7),
    decision_bar_ms=FOUR_HOURS_MS,
    market_era="2024",
    regime_description=(
        "November 4-December 6, 2024 UTC: Bitcoin's post-election run from "
        "roughly 69k toward 100k on Binance BTCUSDT perpetual futures. This "
        "melt-up scenario tests letting winners run, premature exits, and "
        "the funding cost of chasing a crowded long."
    ),
)

Q4_2020_INSTITUTIONAL_RUN = _pack_definition(
    pack_id="q4-2020-institutional-run",
    start=date(2020, 10, 1),
    end_exclusive=date(2021, 1, 1),
    decision_bar_ms=FOUR_HOURS_MS,
    market_era="2020-late",
    regime_description=(
        "October 1-December 31, 2020 UTC: Bitcoin's Q4 institutional "
        "accumulation trend on Binance BTCUSDT perpetual futures. This "
        "melt-up scenario tests trend-following patience through an extended "
        "rather than explosive advance."
    ),
)

JAN_2021_SQUEEZE = _pack_definition(
    pack_id="jan-2021-squeeze",
    start=date(2021, 1, 1),
    end_exclusive=date(2021, 2, 22),
    decision_bar_ms=FOUR_HOURS_MS,
    market_era="2021",
    regime_description=(
        "January 1-February 21, 2021 UTC: a sequence of violent Bitcoin "
        "up-moves and deep mid-trend flushes on Binance BTCUSDT perpetual "
        "futures. This melt-up scenario tests holding discipline through "
        "large retracements inside a continuing trend."
    ),
)

SUMMER_2024_RANGE = _pack_definition(
    pack_id="summer-2024-range",
    start=date(2024, 6, 1),
    end_exclusive=date(2024, 7, 29),
    decision_bar_ms=FOUR_HOURS_MS,
    market_era="2024",
    regime_description=(
        "June 1-July 28, 2024 UTC: Bitcoin's directionless summer range, "
        "distribution-driven dip, and recovery on Binance BTCUSDT "
        "perpetual futures. This chop scenario tests funding bleed, "
        "overtrading, and range-trap discipline."
    ),
)

DEAD_ZONE_2023 = _pack_definition(
    pack_id="2023-dead-zone",
    start=date(2023, 6, 1),
    end_exclusive=date(2023, 10, 1),
    decision_bar_ms=FOUR_HOURS_MS,
    market_era="2023",
    regime_description=(
        "June 1-September 30, 2023 UTC: Bitcoin's historically quiet "
        "low-volatility range on Binance BTCUSDT perpetual futures. This "
        "chop scenario tests knowing when not to trade, funding drag, and "
        "cash discipline."
    ),
)

_PACKS: Mapping[str, PackDefinition] = {
    definition.pack_id: definition
    for definition in (
        COVID_BLACK_THURSDAY,
        CHINA_MINING_BAN,
        LUNA_COLLAPSE,
        FTX_2022,
        YEN_CARRY_UNWIND,
        OCTOBER_10_CASCADE,
        SPOT_ETF_APPROVAL,
        ETF_RUMOR_WHIPSAW,
        ELECTION_RUN,
        Q4_2020_INSTITUTIONAL_RUN,
        JAN_2021_SQUEEZE,
        SUMMER_2024_RANGE,
        DEAD_ZONE_2023,
    )
}


def available_pack_ids() -> tuple[str, ...]:
    """Return named source definitions in deterministic order."""

    return tuple(sorted(_PACKS))


def get_pack_definition(pack_id: str) -> PackDefinition:
    """Resolve one exact source recipe without performing network I/O."""

    try:
        return _PACKS[pack_id]
    except KeyError as exc:
        choices = ", ".join(available_pack_ids())
        raise PackCatalogError(
            f"unknown pack {pack_id!r}; available packs: {choices}"
        ) from exc


def fetch_and_build_pack(
    pack_id: str,
    *,
    raw_root: Path,
    packs_root: Path,
    fetcher: BinanceBulkFetcher | None = None,
) -> BuiltPack:
    """Fetch verified source archives and deterministically build one pack.

    Each role receives a separate raw directory because Binance uses the same
    archive basename for trade, mark, and index 1h klines.
    """

    definition = get_pack_definition(pack_id)
    bulk_fetcher = fetcher or BinanceBulkFetcher()
    sources: list[RawSeriesArchive] = []
    for archive in definition.archives:
        role_directory = raw_root / definition.pack_id / archive.role
        source = bulk_fetcher.fetch_archive(archive.url, role_directory)
        sources.append(
            RawSeriesArchive(
                role=archive.role,
                interval_ms=archive.interval_ms,
                source=source,
                timestamp_unit=archive.timestamp_unit,
            )
        )
    return build_pack(
        definition.config,
        sources,
        packs_root / definition.pack_id,
    )
