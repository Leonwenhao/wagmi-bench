# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the exact golden metrics estimators."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import TypedDict, cast

import pytest

from core.metrics import (
    UndefinedMetricError,
    bar_returns,
    cvar_5_1e8,
    distance_statistics,
    max_drawdown_1e8,
    nearest_rank,
    net_return_1e8,
    sortino_1e8,
    total_fill_cost_micro,
    turnover_1e8,
)

ROOT = Path(__file__).resolve().parents[2]
STARTING_NAV_MICRO = 10_000_000_000


class PositionRow(TypedDict):
    qty_base_1e8: int
    dist_to_liq_1e8: int | None


class LedgerRow(TypedDict):
    nav_micro: int
    positions: dict[str, PositionRow]


class ProfileMetrics(TypedDict):
    net_return_1e8: int
    max_drawdown_1e8: int
    sortino_1e8: int
    cvar5_1e8: int
    turnover_1e8: int
    dist_to_liq_min_1e8: int | None
    dist_to_liq_p05_1e8: int | None
    dist_to_liq_p25_1e8: int | None
    dist_to_liq_median_1e8: int | None


class MetricsDocument(TypedDict):
    profiles: dict[str, ProfileMetrics]


def _load_case(
    episode: str, profile: str
) -> tuple[list[LedgerRow], ProfileMetrics]:
    filename = "ledger.jsonl" if profile == "primary" else "ledger_stress_2x.jsonl"
    base = ROOT / "fixtures" / "golden-mini" / "expected" / episode
    rows = cast(
        list[LedgerRow],
        [
            json.loads(line)
            for line in (base / filename).read_text(encoding="utf-8").splitlines()
        ],
    )
    document = cast(
        MetricsDocument,
        json.loads((base / "metrics.json").read_text(encoding="utf-8")),
    )
    return rows, document["profiles"][profile]


@pytest.mark.parametrize("episode", ["main", "variant-liquidation"])
@pytest.mark.parametrize("profile", ["primary", "stress_2x"])
def test_metric_estimators_match_every_golden_profile(
    episode: str, profile: str
) -> None:
    rows, expected = _load_case(episode, profile)
    navs = [row["nav_micro"] for row in rows]
    returns = bar_returns(navs)
    assert net_return_1e8(navs[-1], STARTING_NAV_MICRO) == expected["net_return_1e8"]
    assert max_drawdown_1e8(navs) == expected["max_drawdown_1e8"]
    assert sortino_1e8(returns) == expected["sortino_1e8"]
    assert cvar_5_1e8(returns) == expected["cvar5_1e8"]

    close_distances = [
        distance
        for row in rows
        if row["positions"]["BTC"]["qty_base_1e8"] != 0
        for distance in [row["positions"]["BTC"]["dist_to_liq_1e8"]]
        if distance is not None
    ]
    min_intrabar = expected["dist_to_liq_min_1e8"]
    assert min_intrabar is not None
    distances = distance_statistics(close_distances, [min_intrabar])
    assert distances.min_intrabar_1e8 == expected["dist_to_liq_min_1e8"]
    assert distances.p05_close_1e8 == expected["dist_to_liq_p05_1e8"]
    assert distances.p25_close_1e8 == expected["dist_to_liq_p25_1e8"]
    assert distances.median_close_1e8 == expected["dist_to_liq_median_1e8"]


def test_turnover_and_fill_cost_match_pinned_definitions() -> None:
    assert turnover_1e8(71_055_996_220, STARTING_NAV_MICRO) == 710_559_962
    assert total_fill_cost_micro(
        [(20_010_000_000, 20_000_000_000), (3_001_500_000, 3_000_000_000)]
    ) == 11_500_000


def test_nearest_rank_and_empty_distance_semantics() -> None:
    values = [40, 10, 30, 20]
    assert nearest_rank(values, 1, 20) == 10
    assert nearest_rank(values, 1, 2) == 20
    assert distance_statistics([], []).min_intrabar_1e8 is None
    with pytest.raises(ValueError, match="input cannot be empty"):
        nearest_rank([], 1, 2)


def test_cvar_uses_ceiling_five_percent_tail_count() -> None:
    returns = tuple(Fraction(value, 100) for value in range(-3, 18))
    assert cvar_5_1e8(returns) == -2_500_000


def test_sortino_zero_downside_is_explicitly_undefined() -> None:
    with pytest.raises(UndefinedMetricError, match="downside deviation is zero"):
        sortino_1e8([Fraction(1, 100), Fraction(2, 100)])

