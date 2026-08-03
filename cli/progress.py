# SPDX-License-Identifier: Apache-2.0
"""Stderr heartbeat for long agent runs.

The engine and recorder stay silent by design; a priced multi-hundred-turn
episode therefore looks frozen from the terminal. ``HeartbeatAgent`` wraps
any :class:`~harness.protocol.InProcessAgent` and narrates progress to a
status stream. It never writes to stdout, never touches evidence, adds no
randomness (phrases rotate by turn index), and delegates ``decide`` and
harness-event draining unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import IO

from harness.protocol import AgentReply, HarnessEvent, InProcessAgent

# Deterministic flavor. Survival-native gallows humor only: no phrase may
# promise, predict, or celebrate returns (LABEL discipline extends to vibes).
HEARTBEAT_PHRASES: tuple[str, ...] = (
    "aping in... responsibly",
    "checking distance to liquidation...",
    "respecting the kill switch...",
    "funding bleed intensifies...",
    "diamond-handing the drawdown...",
    "watching the wick...",
    "consulting the candles...",
    "leverage is a spectrum...",
    "surviving is the meta...",
    "gm. still solvent...",
    "down bad? verifying...",
    "cope, sealed and hashed...",
    "wagmi status: pending...",
    "touching grass between bars...",
    "no paper hands detected...",
    "the ledger remembers everything...",
)

_PLAIN_LINE_EVERY: int = 25


@dataclass
class HeartbeatAgent:
    """Transparent progress-narrating wrapper around a slow agent."""

    inner: InProcessAgent
    stream: IO[str]
    total_turns: int | None = None
    interactive: bool = False
    _decided: int = field(default=0, init=False)
    _started: float = field(default=0.0, init=False)

    def decide(self, request: dict[str, object]) -> AgentReply:
        if self._started == 0.0:
            self._started = time.monotonic()
        attempt = request.get("attempt")
        if attempt == 1:
            self._decided += 1
            self._emit(retrying=False)
        else:
            self._emit(retrying=True)
        try:
            return self.inner.decide(request)
        finally:
            if (
                self.total_turns is not None
                and self._decided >= self.total_turns
            ):
                self._finish()

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        drain = getattr(self.inner, "drain_harness_events", None)
        if drain is None:
            return ()
        events = drain()
        return tuple(events)

    def _emit(self, *, retrying: bool) -> None:
        turn = self._decided
        phrase = (
            "one more time, with valid JSON..."
            if retrying
            else HEARTBEAT_PHRASES[(turn - 1) % len(HEARTBEAT_PHRASES)]
        )
        elapsed = int(time.monotonic() - self._started)
        minutes, seconds = divmod(elapsed, 60)
        of_total = (
            f"/{self.total_turns}" if self.total_turns is not None else ""
        )
        line = f"[turn {turn}{of_total} · {minutes}m{seconds:02d}s] {phrase}"
        if self.interactive:
            self.stream.write("\r\x1b[2K" + line)
        elif retrying or turn == 1 or turn % _PLAIN_LINE_EVERY == 0:
            self.stream.write(line + "\n")
        self.stream.flush()

    def _finish(self) -> None:
        if self.interactive:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()


def stream_is_interactive(stream: IO[str]) -> bool:
    """True when carriage-return live updates will render sanely."""

    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False
