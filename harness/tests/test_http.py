# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from core.action import parse_action
from core.config import EpisodeConfig
from core.engine import run_episode
from harness.http import (
    MAX_CAPTURE_BYTES,
    HTTPAgent,
    HTTPAgentConfigurationError,
    HTTPAgentError,
    HTTPAgentSecretError,
)
from harness.protocol import DecisionTimeout
from recorder.verify import verify_bundle
from recorder.writer import build_bundle_manifest, record_episode_bundle
from report.generator import write_report_files
from spec.canonical import canonical_bytes, sha256_prefixed


@dataclass(frozen=True, slots=True)
class _StubResponse:
    status: int
    body: bytes
    delay_before_headers_seconds: float = 0.0
    delay_between_bytes_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _CapturedRequest:
    path: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(slots=True)
class _StubState:
    responses: list[_StubResponse]
    requests: list[_CapturedRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def capture(self, request: _CapturedRequest) -> _StubResponse:
        with self.lock:
            self.requests.append(request)
            if not self.responses:
                raise AssertionError("stub received more requests than configured")
            return self.responses.pop(0)


class _StubServer(ThreadingHTTPServer):
    state: _StubState

    def __init__(
        self,
        server_address: tuple[str, int],
        state: _StubState,
    ) -> None:
        self.state = state
        super().__init__(server_address, _StubHandler)


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        request = _CapturedRequest(
            path=self.path,
            headers={key.lower(): value for key, value in self.headers.items()},
            body=self.rfile.read(content_length),
        )
        response = cast(_StubServer, self.server).state.capture(request)
        if response.delay_before_headers_seconds:
            time.sleep(response.delay_before_headers_seconds)
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if response.delay_between_bytes_seconds:
                for value in response.body:
                    self.wfile.write(bytes((value,)))
                    self.wfile.flush()
                    time.sleep(response.delay_between_bytes_seconds)
            else:
                self.wfile.write(response.body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Deadline tests intentionally close the peer while this handler
            # is still trying to produce a slow response.
            pass

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _serve(
    responses: list[_StubResponse],
) -> Iterator[tuple[str, _StubState]]:
    state = _StubState(list(responses))
    server = _StubServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(
    *,
    deadline_ms: int = 1_000,
    attempt: int = 1,
    observation: dict[str, object] | None = None,
    retry: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": "runner_request/v1",
        "attempt": attempt,
        "observation": observation
        or {
            "schema": "observation/v1",
            "episode": {
                "episode_id": "ep_1111111111111111",
                "turn": 0,
                "response_deadline_ms": deadline_ms,
            },
        },
        "retry": retry,
    }


def _action_body(target: str = "0") -> bytes:
    return canonical_bytes(
        {
            "schema": "action/v1",
            "intent_kind": "leverage_target",
            "target": {"BTC": target},
        }
    )


def test_valid_response_preserves_bytes_and_canonical_request_without_auth() -> None:
    body = _action_body("1")
    with _serve([_StubResponse(200, body)]) as (origin, state):
        request = _request()
        reply = HTTPAgent(origin).decide(request)

    assert reply.body == body
    assert reply.http_status == 200
    assert reply.transport == "http"
    assert reply.latency_ms >= 0
    assert len(state.requests) == 1
    captured = state.requests[0]
    assert captured.path == "/decide"
    assert captured.body == canonical_bytes(request)
    assert captured.headers["content-type"] == "application/json"
    assert captured.headers["accept-encoding"] == "identity"
    assert "authorization" not in captured.headers
    assert "proxy-authorization" not in captured.headers


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param(lambda value: value, id="exact"),
        pytest.param(lambda value: value.hex().encode("ascii"), id="hex"),
        pytest.param(lambda value: base64.b64encode(value), id="base64"),
        pytest.param(
            lambda value: base64.urlsafe_b64encode(value).rstrip(b"="),
            id="base64url",
        ),
    ],
)
def test_protected_response_material_is_discarded_before_agent_reply(
    transform: object,
) -> None:
    secret = b"TE_SYNTHETIC_SECRET_CANARY_7b72a199"
    encoded = cast("Callable[[bytes], bytes]", transform)(secret)
    body = canonical_bytes(
        {
            "schema": "action/v1",
            "intent_kind": "leverage_target",
            "target": {"BTC": "0"},
            "comment": encoded.decode("ascii"),
        }
    )
    with _serve([_StubResponse(200, body)]) as (origin, _state):
        with pytest.raises(
            HTTPAgentSecretError,
            match="discarded before evidence capture",
        ) as caught:
            HTTPAgent(
                origin,
                protected_response_values=(secret,),
            ).decide(_request())

    assert caught.value.body is None
    assert secret not in str(caught.value).encode("utf-8")


_CANARY_TEXT = "TE_SYNTHETIC_SECRET_CANARY_7b72a199"
# Other JSON escapes elsewhere in the body must not perturb the ingress scan.
_ESCAPE_NOISE = b'"note":"alpha\\nbeta \\"quoted\\" \\/slashed\\/ \\u00e9",'


def _json_escape_chars(
    value: str,
    indices: frozenset[int],
    *,
    upper: bool = False,
) -> str:
    """Escape only the selected characters as JSON ``\\uXXXX`` sequences."""

    template = "\\u{:04X}" if upper else "\\u{:04x}"
    return "".join(
        template.format(ord(char)) if index in indices else char
        for index, char in enumerate(value)
    )


def _mixed_escape(value: str) -> str:
    """Interleave raw, lowercase-hex and uppercase-hex escaped characters."""

    parts: list[str] = []
    for index, char in enumerate(value):
        if index % 3 == 0:
            parts.append(f"\\u{ord(char):04x}")
        elif index % 3 == 1:
            parts.append(f"\\u{ord(char):04X}")
        else:
            parts.append(char)
    return "".join(parts)


def _comment_wire_body(comment: str) -> bytes:
    """Action bytes with ``comment`` spliced in verbatim as JSON source."""

    return (
        b'{"comment":"'
        + comment.encode("ascii")
        + b'","intent_kind":"leverage_target",'
        + _ESCAPE_NOISE
        + b'"schema":"action/v1","target":{"BTC":"0"}}'
    )


@pytest.mark.parametrize(
    "escape",
    [
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset({0})),
            id="single-first-char",
        ),
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset({len(value) // 2})),
            id="single-middle-char",
        ),
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset({len(value) - 1})),
            id="single-last-char",
        ),
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset({2}), upper=True),
            id="single-char-uppercase-hex",
        ),
        pytest.param(
            lambda value: _json_escape_chars(
                value, frozenset(range(0, len(value), 2))
            ),
            id="alternating-chars",
        ),
        pytest.param(_mixed_escape, id="mixed-lower-upper-and-raw"),
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset(range(len(value)))),
            id="fully-escaped",
        ),
    ],
)
def test_json_unicode_escaped_secret_fails_closed_at_ingress(
    escape: Callable[[str], str],
) -> None:
    secret = _CANARY_TEXT.encode("ascii")
    comment = escape(_CANARY_TEXT)
    assert comment != _CANARY_TEXT
    body = _comment_wire_body(comment)
    # The auditor's bypass: the credential is absent as a literal substring in
    # every previously protected encoding, yet a JSON parser recovers it.
    assert secret not in body
    assert json.loads(body)["comment"] == _CANARY_TEXT

    with _serve([_StubResponse(200, body)]) as (origin, _state):
        with pytest.raises(
            HTTPAgentSecretError,
            match="discarded before evidence capture",
        ) as caught:
            HTTPAgent(
                origin,
                protected_response_values=(secret,),
            ).decide(_request())

    assert caught.value.body is None
    assert secret not in str(caught.value).encode("utf-8")


def test_double_escaped_nested_json_secret_fails_closed_at_ingress() -> None:
    secret = _CANARY_TEXT.encode("ascii")
    nested = _json_escape_chars(_CANARY_TEXT, frozenset({0, 5})).replace(
        "\\", "\\\\"
    )
    body = _comment_wire_body(nested)
    inner = cast(str, json.loads(body)["comment"])
    assert secret not in body
    assert secret not in inner.encode("ascii")
    assert cast(str, json.loads(f'"{inner}"')) == _CANARY_TEXT

    with _serve([_StubResponse(200, body)]) as (origin, _state):
        with pytest.raises(
            HTTPAgentSecretError,
            match="discarded before evidence capture",
        ) as caught:
            HTTPAgent(
                origin,
                protected_response_values=(secret,),
            ).decide(_request())

    assert caught.value.body is None


def test_json_escaped_body_without_credentials_is_still_delivered() -> None:
    secret = _CANARY_TEXT.encode("ascii")
    harmless = "harmless commentary"
    body = _comment_wire_body(
        _json_escape_chars(harmless, frozenset(range(len(harmless))))
    )
    with _serve([_StubResponse(200, body)]) as (origin, _state):
        reply = HTTPAgent(
            origin,
            protected_response_values=(secret,),
        ).decide(_request())

    # Unescaping is a scan-only view; the reply keeps the exact wire bytes.
    assert reply.body == body
    assert reply.http_status == 200


def test_secret_echo_never_enters_complete_bundle_or_report(
    tmp_path: Path,
) -> None:
    secret = b"TE_SYNTHETIC_SECRET_CANARY_7b72a199"
    leaking_action = canonical_bytes(
        {
            "schema": "action/v1",
            "intent_kind": "leverage_target",
            "target": {"BTC": "0"},
            "comment": secret.decode("ascii"),
        }
    )
    with _serve(
        [_StubResponse(200, leaking_action) for _ in range(13)]
    ) as (origin, state):
        result = run_episode(
            pack_dir=Path(__file__).resolve().parents[2]
            / "fixtures/golden-mini/pack",
            agent=HTTPAgent(
                origin,
                protected_response_values=(secret,),
            ),
            config=EpisodeConfig(response_deadline_ms=1_000),
            run_id="run_51ec000000000001",
            episode_id="ep_51ec000000000001",
        )

    serialized = canonical_bytes(
        {
            "events": result.events,
            "raw_paths": sorted(result.raw_blobs),
            "metrics": result.metrics,
        }
    )
    assert len(state.requests) == 13
    assert result.raw_blobs == {}
    assert secret not in serialized
    assert not any(
        event["type"] == "AgentResponded"
        for event in result.events
    )
    rejected = [
        cast(dict[str, object], event["payload"])
        for event in result.events
        if event["type"] == "ActionRejected"
    ]
    assert len(rejected) == 13
    assert {row["reason"] for row in rejected} == {"agent_error"}
    assert all(
        row["detail"] == "agent transport failed: HTTPAgentSecretError"
        for row in rejected
    )
    agent_manifest: dict[str, object] = {
        "schema": "agent_manifest/v1",
        "name": "credential-canary-agent",
        "adapter": "http",
        "model_id": "synthetic/no-request",
        "endpoint_domains": ["api.example.com"],
        "inference_params": {},
        "prompt_sha256": None,
        "agent_version": "sec1-test",
        "image_sha256": None,
    }
    bundle = tmp_path / "bundle"
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=1_700_000_000_000,
        host={"os": "test", "arch": "test", "python": "3.12"},
    )
    record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )
    verification = verify_bundle(bundle)
    assert verification.is_complete
    report_dir = tmp_path / "report"
    generated = write_report_files(bundle, report_dir)
    assert generated.terminal_text.is_file()

    forbidden = {
        secret,
        secret.hex().encode("ascii"),
        secret.hex().upper().encode("ascii"),
        base64.b64encode(secret),
        base64.b64encode(secret).rstrip(b"="),
        base64.urlsafe_b64encode(secret),
        base64.urlsafe_b64encode(secret).rstrip(b"="),
    }
    emitted_files = [
        path
        for root in (bundle, report_dir)
        for path in root.rglob("*")
        if path.is_file()
    ]
    assert emitted_files
    assert not [
        (path, representation)
        for path in emitted_files
        for representation in forbidden
        if representation in path.read_bytes()
    ]


def test_malformed_first_response_retries_with_identical_observation() -> None:
    invalid = b'{"broken":'
    valid = _action_body()
    responses = [_StubResponse(400, invalid), _StubResponse(200, valid)]
    responses.extend(_StubResponse(200, valid) for _ in range(12))
    with _serve(responses) as (origin, state):
        result = run_episode(
            pack_dir=Path(__file__).resolve().parents[2]
            / "fixtures/golden-mini/pack",
            agent=HTTPAgent(origin),
            config=EpisodeConfig(response_deadline_ms=1_000),
            run_id="run_1111111111111111",
            episode_id="ep_1111111111111111",
        )

    first = cast(dict[str, object], json.loads(state.requests[0].body))
    second = cast(dict[str, object], json.loads(state.requests[1].body))
    assert first["attempt"] == 1
    assert first["retry"] is None
    assert second["attempt"] == 2
    retry = cast(dict[str, object], second["retry"])
    assert retry["reason"] == "schema_invalid"
    assert retry["prior_raw_sha256"] == sha256_prefixed(invalid)
    assert first["observation"] == second["observation"]
    assert canonical_bytes(first["observation"]) == canonical_bytes(
        second["observation"]
    )
    responded = [
        cast(dict[str, object], event["payload"])
        for event in result.events
        if event["turn"] == 0 and event["type"] == "AgentResponded"
    ]
    assert [payload["http_status"] for payload in responded] == [400, 200]
    assert all(payload["transport"] == "http" for payload in responded)


def test_valid_action_on_4xx_is_forced_through_malformed_retry_path() -> None:
    valid = _action_body()
    responses = [_StubResponse(422, valid), _StubResponse(200, valid)]
    responses.extend(_StubResponse(200, valid) for _ in range(12))
    with _serve(responses) as (origin, state):
        result = run_episode(
            pack_dir=Path(__file__).resolve().parents[2]
            / "fixtures/golden-mini/pack",
            agent=HTTPAgent(origin),
            config=EpisodeConfig(response_deadline_ms=1_000),
            run_id="run_1111111111111111",
            episode_id="ep_1111111111111111",
        )

    first = cast(dict[str, object], json.loads(state.requests[0].body))
    second = cast(dict[str, object], json.loads(state.requests[1].body))
    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert first["observation"] == second["observation"]
    retry = cast(dict[str, object], second["retry"])
    assert retry == {
        "reason": "schema_invalid",
        "detail": "response does not validate against action/v1",
        "prior_raw_sha256": sha256_prefixed(valid),
    }
    turn_zero = [
        event for event in result.events if event["turn"] == 0
    ]
    responses_seen = [
        cast(dict[str, object], event["payload"])
        for event in turn_zero
        if event["type"] == "AgentResponded"
    ]
    assert [payload["http_status"] for payload in responses_seen] == [422, 200]
    parsed = next(
        cast(dict[str, object], event["payload"])
        for event in turn_zero
        if event["type"] == "ActionParsed"
    )
    assert parsed["from_attempt"] == 2
    assert result.raw_blobs["raw/0000-a1.txt"] == valid


def test_header_stall_hits_absolute_deadline_without_internal_retry() -> None:
    with _serve(
        [_StubResponse(200, _action_body(), delay_before_headers_seconds=0.2)]
    ) as (origin, state):
        started = time.monotonic()
        with pytest.raises(DecisionTimeout, match="absolute 40 ms"):
            HTTPAgent(origin).decide(_request(deadline_ms=40))
        elapsed = time.monotonic() - started

    assert elapsed < 0.18
    assert len(state.requests) == 1


def test_slow_trickle_cannot_extend_the_absolute_deadline() -> None:
    with _serve(
        [
            _StubResponse(
                200,
                b"0123456789",
                delay_between_bytes_seconds=0.025,
            )
        ]
    ) as (origin, state):
        started = time.monotonic()
        with pytest.raises(DecisionTimeout, match="absolute 70 ms"):
            HTTPAgent(origin).decide(_request(deadline_ms=70))
        elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert len(state.requests) == 1


def test_5xx_is_actionable_agent_error_with_no_internal_retry() -> None:
    body = b'{"error":"agent crashed"}'
    with _serve([_StubResponse(503, body)]) as (origin, state):
        with pytest.raises(HTTPAgentError, match="does not retry") as caught:
            HTTPAgent(origin).decide(_request())

    assert caught.value.http_status == 503
    assert caught.value.body == body
    assert caught.value.endpoint.endswith("/decide")
    assert len(state.requests) == 1


def test_4xx_is_returned_verbatim_for_malformed_retry_path() -> None:
    body = b"malformed action"
    with _serve([_StubResponse(422, body)]) as (origin, state):
        reply = HTTPAgent(origin).decide(_request())

    assert reply.body == body
    assert reply.http_status == 422
    assert reply.transport == "http"
    assert len(state.requests) == 1


def test_invalid_utf8_is_preserved_for_core_classification() -> None:
    body = b"\xff\xfe\x80"
    with _serve([_StubResponse(200, body)]) as (origin, _state):
        reply = HTTPAgent(origin).decide(_request())

    assert reply.body == body
    assert parse_action(reply.body, ("BTC",)).reason == "oversize"


@pytest.mark.parametrize(
    ("wire_size", "captured_size"),
    [
        (65_536, 65_536),
        (65_537, 65_537),
        (65_538, 65_537),
    ],
)
def test_response_capture_boundary_preserves_oversize_classification(
    wire_size: int,
    captured_size: int,
) -> None:
    body = b"x" * wire_size
    with _serve([_StubResponse(200, body)]) as (origin, _state):
        reply = HTTPAgent(origin).decide(_request())

    assert len(reply.body) == captured_size
    assert reply.body == body[:captured_size]
    assert MAX_CAPTURE_BYTES == 65_537
    if wire_size > 65_536:
        assert parse_action(reply.body, ("BTC",)).reason == "oversize"


def test_connection_refusal_is_actionable_transport_error() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    host, port = cast(tuple[str, int], probe.getsockname())
    probe.close()

    with pytest.raises(HTTPAgentError, match="failed before") as caught:
        HTTPAgent(f"http://{host}:{port}").decide(_request(deadline_ms=500))

    assert caught.value.http_status is None
    assert caught.value.body is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8000",
        "http://user:secret@127.0.0.1:8000",
        "http://127.0.0.1:8000/other",
        "http://127.0.0.1:8000?token=secret",
    ],
)
def test_endpoint_rejects_noncanonical_or_authenticated_origins(
    origin: str,
) -> None:
    with pytest.raises(HTTPAgentConfigurationError):
        HTTPAgent(origin)
