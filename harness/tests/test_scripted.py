# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.protocol import DecisionTimeout
from harness.scripted import ConstantTargetAgent, MomentumAgent, ScriptedFixtureAgent

ROOT = Path(__file__).resolve().parents[2]


def _request(turn: int, closes: list[int] | None = None) -> dict[str, object]:
    bars = [
        {
            "ts": index,
            "available_at": index + 1,
            "o": close,
            "h": close,
            "l": close,
            "c": close,
            "v_base_1e8": 1,
        }
        for index, close in enumerate(closes or [100, 101])
    ]
    return {
        "schema": "runner_request/v1",
        "attempt": 1,
        "observation": {
            "episode": {"turn": turn},
            "markets": {"BTC": {"bars": bars}},
        },
        "retry": None,
    }


def test_fixture_agent_returns_exact_bytes_and_repeats_on_retry() -> None:
    agent = ScriptedFixtureAgent.from_path(ROOT / "fixtures/golden-mini/actions.jsonl")
    first = agent.decide(_request(0)).body
    retry_request = _request(0)
    retry_request["attempt"] = 2
    assert agent.decide(retry_request).body == first
    assert json.loads(first)["target"]["BTC"] == "2"


def test_fixture_agent_models_timeout() -> None:
    agent = ScriptedFixtureAgent.from_path(ROOT / "fixtures/golden-mini/actions.jsonl")
    with pytest.raises(DecisionTimeout):
        agent.decide(_request(1))


@pytest.mark.parametrize(
    ("target_lev_1e4", "expected"),
    [(10_000, "1"), (-10_000, "-1"), (0, "0"), (25_000, "2.5"), (-5_000, "-0.5")],
)
def test_constant_target_agent_restates_one_target(
    target_lev_1e4: int, expected: str
) -> None:
    agent = ConstantTargetAgent(target_lev_1e4=target_lev_1e4)
    first = agent.decide(_request(0)).body
    later = agent.decide(_request(7, [200, 150])).body
    assert first == later
    document = json.loads(first)
    assert document["schema"] == "action/v1"
    assert document["target"]["BTC"] == expected


def test_constant_target_agent_rejects_request_without_observation() -> None:
    agent = ConstantTargetAgent(target_lev_1e4=10_000)
    with pytest.raises(ValueError):
        agent.decide({"schema": "runner_request/v1"})


@pytest.mark.parametrize(
    ("closes", "expected"),
    [([100, 101], "1"), ([101, 100], "-1"), ([100, 100], "0"), ([100], "0")],
)
def test_momentum_agent_is_deterministic(closes: list[int], expected: str) -> None:
    agent = MomentumAgent()
    first = agent.decide(_request(0, closes)).body
    second = agent.decide(_request(0, closes)).body
    assert first == second
    assert json.loads(first)["target"]["BTC"] == expected

