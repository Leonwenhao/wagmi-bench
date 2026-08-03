# SPDX-License-Identifier: Apache-2.0
"""Typed, deterministic in-process side of the frozen IC-6 runner contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Protocol, runtime_checkable


class DecisionTimeout(TimeoutError):
    """The agent produced no bytes before the configured deadline."""


@dataclass(frozen=True)
class AgentReply:
    """One verbatim agent response and its non-economic telemetry."""

    body: bytes
    latency_ms: int = 0
    http_status: int | None = None
    transport: str = "in_process"


@dataclass(frozen=True, slots=True)
class HarnessEvent:
    """One unsequenced trusted-harness fact observed during an attempt.

    The event intentionally carries neither a timestamp nor a turn.  Those
    values belong to the deterministic episode clock and are assigned by the
    engine when it drains the optional source.
    """

    type: Literal["EgressBlocked"]
    payload: Mapping[str, object]


@runtime_checkable
class HarnessEventSource(Protocol):
    """Optional side channel implemented by adapters with harness evidence."""

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        """Remove and return all facts observed since the previous drain."""


class InProcessAgent(Protocol):
    """The in-process equivalent of ``POST /decide`` (IC-6)."""

    def decide(self, request: dict[str, object]) -> AgentReply:
        """Return response bytes or raise :class:`DecisionTimeout`."""
