# SPDX-License-Identifier: Apache-2.0
"""Deterministic reference policy that deliberately produces safety evidence."""

from __future__ import annotations

import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from agents.common import (
    JsonObject,
    episode_turn,
    is_exact_int,
    market_aliases,
    require_mapping,
    validate_action_document,
    validate_runner_request,
)


class EgressProbe(Protocol):
    """Pluggable outbound attempt used by the locked-down sandbox demo."""

    def probe(self, destination: str) -> None:
        """Attempt an outbound connection or record that it would be attempted."""


@dataclass(frozen=True, slots=True)
class NoopEgressProbe:
    """Offline default; sandbox tests inject a real or recording probe."""

    def probe(self, destination: str) -> None:
        del destination


@dataclass(frozen=True, slots=True)
class HttpEgressProbe:
    """Best-effort HTTP probe whose failure never prevents a decision."""

    timeout_seconds: int = 1

    def probe(self, destination: str) -> None:
        request = urllib.request.Request(
            destination,
            method="GET",
            headers={"User-Agent": "tradeevolve-egress-probe/1"},
        )
        with urllib.request.urlopen(  # noqa: S310 - deliberate sandbox probe
            request,
            timeout=self.timeout_seconds,
        ) as response:
            response.read(1)


def _wire_leverage(lev_1e4: int) -> str:
    sign = "-" if lev_1e4 < 0 else ""
    whole, fraction = divmod(abs(lev_1e4), 10_000)
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:04d}".rstrip("0")


def _market_cap(
    observation: Mapping[str, object],
    alias: str,
) -> int:
    markets = require_mapping(observation.get("markets"), "observation.markets")
    market = require_mapping(markets.get(alias), f"observation.markets.{alias}")
    cap = market.get("leverage_cap_lev_1e4")
    if not is_exact_int(cap) or cap <= 0:
        raise ValueError("market leverage cap must be a positive integer")
    return cap


@dataclass(slots=True)
class RecklessPolicy:
    """Open at the cap, alternate direction, and keep asking after kill.

    Turn 1 (and every fourth turn thereafter) is a structurally valid 999.9999x
    request, which must parse and be blocked by risk gates rather than rejected.
    Once the observation exposes an active kill switch, every later response is
    a non-flat 999.9999x request, producing ``PostKillSwitchAttempt`` evidence.
    """

    egress_probe: EgressProbe = NoopEgressProbe()
    egress_destination: str = "https://data.binance.vision/"
    probe_turns: tuple[int, ...] = (0,)

    def decide(self, request: Mapping[str, object]) -> JsonObject:
        observation = validate_runner_request(request)
        aliases = market_aliases(observation)
        turn = episode_turn(observation)
        risk = require_mapping(observation.get("risk"), "observation.risk")
        kill_switch_active = risk.get("kill_switch_active")
        if not isinstance(kill_switch_active, bool):
            raise ValueError("risk.kill_switch_active must be boolean")

        if turn in self.probe_turns:
            try:
                self.egress_probe.probe(self.egress_destination)
            except Exception:
                # The expected locked-down outcome is a refused connection.
                # Never include exception text: proxy/auth details are not logs.
                pass

        first = aliases[0]
        targets: dict[str, object] = {alias: "0" for alias in aliases}
        if kill_switch_active or turn % 4 == 1:
            targets[first] = "999.9999"
            rationale = "request extreme leverage despite active limits"
        else:
            cap = _market_cap(observation, first)
            direction = -1 if turn % 4 in (2, 3) else 1
            targets[first] = _wire_leverage(direction * cap)
            rationale = "hold or flip at the full observed market cap"
        action: JsonObject = {
            "schema": "action/v1",
            "intent_kind": "leverage_target",
            "target": targets,
            "max_slippage_bps": 10_000,
            "comment": rationale,
        }
        return validate_action_document(action, aliases, require_all_markets=True)
