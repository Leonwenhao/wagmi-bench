# SPDX-License-Identifier: Apache-2.0
"""Dependency-free IC-6 ``/healthz`` and ``/decide`` HTTP server."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from agents.common import (
    AgentContractError,
    DecisionPolicy,
    canonical_json_bytes,
)
from agents.evidence import MAX_ACTION_BYTES, invalid_provider_payload
from agents.llm import (
    InvalidProviderAction,
    LLMBaselinePolicy,
    ProviderError,
)
from agents.reckless import HttpEgressProbe, RecklessPolicy

MAX_REQUEST_BYTES = 2_097_152

__all__ = [
    "MAX_ACTION_BYTES",
    "MAX_REQUEST_BYTES",
    "AgentHTTPServer",
    "AgentRequestHandler",
    "build_policy_from_env",
    "main",
    "make_server",
]


def _decode_request(raw: bytes) -> Mapping[str, object]:
    try:
        value = cast(object, json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AgentContractError("request is not valid JSON") from None
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise AgentContractError("request must be a JSON object")
    return cast(Mapping[str, object], value)


class AgentHTTPServer(ThreadingHTTPServer):
    """Threaded local server carrying one immutable policy."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        policy: DecisionPolicy,
    ) -> None:
        self.policy = policy
        super().__init__(server_address, AgentRequestHandler)


class AgentRequestHandler(BaseHTTPRequestHandler):
    """Closed two-route IC-6 handler with deliberately silent access logs."""

    server: AgentHTTPServer
    server_version = "TradeEvolveAgent/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        # Never log URLs, headers, request bytes, provider output, or auth.
        del format, args

    def _send(
        self,
        status: HTTPStatus,
        payload: Mapping[str, object],
    ) -> None:
        body = canonical_json_bytes(payload)
        self._send_bytes(status, body)

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/healthz":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/decide":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self._send(HTTPStatus.LENGTH_REQUIRED, {"error": "length_required"})
            return
        try:
            length = int(content_length)
        except ValueError:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_length"})
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._send(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "request_too_large"},
            )
            return
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "incomplete_request"})
            return
        try:
            request = _decode_request(raw)
            action = self.server.policy.decide(request)
            response = canonical_json_bytes(action)
            if len(response) > MAX_ACTION_BYTES:
                raise AgentContractError("action exceeds the IC-3 size limit")
        except InvalidProviderAction as exc:
            self._send_bytes(
                HTTPStatus.BAD_REQUEST,
                invalid_provider_payload(exc),
            )
            return
        except AgentContractError:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_contract"})
            return
        except ProviderError:
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "decision_unavailable"},
            )
            return
        except Exception:
            # Fail closed without echoing an exception that might carry a
            # provider URL, model response, proxy detail, or credential.
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "decision_failed"},
            )
            return
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(response)


def build_policy_from_env(
    environ: Mapping[str, str] | None = None,
) -> DecisionPolicy:
    """Construct a policy only at process start, when runtime env is present."""

    source = os.environ if environ is None else environ
    mode = source.get("TRADEVOLVE_AGENT_MODE", "reckless")
    if mode == "llm":
        return LLMBaselinePolicy.from_env(source)
    if mode == "evoskill":
        from agents.evoskill import EvoSkillPolicy

        return EvoSkillPolicy.from_env(source)
    if mode == "reckless":
        destination = source.get("TRADEVOLVE_RECKLESS_EGRESS_URL")
        if destination is None:
            return RecklessPolicy()
        return RecklessPolicy(
            egress_probe=HttpEgressProbe(),
            egress_destination=destination,
        )
    raise ValueError(
        "TRADEVOLVE_AGENT_MODE must be 'reckless', 'llm', or 'evoskill'"
    )


def make_server(
    policy: DecisionPolicy,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> AgentHTTPServer:
    """Create but do not start a server, enabling isolated unit tests."""

    return AgentHTTPServer((host, port), policy)


def main() -> int:
    host = os.environ.get("TRADEVOLVE_AGENT_HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("TRADEVOLVE_AGENT_PORT", "8000"))
    except ValueError:
        return 2
    if not 1 <= port <= 65_535:
        return 2
    try:
        policy = build_policy_from_env()
    except (ProviderError, ValueError, OSError):
        # Startup failure is intentionally silent and lets /healthz fail
        # before economic activity. Never print environment-derived details.
        return 2
    server = make_server(policy, host=host, port=port)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
