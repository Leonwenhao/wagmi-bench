# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator

from core.config import EpisodeConfig
from core.engine import EpisodeResult, run_episode
from harness.protocol import AgentReply, DecisionTimeout, HarnessEvent
from harness.scripted import MomentumAgent
from spec.canonical import canonical_bytes, sha256_prefixed

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "fixtures" / "golden-mini"
LEAKAGE = ROOT / "fixtures" / "leakage-probe"
EVENT_SCHEMA = json.loads(
    (ROOT / "spec/schemas/event.v1.schema.json").read_text(encoding="utf-8")
)


def _target_body(leverage_lev_1e4: int) -> bytes:
    sign = "-" if leverage_lev_1e4 < 0 else ""
    magnitude = abs(leverage_lev_1e4)
    whole, fraction = divmod(magnitude, 10_000)
    text = f"{sign}{whole}"
    if fraction:
        text += "." + f"{fraction:04d}".rstrip("0")
    return canonical_bytes(
        {
            "schema": "action/v1",
            "target": {"BTC": text},
        }
    )


@dataclass
class _SequenceAgent:
    targets: list[int]

    def decide(self, request: dict[str, object]) -> AgentReply:
        observation = cast(dict[str, object], request["observation"])
        episode = cast(dict[str, object], observation["episode"])
        turn = cast(int, episode["turn"])
        target = self.targets[turn] if turn < len(self.targets) else 0
        return AgentReply(_target_body(target))


class _PartialUsageAgent:
    def decide(self, request: dict[str, object]) -> AgentReply:
        del request
        return AgentReply(
            canonical_bytes(
                {
                    "schema": "action/v1",
                    "target": {"BTC": "0"},
                    "usage": {"input_tokens": 1},
                }
            )
        )


class _InvalidThenTimeoutAgent:
    def decide(self, request: dict[str, object]) -> AgentReply:
        attempt = request["attempt"]
        if attempt == 1:
            return AgentReply(b"not-json")
        raise DecisionTimeout("second attempt timed out")


@dataclass
class _FourXXValidActionAgent:
    requests: list[dict[str, object]] = field(default_factory=list)

    def decide(self, request: dict[str, object]) -> AgentReply:
        self.requests.append(request)
        observation = cast(dict[str, object], request["observation"])
        episode = cast(dict[str, object], observation["episode"])
        turn = cast(int, episode["turn"])
        attempt = cast(int, request["attempt"])
        status = 422 if turn == 0 and attempt == 1 else 200
        return AgentReply(
            _target_body(0),
            http_status=status,
            transport="http",
        )


@dataclass
class _EgressEvidenceAgent:
    _pending: tuple[HarnessEvent, ...] = ()
    _attempt_drain_due: bool = False
    _final_sent: bool = False

    def decide(self, request: dict[str, object]) -> AgentReply:
        observation = cast(dict[str, object], request["observation"])
        episode = cast(dict[str, object], observation["episode"])
        turn = cast(int, episode["turn"])
        attempt = cast(int, request["attempt"])
        self._attempt_drain_due = True
        if turn == 0 and attempt == 1:
            self._pending = (
                HarnessEvent(
                    type="EgressBlocked",
                    payload={
                        "destination": "z-sha256:2222",
                        "port": 443,
                        "protocol": "https",
                        "count": 1,
                    },
                ),
                HarnessEvent(
                    type="EgressBlocked",
                    payload={
                        "destination": "a-sha256:1111",
                        "port": None,
                        "protocol": "dns",
                        "count": 2,
                    },
                ),
            )
            return AgentReply(b"{")
        if turn == 0 and attempt == 2:
            self._pending = (
                HarnessEvent(
                    type="EgressBlocked",
                    payload={
                        "destination": "z-sha256:2222",
                        "port": 443,
                        "protocol": "https",
                        "count": 4,
                    },
                ),
                HarnessEvent(
                    type="EgressBlocked",
                    payload={
                        "destination": "a-sha256:1111",
                        "port": None,
                        "protocol": "dns",
                        "count": 3,
                    },
                ),
            )
        else:
            self._pending = ()
        return AgentReply(_target_body(0))

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        if self._attempt_drain_due:
            self._attempt_drain_due = False
            pending = self._pending
            self._pending = ()
            return pending
        if self._final_sent:
            return ()
        self._final_sent = True
        return (
            HarnessEvent(
                type="EgressBlocked",
                payload={
                    "destination": "final-sha256:3333",
                    "port": 9,
                    "protocol": "tcp",
                    "count": 7,
                },
            ),
        )


def _run(
    agent: object,
    *,
    config: EpisodeConfig | None = None,
    pack: Path | None = None,
) -> EpisodeResult:
    return run_episode(
        pack_dir=pack or GOLDEN / "pack",
        agent=cast(MomentumAgent, agent),
        config=config or EpisodeConfig(),
        run_id="run_1111111111111111",
        episode_id="ep_1111111111111111",
    )


def test_partial_close_to_flat_retains_a_valid_margin_basis() -> None:
    # This sequence repeatedly asks to cross or flatten through the golden
    # pack's participation cap.  A partial target=0 close must not leave a
    # residual lot with zero leverage/margin basis.
    targets = [
        -10_000,
        30_000,
        0,
        -30_000,
        10_000,
        -20_000,
        20_000,
        -10_000,
        30_000,
        0,
        -30_000,
        10_000,
        -20_000,
    ]
    result = _run(_SequenceAgent(targets))
    assert result.ledger_primary
    assert all(
        cast(int, row["nav_micro"])
        == cast(int, row["cash_micro"])
        + cast(dict[str, dict[str, int]], row["positions"])["BTC"]["upnl_micro"]
        for row in result.ledger_primary
    )


def test_sub_step_target_is_recorded_as_qty_rounding() -> None:
    result = _run(
        _SequenceAgent([500]),
        config=EpisodeConfig(starting_nav_micro=1_000_000),
    )
    cancels = [
        event
        for event in result.events
        if event["type"] == "OrderCancelled"
        and cast(dict[str, object], event["payload"])["reason"]
        == "qty_rounding"
    ]
    assert len(cancels) == 1
    payload = cast(dict[str, object], cancels[0]["payload"])
    assert cast(int, payload["requested_qty_base_1e8"]) > 0
    assert payload["requested_qty_base_1e8"] == payload["cancelled_qty_base_1e8"]


def test_kill_switch_arms_while_flat_and_records_later_attempt() -> None:
    result = _run(
        _SequenceAgent([10_000, 0, 10_000, 0, 10_000]),
        config=EpisodeConfig(drawdown_kill_switch_1e8=200_000),
    )
    kill = [event for event in result.events if event["type"] == "KillSwitchTriggered"]
    attempts = [
        event for event in result.events if event["type"] == "PostKillSwitchAttempt"
    ]
    assert len(kill) == 1
    assert kill[0]["turn"] == 3
    assert cast(dict[str, object], kill[0]["payload"])["flatten_order_seqs"] == []
    assert [event["turn"] for event in attempts] == [4]
    assert result.metrics["profile_invariant"] == {
        "bars": 14,
        "turns": 13,
        "invalid_actions": 0,
        "missed_decisions": 0,
        "gate_blocks": 1,
        "post_kill_switch_attempts": 1,
        "egress_blocked_count": 0,
        "liquidated": False,
        "kill_switch_fired": True,
        "survival_verdict": "killed_flat",
    }


def test_forced_kill_flatten_precedes_flat_margin_and_kill_event() -> None:
    result = _run(
        _SequenceAgent([10_000, 0, 10_000]),
        config=EpisodeConfig(drawdown_kill_switch_1e8=100_000),
    )
    kill = next(
        event for event in result.events if event["type"] == "KillSwitchTriggered"
    )
    payload = cast(dict[str, object], kill["payload"])
    flatten_seq = cast(list[int], payload["flatten_order_seqs"])[0]
    between = [
        event
        for event in result.events
        if flatten_seq < cast(int, event["seq"]) < cast(int, kill["seq"])
    ]
    assert any(
        event["type"] == "MarginUpdate"
        and cast(dict[str, object], event["payload"])["position_qty_base_1e8"] == 0
        for event in between
    )


def test_partial_usage_is_normalized_to_schema_valid_null_telemetry() -> None:
    result = _run(_PartialUsageAgent())
    responded = [
        event for event in result.events if event["type"] == "AgentResponded"
    ]
    assert responded
    assert all(
        cast(dict[str, object], event["payload"])["token_usage"] is None
        for event in responded
    )
    validator = Draft202012Validator(EVENT_SCHEMA)
    assert not [
        error
        for event in result.events
        for error in validator.iter_errors(event)
    ]


def test_retry_timeout_records_both_attempts_and_feedback() -> None:
    result = _run(_InvalidThenTimeoutAgent())
    rejected = [
        event for event in result.events if event["type"] == "ActionRejected"
    ]
    assert rejected
    payload = cast(dict[str, object], rejected[0]["payload"])
    assert payload["reason"] == "timeout"
    assert payload["attempts"] == 2
    assert isinstance(payload["validator_error"], str)


def test_http_4xx_valid_body_is_evidence_then_forced_malformed_retry() -> None:
    agent = _FourXXValidActionAgent()
    valid_body = _target_body(0)

    result = _run(agent)

    first_two_requests = agent.requests[:2]
    assert [request["attempt"] for request in first_two_requests] == [1, 2]
    retry = cast(dict[str, object], first_two_requests[1]["retry"])
    assert retry == {
        "reason": "schema_invalid",
        "detail": "response does not validate against action/v1",
        "prior_raw_sha256": sha256_prefixed(valid_body),
    }
    turn_zero = [event for event in result.events if event["turn"] == 0]
    responded = [
        event for event in turn_zero if event["type"] == "AgentResponded"
    ]
    assert [
        cast(dict[str, object], event["payload"])["http_status"]
        for event in responded
    ] == [422, 200]
    assert result.raw_blobs["raw/0000-a1.txt"] == valid_body
    assert result.raw_blobs["raw/0000-a2.txt"] == valid_body
    parsed = next(
        event for event in turn_zero if event["type"] == "ActionParsed"
    )
    assert cast(dict[str, object], parsed["payload"])["from_attempt"] == 2


def test_egress_blocks_are_coalesced_ordered_counted_and_finally_drained() -> None:
    result = _run(_EgressEvidenceAgent())

    turn_zero = [event for event in result.events if event["turn"] == 0]
    lifecycle = [
        event["type"]
        for event in turn_zero
        if event["type"]
        in {
            "ObservationEmitted",
            "AgentResponded",
            "EgressBlocked",
            "ActionParsed",
        }
    ]
    assert lifecycle == [
        "ObservationEmitted",
        "AgentResponded",
        "AgentResponded",
        "EgressBlocked",
        "EgressBlocked",
        "ActionParsed",
    ]
    blocked = [
        event for event in turn_zero if event["type"] == "EgressBlocked"
    ]
    assert [event["source"] for event in blocked] == ["harness", "harness"]
    assert len({cast(int, event["ts"]) for event in blocked}) == 1
    assert [
        cast(dict[str, object], event["payload"])
        for event in blocked
    ] == [
        {
            "destination": "a-sha256:1111",
            "port": None,
            "protocol": "dns",
            "count": 5,
        },
        {
            "destination": "z-sha256:2222",
            "port": 443,
            "protocol": "https",
            "count": 5,
        },
    ]
    assert [event["type"] for event in result.events[-2:]] == [
        "EgressBlocked",
        "EpisodeEnd",
    ]
    final_block = result.events[-2]
    assert final_block["turn"] is None
    assert final_block["bar_index"] is None
    assert final_block["source"] == "harness"
    assert cast(dict[str, object], final_block["payload"])["count"] == 7
    invariant = cast(dict[str, object], result.metrics["profile_invariant"])
    assert invariant["egress_blocked_count"] == 17
    validator = Draft202012Validator(EVENT_SCHEMA)
    assert not [
        error
        for event in result.events
        for error in validator.iter_errors(event)
    ]


def test_poisoned_available_at_cannot_move_full_engine_time() -> None:
    result = _run(MomentumAgent(), pack=LEAKAGE)
    event_times = [cast(int, event["ts"]) for event in result.events]
    assert event_times == sorted(event_times)
    assert all(
        row["ts"]
        == result.pack.window_start_ts
        + (cast(int, row["bar_index"]) + result.pack.warmup_bars + 1)
        * result.pack.decision_bar_ms
        for row in result.ledger_primary
    )


def test_deterministic_generated_action_sequences_preserve_ledger_invariants() -> None:
    # Merge CI runs a bounded sample.  Nightly CI sets this to 10000, the
    # explicit MATH-2 acceptance volume.
    #
    # MATH-2 requires three ledger invariants, all in integer micro-units and
    # all exact, on every row of both cost profiles:
    #   1. the delta-NAV attribution closes -- d_nav equals the sum of the four
    #      stored attribution terms;
    #   2. NAV reconciles to state -- nav equals free cash plus the marked
    #      unrealized PnL of every open position;
    #   3. cash is conserved to the unit -- cash moves only by realized
    #      components, so cash equals the starting NAV plus cumulative realized
    #      PnL, bar by bar as well as in total.
    episodes = int(os.environ.get("TRADEVOLVE_FUZZ_EPISODES", "100"))
    assert episodes >= 1
    config = EpisodeConfig()
    choices = (-30_000, -20_000, -10_000, 0, 10_000, 20_000, 30_000)
    for seed in range(episodes):
        rng = random.Random(seed)
        targets = [rng.choice(choices) for _ in range(13)]
        result = run_episode(
            pack_dir=GOLDEN / "pack",
            agent=_SequenceAgent(targets),
            config=config,
            run_id=f"run_{seed:016x}",
            episode_id=f"ep_{seed:016x}",
        )
        for rows in (result.ledger_primary, result.ledger_stress_2x):
            assert rows
            previous: dict[str, object] | None = None
            for row in rows:
                assert row["d_nav_micro"] == (
                    cast(int, row["d_price_pnl_micro"])
                    + cast(int, row["d_funding_micro"])
                    + cast(int, row["d_fees_micro"])
                    + cast(int, row["d_liq_penalty_micro"])
                )
                positions = cast(dict[str, object], row["positions"])
                unrealized = sum(
                    cast(int, cast(dict[str, object], position)["upnl_micro"])
                    for position in positions.values()
                )
                cash = cast(int, row["cash_micro"])
                realized = cast(int, row["realized_pnl_micro"])
                assert row["nav_micro"] == cash + unrealized
                assert cash == config.starting_nav_micro + realized
                if previous is None:
                    assert cash == config.starting_nav_micro
                    assert realized == 0
                else:
                    assert cash - cast(int, previous["cash_micro"]) == (
                        realized - cast(int, previous["realized_pnl_micro"])
                    )
                previous = row
