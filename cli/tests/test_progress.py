# SPDX-License-Identifier: Apache-2.0
"""Heartbeat wrapper tests: narration without interference."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO

from cli.progress import (
    HEARTBEAT_PHRASES,
    HeartbeatAgent,
    stream_is_interactive,
)
from harness.protocol import AgentReply


@dataclass
class _EchoAgent:
    replies: list[dict[str, object]] = field(default_factory=list)

    def decide(self, request: dict[str, object]) -> AgentReply:
        self.replies.append(request)
        return AgentReply(body=b'{"ok":true}', http_status=200)


def _request(turn: int, attempt: int = 1) -> dict[str, object]:
    return {
        "schema": "runner_request/v1",
        "attempt": attempt,
        "observation": {"episode": {"turn": turn}},
        "retry": None,
    }


def test_heartbeat_delegates_verbatim_and_counts_turns() -> None:
    inner = _EchoAgent()
    stream = StringIO()
    agent = HeartbeatAgent(inner=inner, stream=stream, total_turns=3)

    for turn in range(3):
        reply = agent.decide(_request(turn))
        assert reply.body == b'{"ok":true}'

    assert len(inner.replies) == 3
    output = stream.getvalue()
    assert "[turn 1/3" in output
    assert HEARTBEAT_PHRASES[0] in output
    assert "wagmi" not in output.split("[turn 1/3")[0]


def test_retry_attempt_does_not_advance_the_turn_counter() -> None:
    inner = _EchoAgent()
    stream = StringIO()
    agent = HeartbeatAgent(inner=inner, stream=stream, total_turns=None)

    agent.decide(_request(0, attempt=1))
    agent.decide(_request(0, attempt=2))
    agent.decide(_request(1, attempt=1))

    output = stream.getvalue()
    assert "one more time, with valid JSON..." in output
    assert agent._decided == 2
    assert "[turn 3" not in output


def test_plain_stream_prints_first_and_every_25th_turn() -> None:
    inner = _EchoAgent()
    stream = StringIO()
    agent = HeartbeatAgent(inner=inner, stream=stream, total_turns=60)

    for turn in range(60):
        agent.decide(_request(turn))

    lines = [line for line in stream.getvalue().splitlines() if line]
    starts = [line.split(" ·")[0] for line in lines]
    assert starts == ["[turn 1/60", "[turn 25/60", "[turn 50/60"]


def test_interactive_stream_rewrites_one_line_and_clears_at_end() -> None:
    inner = _EchoAgent()
    stream = StringIO()
    agent = HeartbeatAgent(
        inner=inner,
        stream=stream,
        total_turns=2,
        interactive=True,
    )

    agent.decide(_request(0))
    agent.decide(_request(1))

    output = stream.getvalue()
    assert output.count("\r\x1b[2K") == 3
    assert output.endswith("\r\x1b[2K")
    assert "\n" not in output


def test_stream_interactivity_detection_is_fail_closed() -> None:
    assert stream_is_interactive(StringIO()) is False

    class NoIsatty:
        pass

    assert stream_is_interactive(NoIsatty()) is False  # type: ignore[arg-type]


def test_drain_harness_events_passthrough_default_empty() -> None:
    agent = HeartbeatAgent(
        inner=_EchoAgent(),
        stream=StringIO(),
        total_turns=None,
    )
    assert agent.drain_harness_events() == ()
