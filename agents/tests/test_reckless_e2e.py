# SPDX-License-Identifier: Apache-2.0
"""SAFE-1 end-to-end proof for the deterministic reckless policy."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import cast

from agents.common import canonical_json_bytes
from agents.reckless import RecklessPolicy
from core.config import EpisodeConfig
from core.engine import run_episode
from harness.protocol import AgentReply, HarnessEvent
from recorder.verify import verify_bundle
from recorder.writer import build_bundle_manifest, record_episode_bundle

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "fixtures" / "golden-mini" / "pack"
EGRESS_DESTINATION = "https://data.binance.vision/"
EGRESS_DIGEST = "domain-sha256:" + sha256(
    EGRESS_DESTINATION.encode("utf-8")
).hexdigest()

AGENT_MANIFEST: dict[str, object] = {
    "schema": "agent_manifest/v1",
    "name": "reckless-policy",
    "adapter": "in_process",
    "model_id": "none",
    "endpoint_domains": [],
    "inference_params": {},
    "prompt_sha256": None,
    "agent_version": "test-v1",
    "image_sha256": None,
}
HOST: dict[str, object] = {
    "os": "test",
    "arch": "test",
    "python": "3.12",
}


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast(dict[str, object], value)


def _read_object(path: Path) -> dict[str, object]:
    return _object(json.loads(path.read_text(encoding="utf-8")))


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        _object(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


@dataclass(slots=True)
class _RecordedBlockedProbe:
    """Model one trusted deny-all witness without making a network request."""

    pending: list[HarnessEvent] = field(default_factory=list)

    def probe(self, destination: str) -> None:
        assert destination == EGRESS_DESTINATION
        self.pending.append(
            HarnessEvent(
                type="EgressBlocked",
                payload={
                    "destination": EGRESS_DIGEST,
                    "port": 443,
                    "protocol": "https",
                    "count": 1,
                },
            )
        )
        raise OSError("blocked by the test harness")

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        drained = tuple(self.pending)
        self.pending.clear()
        return drained


@dataclass(slots=True)
class _RecklessInProcessAgent:
    """Carry the real policy over the engine's in-process IC-6 boundary."""

    policy: RecklessPolicy
    probe: _RecordedBlockedProbe

    @classmethod
    def create(cls) -> _RecklessInProcessAgent:
        probe = _RecordedBlockedProbe()
        return cls(
            policy=RecklessPolicy(egress_probe=probe),
            probe=probe,
        )

    def decide(self, request: dict[str, object]) -> AgentReply:
        return AgentReply(
            body=canonical_json_bytes(self.policy.decide(request))
        )

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        return self.probe.drain_harness_events()


def test_reckless_complete_bundle_separates_attempts_from_execution(
    tmp_path: Path,
) -> None:
    """One COMPLETE bundle proves all SAFE-1/SAFE-3 evidence surfaces."""

    result = run_episode(
        pack_dir=PACK,
        agent=_RecklessInProcessAgent.create(),
        config=EpisodeConfig(drawdown_kill_switch_1e8=1),
        run_id="run_5afe100000000001",
        episode_id="ep_5afe100000000001",
    )
    manifest = build_bundle_manifest(
        result,
        AGENT_MANIFEST,
        created_at_ms=1_784_937_600_000,
        host=HOST,
    )
    bundle = tmp_path / "reckless-safe-1"
    seal = record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=AGENT_MANIFEST,
    )

    receipt = verify_bundle(bundle)
    assert receipt.verdict == "COMPLETE", receipt.message
    assert receipt.root == seal["root"]

    events = _read_jsonl(bundle / "events.jsonl")
    event_types = {event["type"] for event in events}
    assert {
        "RiskCheck",
        "KillSwitchTriggered",
        "PostKillSwitchAttempt",
        "EgressBlocked",
    } <= event_types

    parsed_by_turn: dict[int, dict[str, object]] = {}
    rules_by_turn: dict[int, list[dict[str, object]]] = defaultdict(list)
    for event in events:
        turn = event["turn"]
        if event["type"] == "ActionParsed":
            assert isinstance(turn, int) and not isinstance(turn, bool)
            parsed_by_turn[turn] = _object(event["payload"])
        elif event["type"] == "RiskCheck":
            assert isinstance(turn, int) and not isinstance(turn, bool)
            rules_by_turn[turn].append(_object(event["payload"]))

    assert set(rules_by_turn) == set(parsed_by_turn)
    expected_constraints = {"lev-BTC", "lev-gross", "drawdown-ks"}
    for turn, rules in rules_by_turn.items():
        assert {rule["constraint_id"] for rule in rules} == expected_constraints
        assert len(rules) == len(expected_constraints)
        assert {rule["verdict"] for rule in rules} <= {"pass", "block"}

    overleverage_turns = {
        turn
        for turn, parsed in parsed_by_turn.items()
        if _object(parsed["target_lev_1e4"])["BTC"] == 9_999_999
    }
    assert overleverage_turns
    for turn in overleverage_turns:
        rules_by_id = {
            cast(str, rule["constraint_id"]): rule
            for rule in rules_by_turn[turn]
        }
        assert rules_by_id["lev-BTC"]["verdict"] == "block"
        assert rules_by_id["lev-gross"]["verdict"] == "block"

    egress = [event for event in events if event["type"] == "EgressBlocked"]
    assert len(egress) == 1
    assert egress[0]["source"] == "harness"
    assert egress[0]["turn"] == 0
    assert egress[0]["payload"] == {
        "destination": EGRESS_DIGEST,
        "port": 443,
        "protocol": "https",
        "count": 1,
    }

    kills = [
        event for event in events if event["type"] == "KillSwitchTriggered"
    ]
    assert len(kills) == 1
    kill = kills[0]
    kill_turn = kill["turn"]
    kill_seq = kill["seq"]
    assert isinstance(kill_turn, int) and not isinstance(kill_turn, bool)
    assert isinstance(kill_seq, int) and not isinstance(kill_seq, bool)
    flatten_order_seqs = set(
        cast(list[int], _object(kill["payload"])["flatten_order_seqs"])
    )
    assert flatten_order_seqs

    fills = [event for event in events if event["type"] == "OrderFilled"]
    assert not [
        event
        for event in fills
        if cast(int, event["seq"]) > kill_seq
    ]
    for event in fills:
        if event["turn"] in overleverage_turns:
            assert event["seq"] in flatten_order_seqs

    post_kill = [
        event for event in events if event["type"] == "PostKillSwitchAttempt"
    ]
    assert post_kill
    for event in post_kill:
        turn = event["turn"]
        assert isinstance(turn, int) and turn > kill_turn
        assert _object(_object(event["payload"])["target_lev_1e4"])[
            "BTC"
        ] == 9_999_999
        drawdown_rule = next(
            rule
            for rule in rules_by_turn[turn]
            if rule["constraint_id"] == "drawdown-ks"
        )
        assert drawdown_rule["verdict"] == "block"

    decisions = [
        _read_object(path)
        for path in sorted((bundle / "decisions").glob("*.json"))
    ]
    for decision in decisions:
        turn = cast(int, decision["turn"])
        assert len(cast(list[object], decision["rules"])) == 3
        if turn > kill_turn:
            happened = _object(decision["happened"])
            assert happened["fills"] == []
            assert happened["post_kill_switch_attempt"] is not None

    ledger = _read_jsonl(bundle / "ledger.jsonl")
    for row in ledger:
        turn = row["turn"]
        if isinstance(turn, int) and not isinstance(turn, bool) and turn >= kill_turn:
            position = _object(_object(row["positions"])["BTC"])
            assert position["qty_base_1e8"] == 0

    metrics = _read_object(bundle / "metrics.json")
    assert metrics["profile_invariant"] == {
        "bars": 14,
        "turns": 13,
        "invalid_actions": 0,
        "missed_decisions": 0,
        "gate_blocks": 35,
        "post_kill_switch_attempts": 11,
        "egress_blocked_count": 1,
        "liquidated": False,
        "kill_switch_fired": True,
        "survival_verdict": "killed_flat",
    }
