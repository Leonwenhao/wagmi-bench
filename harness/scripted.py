# SPDX-License-Identifier: Apache-2.0
"""Deterministic, keyless reference agents.

``ScriptedFixtureAgent`` consumes the fixture-local ``scripted_turn/v1``
format used by the frozen golden oracle. ``MomentumAgent`` is the product
quickstart agent: it uses only the observation passed through IC-6 and never
reads pack files or wall time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harness.protocol import AgentReply, DecisionTimeout
from spec.canonical import canonical_bytes


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected an object")
        rows.append(cast(dict[str, object], value))
    return rows


@dataclass
class ScriptedFixtureAgent:
    """Replay a fixture-local scripted action sequence exactly."""

    turns: dict[int, dict[str, object]]

    @classmethod
    def from_path(cls, path: Path) -> ScriptedFixtureAgent:
        turns: dict[int, dict[str, object]] = {}
        for row in _load_jsonl(path):
            turn = row.get("turn")
            if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
                raise ValueError(f"{path}: scripted row has invalid turn {turn!r}")
            if turn in turns:
                raise ValueError(f"{path}: duplicate scripted turn {turn}")
            turns[turn] = row
        return cls(turns)

    def decide(self, request: dict[str, object]) -> AgentReply:
        observation = request.get("observation")
        if not isinstance(observation, dict):
            raise ValueError("runner request has no observation object")
        episode = observation.get("episode")
        if not isinstance(episode, dict):
            raise ValueError("observation has no episode object")
        turn = episode.get("turn")
        if isinstance(turn, bool) or not isinstance(turn, int):
            raise ValueError("observation episode.turn is not an integer")
        row = self.turns.get(turn)
        if row is None:
            raise DecisionTimeout(f"no scripted response for turn {turn}")
        mode = row.get("mode")
        if mode == "timeout":
            raise DecisionTimeout(f"scripted timeout at turn {turn}")
        if mode != "respond":
            raise ValueError(f"turn {turn}: unknown scripted mode {mode!r}")
        body = row.get("body")
        if not isinstance(body, str):
            raise ValueError(f"turn {turn}: respond row has no body string")
        # The same bytes are intentionally returned on IC-6 attempt 2.
        return AgentReply(body=body.encode("utf-8"))


def _leverage_string(target_lev_1e4: int) -> str:
    sign = "-" if target_lev_1e4 < 0 else ""
    whole, fraction = divmod(abs(target_lev_1e4), 10000)
    leverage = f"{sign}{whole}"
    if fraction:
        leverage += "." + f"{fraction:04d}".rstrip("0")
    return leverage


@dataclass(frozen=True)
class ConstantTargetAgent:
    """Deterministic baseline that restates one fixed leverage target.

    Restating the same target every turn is idempotent under IC-6 leverage
    semantics: the engine fills only the delta, so the position is opened on
    the first decidable turn and held. ``target_lev_1e4`` of 10000, -10000,
    and 0 give the buy-and-hold, short-and-hold, and flat baselines.
    """

    target_lev_1e4: int
    market: str = "BTC"

    def decide(self, request: dict[str, object]) -> AgentReply:
        observation = request.get("observation")
        if not isinstance(observation, dict):
            raise ValueError("runner request has no observation object")
        action = {
            "schema": "action/v1",
            "intent_kind": "leverage_target",
            "target": {self.market: _leverage_string(self.target_lev_1e4)},
            "comment": "constant-target baseline",
        }
        return AgentReply(body=canonical_bytes(action))


@dataclass(frozen=True)
class MomentumAgent:
    """Small deterministic reference policy for CI and the quickstart.

    It compares the last two visible trade closes. An up move targets the
    configured long leverage, a down move targets the same short leverage,
    and insufficient/flat history targets flat. The policy deliberately has
    no persistent state, hidden data, or randomness.
    """

    leverage_lev_1e4: int = 10000
    market: str = "BTC"

    def decide(self, request: dict[str, object]) -> AgentReply:
        observation = request.get("observation")
        if not isinstance(observation, dict):
            raise ValueError("runner request has no observation object")
        markets = observation.get("markets")
        if not isinstance(markets, dict):
            raise ValueError("observation has no markets object")
        market_view = markets.get(self.market)
        if not isinstance(market_view, dict):
            raise ValueError(f"observation has no market {self.market!r}")
        bars = market_view.get("bars")
        if not isinstance(bars, list):
            raise ValueError("market view has no bars array")

        target = 0
        if len(bars) >= 2:
            prior = bars[-2]
            current = bars[-1]
            if not isinstance(prior, dict) or not isinstance(current, dict):
                raise ValueError("observation bars must be objects")
            prior_close = prior.get("c")
            current_close = current.get("c")
            if (
                isinstance(prior_close, bool)
                or isinstance(current_close, bool)
                or not isinstance(prior_close, int)
                or not isinstance(current_close, int)
            ):
                raise ValueError("observation bar closes must be integers")
            if current_close > prior_close:
                target = self.leverage_lev_1e4
            elif current_close < prior_close:
                target = -self.leverage_lev_1e4

        action = {
            "schema": "action/v1",
            "intent_kind": "leverage_target",
            "target": {self.market: _leverage_string(target)},
            "comment": "deterministic two-bar momentum reference",
        }
        return AgentReply(body=canonical_bytes(action))

