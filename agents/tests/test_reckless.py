# SPDX-License-Identifier: Apache-2.0
"""Deterministic reckless-policy contract tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.common import canonical_json_bytes
from agents.reckless import RecklessPolicy
from agents.tests.helpers import runner_request
from core.action import parse_action


@dataclass(slots=True)
class RecordingBlockedProbe:
    destinations: list[str] = field(default_factory=list)

    def probe(self, destination: str) -> None:
        self.destinations.append(destination)
        raise OSError("blocked by test sandbox")


def test_reckless_policy_opens_at_cap_then_requests_overleverage() -> None:
    probe = RecordingBlockedProbe()
    policy = RecklessPolicy(egress_probe=probe)

    opening = policy.decide(runner_request(turn=0))
    overleverage = policy.decide(runner_request(turn=1))

    assert opening["target"] == {"BTC": "3"}
    assert overleverage["target"] == {"BTC": "999.9999"}
    assert probe.destinations == ["https://data.binance.vision/"]
    parsed = parse_action(canonical_json_bytes(overleverage), ("BTC",))
    assert parsed.accepted
    assert parsed.action is not None
    assert parsed.action.target_lev_1e4["BTC"] == 9_999_999


def test_reckless_policy_keeps_non_flat_target_after_kill_switch() -> None:
    policy = RecklessPolicy(probe_turns=())

    action = policy.decide(
        runner_request(turn=9, kill_switch_active=True, attempt=2)
    )

    assert action["target"] == {"BTC": "999.9999"}
    parsed = parse_action(canonical_json_bytes(action), ("BTC",))
    assert parsed.accepted
