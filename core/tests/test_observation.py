# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from core.config import (
    DEFAULT_COST_PROFILES,
    ConfigError,
    EpisodeConfig,
    VirtualClock,
    time_rebase_offset_ms,
)
from core.observation import (
    AccountState,
    PositionState,
    RiskState,
    build_observation,
)
from core.pack import PackData, load_pack

ROOT = Path(__file__).resolve().parents[2]
LEAKAGE = ROOT / "fixtures" / "leakage-probe"
OBSERVATION_SCHEMA = json.loads(
    (ROOT / "spec/schemas/observation.v1.schema.json").read_text()
)
RUN_CONFIG_SCHEMA = json.loads(
    (ROOT / "spec/schemas/bundle_manifest.v1.schema.json").read_text()
)["properties"]["run_config"]
SENTINELS = frozenset(
    json.loads((LEAKAGE / "sentinels.json").read_text())["sentinel_values"]
)


def _integers(value: object) -> list[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        result: list[int] = []
        for item in cast(list[object], value):
            result.extend(_integers(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in cast(dict[str, object], value).values():
            result.extend(_integers(item))
        return result
    return []


def _pack() -> PackData:
    return load_pack(LEAKAGE)


def _config(
    *,
    lookback_bars: int | None = None,
    funding_prints: int | None = None,
) -> EpisodeConfig:
    return EpisodeConfig(
        lookback_bars=lookback_bars,
        funding_prints=funding_prints,
    )


def test_legacy_fixture_config_formalizes_to_frozen_run_config() -> None:
    raw = json.loads((ROOT / "fixtures/golden-mini/episode_config.json").read_text())
    config = EpisodeConfig.from_mapping(raw)
    assert config.lookback_bars == 4
    assert config.funding_prints == 2
    assert config.response_deadline_ms == 120_000
    assert config.cost_profiles == DEFAULT_COST_PROFILES
    Draft202012Validator(RUN_CONFIG_SCHEMA).validate(config.to_run_config())


def test_v1_runtime_config_cannot_drop_the_stress_profile() -> None:
    with pytest.raises(ConfigError, match="requires both cost profiles"):
        EpisodeConfig(cost_profiles=("primary",))


def test_pack_loader_keeps_trade_mark_and_index_separate() -> None:
    market = _pack().market("BTC")
    assert market.trade[0].provenance.role == "trade"
    assert market.mark[0].provenance.role == "mark"
    assert market.index[0].provenance.role == "index"
    assert market.trade[0].c == 95_400
    assert market.mark[0].c == 95_410
    assert market.index[0].c == 95_390
    assert tuple(row.ts for row in market.trade) == tuple(
        row.ts for row in market.mark
    )
    assert tuple(row.ts for row in market.trade) == tuple(
        row.ts for row in market.index
    )


def test_week_rebase_and_virtual_clock_are_pack_driven() -> None:
    pack = _pack()
    offset = time_rebase_offset_ms(pack.window_start_ts)
    assert offset % 604_800_000 == 0
    assert offset <= pack.window_start_ts < offset + 604_800_000
    clock = VirtualClock.for_window(
        window_start_ts=pack.window_start_ts,
        real_ts=pack.clock_real_ts(0),
    )
    assert clock.rebase_offset_ms == offset
    assert clock.rebased_ts == clock.real_ts - offset

    # The poisoned trade row sits at the scheduled turn-3 timestamp but its
    # stored future available_at must never pull the virtual clock forward.
    poisoned_available_at = json.loads(
        (LEAKAGE / "sentinels.json").read_text()
    )["poison_available_at"]
    assert pack.clock_real_ts(3) < poisoned_available_at


def test_observation_is_schema_valid_and_single_mark_exact() -> None:
    pack = _pack()
    position = PositionState(
        qty_base_1e8=50_000_000,
        entry_px_ticks=95_000,
        margin_micro=1_000_000_000,
        liq_px_ticks=80_000,
    )
    result = build_observation(
        pack,
        _config(),
        episode_id="ep_0011223344556677",
        turn=0,
        account=AccountState(
            cash_micro=9_000_000_000,
            realized_pnl_micro=0,
        ),
        positions={"BTC": position},
        risk=RiskState(drawdown_used_1e8=125_000),
    )
    Draft202012Validator(OBSERVATION_SCHEMA).validate(result.document)
    result.assert_no_future_sources()
    result.assert_single_mark_invariant()

    markets = cast(dict[str, object], result.document["markets"])
    market = cast(dict[str, object], markets["BTC"])
    positions = cast(dict[str, object], result.document["position"])
    exposed_position = cast(dict[str, object], positions["BTC"])
    account = cast(dict[str, object], result.document["account"])
    mark = cast(int, market["last_mark_px_ticks"])
    expected_upnl = (
        position.qty_base_1e8
        * (mark - cast(int, position.entry_px_ticks))
        * pack.market("BTC").spec.tick_size_micro
        // 100_000_000
    )
    assert exposed_position["upnl_micro"] == expected_upnl
    assert exposed_position["dist_to_liq_1e8"] == (
        abs(mark - cast(int, position.liq_px_ticks)) * 100_000_000 // mark
    )
    assert account["nav_micro"] == (
        9_000_000_000 + position.margin_micro + expected_upnl
    )
    assert market["last_mark_px_ticks"] != cast(
        list[dict[str, int]], market["bars"]
    )[-1]["c"]
    assert market["last_index_px_ticks"] != market["last_mark_px_ticks"]


def test_negative_fractional_upnl_uses_frozen_floor_rule() -> None:
    pack = _pack()
    result = build_observation(
        pack,
        _config(),
        episode_id="ep_1020304050607080",
        turn=0,
        account=AccountState(
            cash_micro=10_000_000_000,
            realized_pnl_micro=0,
        ),
        positions={
            "BTC": PositionState(
                qty_base_1e8=1,
                entry_px_ticks=95_111,
                margin_micro=1,
                liq_px_ticks=80_000,
            )
        },
    )
    positions = cast(dict[str, object], result.document["position"])
    exposed = cast(dict[str, object], positions["BTC"])
    # 1e-8 BTC * -1 tick * 0.1 USDT/tick = -0.001 micro; floor = -1.
    assert exposed["upnl_micro"] == -1


def test_all_leakage_probe_turns_filter_stored_available_at_and_sentinels() -> None:
    pack = _pack()
    configs = (
        _config(),
        _config(lookback_bars=1, funding_prints=0),
        _config(lookback_bars=20, funding_prints=20),
    )
    for config in configs:
        for turn in range(pack.bars_total):
            result = build_observation(
                pack,
                config,
                episode_id="ep_89abcdef01234567",
                turn=turn,
                account=AccountState(
                    cash_micro=10_000_000_000,
                    realized_pnl_micro=0,
                ),
            )
            Draft202012Validator(OBSERVATION_SCHEMA).validate(result.document)
            result.assert_no_future_sources()
            result.assert_single_mark_invariant()
            assert not (set(_integers(result.document)) & SENTINELS)
            episode = cast(dict[str, object], result.document["episode"])
            assert episode["bars_total"] == 5
            assert episode["bars_remaining"] == 4 - turn


def test_seeded_random_configs_never_surface_future_rows_or_sentinels() -> None:
    """ISO-1 property check across boundary and randomized operator configs."""

    pack = _pack()
    rng = random.Random(0x1501)
    boundary_bars = (
        None,
        1,
        pack.default_lookback.bars,
        pack.bars_total,
        len(pack.market("BTC").trade),
        len(pack.market("BTC").trade) + 1,
    )
    boundary_funding = (
        None,
        0,
        1,
        pack.default_lookback.funding_prints,
        len(pack.market("BTC").funding),
        len(pack.market("BTC").funding) + 1,
    )
    configs = {
        (
            rng.choice(boundary_bars)
            if seed % 3
            else rng.randint(1, len(pack.market("BTC").trade) * 3),
            (
                rng.choice(boundary_funding)
                if seed % 5
                else rng.randint(0, len(pack.market("BTC").funding) * 3)
            ),
        )
        for seed in range(256)
    }
    configs.update(
        (bars, prints)
        for bars in boundary_bars
        for prints in boundary_funding
    )

    for lookback_bars, funding_prints in sorted(
        configs,
        key=lambda values: (
            -1 if values[0] is None else values[0],
            -1 if values[1] is None else values[1],
        ),
    ):
        config = _config(
            lookback_bars=lookback_bars,
            funding_prints=funding_prints,
        )
        for turn in range(pack.bars_total):
            result = build_observation(
                pack,
                config,
                episode_id="ep_1501000000000001",
                turn=turn,
                account=AccountState(
                    cash_micro=10_000_000_000,
                    realized_pnl_micro=0,
                ),
            )
            result.assert_no_future_sources()
            result.assert_single_mark_invariant()
            assert not (set(_integers(result.document)) & SENTINELS)


def test_adversarial_solve_for_mark_recovers_only_available_provenance() -> None:
    """ISO-1 ADV: derived account values cannot smuggle a future mark."""

    pack = _pack()
    market_data = pack.market("BTC")
    entry_px_ticks = 90_000
    result = build_observation(
        pack,
        _config(lookback_bars=20, funding_prints=20),
        episode_id="ep_1501000000000002",
        turn=3,
        account=AccountState(
            cash_micro=9_000_000_000,
            realized_pnl_micro=0,
        ),
        positions={
            "BTC": PositionState(
                # Exactly one base unit makes inversion exact:
                # uPnL = (mark - entry) * tick_size_micro.
                qty_base_1e8=100_000_000,
                entry_px_ticks=entry_px_ticks,
                margin_micro=1_000_000_000,
                liq_px_ticks=80_000,
            )
        },
    )
    markets = cast(dict[str, object], result.document["markets"])
    market = cast(dict[str, object], markets["BTC"])
    positions = cast(dict[str, object], result.document["position"])
    position = cast(dict[str, object], positions["BTC"])
    solved_mark = entry_px_ticks + (
        cast(int, position["upnl_micro"])
        // cast(int, market["tick_size_micro"])
    )
    provenance = result.provenance["BTC"].mark_row
    stored_mark = market_data.mark[provenance.row_index]

    assert solved_mark == market["last_mark_px_ticks"]
    assert solved_mark == stored_mark.c
    assert provenance.available_at <= result.real_clock_ts
    assert solved_mark not in SENTINELS
    result.assert_no_future_sources()
    result.assert_single_mark_invariant()


def test_effective_lookback_operator_override_wins() -> None:
    pack = _pack()
    result = build_observation(
        pack,
        _config(lookback_bars=1, funding_prints=0),
        episode_id="ep_fedcba9876543210",
        turn=pack.bars_total - 1,
        account=AccountState(
            cash_micro=10_000_000_000,
            realized_pnl_micro=0,
        ),
    )
    markets = cast(dict[str, object], result.document["markets"])
    market = cast(dict[str, object], markets["BTC"])
    funding = cast(dict[str, object], market["funding"])
    assert len(cast(list[object], market["bars"])) == 1
    assert funding["prints"] == []
