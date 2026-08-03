# SPDX-License-Identifier: Apache-2.0
"""MATH-7 coverage for the venue cancellation rules and their labels.

The engine rejects a child order for three distinct venue reasons, and the
recorded ``OrderCancelled.reason`` is the only place that distinction survives
into the evidence bundle.  Two of those branches -- participation-cap overage
and min-notional -- are adjacent lines in the same ``elif`` ladder, so a
deleted min-notional check or a swapped pair of labels changes nothing an
aggregate assertion can see.  These tests bind each branch to a scenario whose
arithmetic is hand-checkable against the golden pack constants:

* qty step 0.001 BTC (``100_000`` in 1e-8 units), tick size 10_000 micro,
  min notional 10_000_000 micro, half spread 5 bp, impact disabled;
* a one-step order at a 1_000_000-tick open is worth exactly the min notional
  before costs, so the worst-side half spread decides the outcome: a buy fills
  at 10_005_000 micro and a sell is rejected at 9_995_000 micro.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from core.config import EpisodeConfig
from core.engine import EpisodeResult, run_episode
from core.math import calculate_fill
from core.models import FillCalculation, Side
from core.pack import BarRow, MarketSpec
from harness.protocol import AgentReply
from spec.canonical import canonical_bytes

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PACK = ROOT / "fixtures" / "golden-mini" / "pack"

# Golden-mini BTC constants, restated for readability of the expectations.
QTY_STEP_BASE_1E8 = 100_000
MIN_NOTIONAL_MICRO = 10_000_000
ONE_STEP_BUY_NOTIONAL_MICRO = 10_005_000
ONE_STEP_SELL_NOTIONAL_MICRO = 9_995_000


def _target_body(leverage_lev_1e4: int) -> bytes:
    sign = "-" if leverage_lev_1e4 < 0 else ""
    magnitude = abs(leverage_lev_1e4)
    whole, fraction = divmod(magnitude, 10_000)
    text = f"{sign}{whole}"
    if fraction:
        text += "." + f"{fraction:04d}".rstrip("0")
    return canonical_bytes({"schema": "action/v1", "target": {"BTC": text}})


class _SequenceAgent:
    """Replay a fixed leverage schedule, then flatten."""

    def __init__(self, targets: tuple[int, ...]) -> None:
        self._targets = targets

    def decide(self, request: dict[str, object]) -> AgentReply:
        observation = cast(dict[str, object], request["observation"])
        episode = cast(dict[str, object], observation["episode"])
        turn = cast(int, episode["turn"])
        target = self._targets[turn] if turn < len(self._targets) else 0
        return AgentReply(_target_body(target))


def _run(pack_dir: Path, targets: tuple[int, ...]) -> EpisodeResult:
    return run_episode(
        pack_dir=pack_dir,
        agent=_SequenceAgent(targets),
        config=EpisodeConfig(),
        run_id="run_" + "0" * 16,
        episode_id="ep_" + "0" * 16,
    )


def _synthetic_pack(
    destination: Path,
    *,
    volume_base_1e8: int | None = None,
    execution_patch: Mapping[str, object] | None = None,
) -> Path:
    """Copy the golden pack into ``destination`` and restate its digests.

    The frozen fixture is never written; only the copy is patched, and the
    manifest byte counts, record counts, and SHA-256 digests are recomputed so
    the copy still loads under mandatory integrity verification.
    """

    shutil.copytree(GOLDEN_PACK, destination)
    if volume_base_1e8 is not None:
        bars = destination / "bars_1h.jsonl"
        rewritten: list[str] = []
        for line in bars.read_text(encoding="utf-8").splitlines():
            row = cast(dict[str, object], json.loads(line))
            row["v_base_1e8"] = volume_base_1e8
            rewritten.append(
                json.dumps(row, sort_keys=True, separators=(",", ":"))
            )
        bars.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    manifest_path = destination / "manifest.json"
    manifest = cast(
        dict[str, object],
        json.loads(manifest_path.read_text(encoding="utf-8")),
    )
    if execution_patch is not None:
        markets = cast(dict[str, object], manifest["markets"])
        market = cast(dict[str, object], markets["BTC"])
        cast(dict[str, object], market["execution"]).update(execution_patch)
    for raw_entry in cast(list[object], manifest["files"]):
        entry = cast(dict[str, object], raw_entry)
        raw = (destination / cast(str, entry["path"])).read_bytes()
        entry["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        entry["bytes"] = len(raw)
        entry["records"] = len(raw.splitlines())
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return destination


def _events(result: EpisodeResult, event_type: str) -> list[dict[str, object]]:
    return [event for event in result.events if event["type"] == event_type]


def _payloads(result: EpisodeResult, event_type: str) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], event["payload"])
        for event in _events(result, event_type)
    ]


def _counterfactual_fill(
    spec: MarketSpec,
    bar: BarRow,
    *,
    side: Side,
    requested_qty_base_1e8: int,
) -> FillCalculation:
    """Recompute the fill the engine saw, independently of its branching."""

    return calculate_fill(
        side=side,
        requested_qty_base_1e8=requested_qty_base_1e8,
        bar_volume_base_1e8=bar.v_base_1e8,
        participation_cap_1e8=spec.participation_cap_1e8,
        qty_step_base_1e8=spec.qty_step_base_1e8,
        ref_px_ticks=bar.o,
        tick_size_micro=spec.tick_size_micro,
        half_spread_1e8=spec.half_spread_1e8,
        impact_coeff_1e8=spec.impact_coeff_1e8,
        impact_model=spec.impact_model,
        taker_fee_rate_1e8=spec.taker_fee_rate_1e8,
        cost_multiplier_1e4=spec.cost_profile_multipliers_1e4["primary"],
    )


def _position_qty(result: EpisodeResult, bar_index: int) -> int:
    row = next(
        row for row in result.ledger_primary if row["bar_index"] == bar_index
    )
    positions = cast(dict[str, object], row["positions"])
    return cast(int, cast(dict[str, object], positions["BTC"])["qty_base_1e8"])


# Turn 0 opens 0.003 BTC, turn 1 trims to 0.002 BTC, and turn 2 asks to trim
# one further step on the bar that reopens at 1_000_000 ticks: that child sell
# is worth 9_995_000 micro after the worst-side half spread, five thousand
# micro short of the venue minimum.
_RESIZE_TARGETS = (30, 30, 15)


def test_resize_child_order_below_min_notional_is_cancelled() -> None:
    result = _run(GOLDEN_PACK, _RESIZE_TARGETS)
    cancels = _events(result, "OrderCancelled")
    assert len(cancels) == 1
    cancelled = cancels[0]
    assert cancelled["turn"] == 2
    assert cancelled["bar_index"] == 3
    assert cancelled["source"] == "engine"
    payload = cast(dict[str, object], cancelled["payload"])
    assert payload == {
        "market": "BTC",
        "reason": "min_notional",
        "requested_qty_base_1e8": QTY_STEP_BASE_1E8,
        "cancelled_qty_base_1e8": QTY_STEP_BASE_1E8,
        "detail": (
            f"min_notional: requested_qty {QTY_STEP_BASE_1E8} not executed"
        ),
    }
    assert [
        event["turn"] for event in _events(result, "OrderFilled")
    ] == [0, 1, 3]

    # The rejected resize leaves the position exactly as the prior bar left it.
    assert _position_qty(result, 2) == 200_000
    assert _position_qty(result, 3) == 200_000

    # Independently of the engine's branch order: the participation cap had
    # ample room for this order, so min notional is the only rule that can
    # have rejected it.
    market = result.pack.market("BTC")
    fill = _counterfactual_fill(
        market.spec,
        market.trade[3],
        side="sell",
        requested_qty_base_1e8=QTY_STEP_BASE_1E8,
    )
    assert fill.quantities.filled_qty_base_1e8 == QTY_STEP_BASE_1E8
    assert fill.quantities.capacity_qty_base_1e8 >= QTY_STEP_BASE_1E8
    assert fill.notional_micro == ONE_STEP_SELL_NOTIONAL_MICRO
    assert market.spec.min_notional_micro == MIN_NOTIONAL_MICRO
    assert fill.notional_micro < market.spec.min_notional_micro


def test_participation_cap_cancellation_carries_participation_reason(
    tmp_path: Path,
) -> None:
    # 0.00999999 BTC of bar volume: a 10% participation cap floors to less
    # than one quantity step, so nothing at all can be filled.
    pack = _synthetic_pack(tmp_path / "pack", volume_base_1e8=999_999)
    result = _run(pack, (10,))
    cancels = _events(result, "OrderCancelled")
    assert len(cancels) == 1
    cancelled = cancels[0]
    assert cancelled["turn"] == 0
    assert cancelled["bar_index"] == 1
    payload = cast(dict[str, object], cancelled["payload"])
    assert payload == {
        "market": "BTC",
        "reason": "participation_cap",
        "requested_qty_base_1e8": QTY_STEP_BASE_1E8,
        "cancelled_qty_base_1e8": QTY_STEP_BASE_1E8,
        "detail": (
            f"participation_cap: requested_qty {QTY_STEP_BASE_1E8} "
            "not executed"
        ),
    }
    assert _events(result, "OrderFilled") == []

    market = result.pack.market("BTC")
    fill = _counterfactual_fill(
        market.spec,
        market.trade[1],
        side="buy",
        requested_qty_base_1e8=QTY_STEP_BASE_1E8,
    )
    assert fill.quantities.capacity_qty_base_1e8 == 0
    assert fill.quantities.filled_qty_base_1e8 == 0


def test_min_notional_and_participation_reasons_stay_distinct_in_evidence(
    tmp_path: Path,
) -> None:
    min_notional_run = _run(GOLDEN_PACK, _RESIZE_TARGETS)
    participation_run = _run(
        _synthetic_pack(tmp_path / "pack", volume_base_1e8=999_999),
        (10,),
    )
    min_notional_payload = _payloads(min_notional_run, "OrderCancelled")[0]
    participation_payload = _payloads(participation_run, "OrderCancelled")[0]

    min_notional_reason = cast(str, min_notional_payload["reason"])
    participation_reason = cast(str, participation_payload["reason"])
    assert min_notional_reason != participation_reason
    assert min_notional_reason == "min_notional"
    assert participation_reason == "participation_cap"
    assert min_notional_payload["detail"] != participation_payload["detail"]
    assert cast(str, min_notional_payload["detail"]).startswith(
        "min_notional:"
    )
    assert cast(str, participation_payload["detail"]).startswith(
        "participation_cap:"
    )


def test_order_above_min_notional_boundary_executes() -> None:
    # One step bought at a 1_000_000-tick open: 10_005_000 micro, five
    # thousand micro clear of the venue minimum.
    result = _run(GOLDEN_PACK, (10,))
    assert _events(result, "OrderCancelled") == []
    opening = _payloads(result, "OrderFilled")[0]
    notional = cast(int, opening["notional_micro"])
    assert opening["side"] == "buy"
    assert opening["qty_base_1e8"] == QTY_STEP_BASE_1E8
    assert notional == ONE_STEP_BUY_NOTIONAL_MICRO
    assert notional > result.pack.market("BTC").spec.min_notional_micro


@pytest.mark.parametrize(
    ("half_spread_1e8", "expected_notional_micro", "expected_cancelled"),
    [
        # Zero half spread: the sell lands on exactly the venue minimum, and
        # the rule is a strict "below", so the order executes.
        (0, MIN_NOTIONAL_MICRO, False),
        # One tick of half spread: 9_999_990 micro, ten micro short.
        (100, MIN_NOTIONAL_MICRO - 10, True),
    ],
)
def test_min_notional_boundary_is_exact(
    tmp_path: Path,
    half_spread_1e8: int,
    expected_notional_micro: int,
    expected_cancelled: bool,
) -> None:
    pack = _synthetic_pack(
        tmp_path / "pack",
        execution_patch={"half_spread_1e8": half_spread_1e8},
    )
    result = _run(pack, (-10,))
    market = result.pack.market("BTC")
    fill = _counterfactual_fill(
        market.spec,
        market.trade[1],
        side="sell",
        requested_qty_base_1e8=QTY_STEP_BASE_1E8,
    )
    assert fill.notional_micro == expected_notional_micro

    cancels = _payloads(result, "OrderCancelled")
    fills = _payloads(result, "OrderFilled")
    if expected_cancelled:
        assert [payload["reason"] for payload in cancels] == ["min_notional"]
        assert fills == []
    else:
        assert cancels == []
        assert fills[0]["side"] == "sell"
        assert fills[0]["notional_micro"] == expected_notional_micro
