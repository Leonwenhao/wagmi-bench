# SPDX-License-Identifier: Apache-2.0
"""Deadline-bounded HTTP/1.1 adapter for the frozen IC-6 runner contract.

One :meth:`HTTPAgent.decide` call is exactly one ``POST /decide`` attempt.
The episode loop owns the single malformed-output retry because only the
IC-3 parser can construct the required validator feedback.

Responses are never decoded here.  Successful and 4xx bodies are returned as
the exact bytes received, capped at 65,537 bytes: one byte beyond the IC-3
limit so the core parser can deterministically classify an oversized response.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast
from urllib.parse import SplitResult, quote_from_bytes, urlsplit

from harness.protocol import AgentReply, DecisionTimeout
from spec.canonical import canonical_bytes

__all__ = [
    "HTTPAgent",
    "HTTPAgentConfigurationError",
    "HTTPAgentError",
    "HTTPAgentSecretError",
    "MAX_CAPTURE_BYTES",
]

MAX_ACTION_BYTES: Final = 65_536
MAX_CAPTURE_BYTES: Final = MAX_ACTION_BYTES + 1
_READ_CHUNK_BYTES: Final = 16_384
_DECIDE_PATH: Final = "/decide"

_BACKSLASH: Final = 0x5C
_HEX_DIGITS: Final = frozenset(b"0123456789abcdefABCDEF")
_JSON_SIMPLE_ESCAPES: Final = {
    ord('"'): b'"',
    ord("\\"): b"\\",
    ord("/"): b"/",
    ord("b"): b"\b",
    ord("f"): b"\f",
    ord("n"): b"\n",
    ord("r"): b"\r",
    ord("t"): b"\t",
}
# A body may nest one JSON document inside another's string values, so a
# single unescape pass is not enough to expose a doubly escaped credential.
_MAX_UNESCAPE_PASSES: Final = 3


class HTTPAgentConfigurationError(ValueError):
    """The HTTP runner endpoint or request deadline is not usable."""


class HTTPAgentError(RuntimeError):
    """An IC-6 transport failure that must become ``agent_error``.

    ``body`` is populated for an HTTP status response and retains the exact
    received bytes (up to :data:`MAX_CAPTURE_BYTES`).  It is deliberately not
    interpolated into the exception message, where model output could leak.
    """

    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        http_status: int | None = None,
        body: bytes | None = None,
    ) -> None:
        self.endpoint: Final = endpoint
        self.http_status: Final = http_status
        self.body: Final = body
        super().__init__(message)


class HTTPAgentSecretError(HTTPAgentError):
    """A response carried protected credential material and was discarded."""


@dataclass(frozen=True, slots=True)
class _HTTPResult:
    status: int
    body: bytes
    finished_at_ns: int


@dataclass(frozen=True, slots=True)
class _TransportFailure:
    error: Exception
    finished_at_ns: int


_WorkerOutcome = _HTTPResult | _TransportFailure


class _ConnectionGuard:
    """Allow the deadline owner to interrupt a worker's blocking socket."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection: http.client.HTTPConnection | None = None
        self._cancelled = False

    def install(self, connection: http.client.HTTPConnection) -> bool:
        with self._lock:
            if self._cancelled:
                return False
            self._connection = connection
            return True

    def abort(self) -> None:
        with self._lock:
            self._cancelled = True
            connection = self._connection
        if connection is not None:
            connection.close()


def _validate_origin(origin: str) -> SplitResult:
    parsed = urlsplit(origin)
    if parsed.scheme != "http":
        raise HTTPAgentConfigurationError(
            f"IC-6 agent origin must use plain sandbox-internal HTTP: {origin!r}"
        )
    if parsed.hostname is None:
        raise HTTPAgentConfigurationError(
            f"IC-6 agent origin has no hostname: {origin!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise HTTPAgentConfigurationError(
            "IC-6 forbids credentials and auth in the agent origin"
        )
    if parsed.query or parsed.fragment:
        raise HTTPAgentConfigurationError(
            f"IC-6 agent origin must not contain a query or fragment: {origin!r}"
        )
    if parsed.path not in {"", "/", _DECIDE_PATH}:
        raise HTTPAgentConfigurationError(
            "IC-6 agent origin path must be empty, '/', or '/decide'; "
            f"got {parsed.path!r}"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPAgentConfigurationError(
            f"IC-6 agent origin has an invalid port: {origin!r}"
        ) from exc
    if port is not None and not 1 <= port <= 65_535:
        raise HTTPAgentConfigurationError(
            f"IC-6 agent origin port is out of range: {port}"
        )
    return parsed


def _deadline_ms(request: Mapping[str, object]) -> int:
    observation = request.get("observation")
    if not isinstance(observation, Mapping):
        raise HTTPAgentConfigurationError(
            "runner_request.observation must be an object"
        )
    episode = observation.get("episode")
    if not isinstance(episode, Mapping):
        raise HTTPAgentConfigurationError(
            "runner_request.observation.episode must be an object"
        )
    value = episode.get("response_deadline_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HTTPAgentConfigurationError(
            "runner_request.observation.episode.response_deadline_ms "
            "must be a positive integer"
        )
    return value


def _validate_request_envelope(request: Mapping[str, object]) -> None:
    expected = {"schema", "attempt", "observation", "retry"}
    if set(request) != expected:
        missing = sorted(expected - set(request))
        extra = sorted(set(request) - expected)
        raise HTTPAgentConfigurationError(
            "runner_request/v1 fields do not match the frozen envelope: "
            f"missing={missing}, extra={extra}"
        )
    if request.get("schema") != "runner_request/v1":
        raise HTTPAgentConfigurationError(
            "runner request schema must be exactly 'runner_request/v1'"
        )
    attempt = request.get("attempt")
    retry = request.get("retry")
    if attempt == 1 and retry is not None:
        raise HTTPAgentConfigurationError(
            "runner_request attempt 1 must have retry=null"
        )
    if attempt == 2 and not isinstance(retry, Mapping):
        raise HTTPAgentConfigurationError(
            "runner_request attempt 2 must carry retry feedback"
        )
    if attempt not in (1, 2) or isinstance(attempt, bool):
        raise HTTPAgentConfigurationError(
            "runner_request.attempt must be exactly 1 or 2"
        )


def _read_capped(response: http.client.HTTPResponse) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_CAPTURE_BYTES
    while remaining:
        chunk = response.read(min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _is_hex_quad(digits: bytes) -> bool:
    return len(digits) == 4 and all(digit in _HEX_DIGITS for digit in digits)


def _json_escaped(raw: bytes, *, upper: bool) -> bytes | None:
    """Return the fully ``\\uXXXX``-escaped form of ``raw``, if representable."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    template = "\\u{:04X}" if upper else "\\u{:04x}"
    parts: list[str] = []
    for char in text:
        code = ord(char)
        if code > 0xFFFF:
            offset = code - 0x10000
            parts.append(template.format(0xD800 + (offset >> 10)))
            parts.append(template.format(0xDC00 + (offset & 0x3FF)))
        else:
            parts.append(template.format(code))
    return "".join(parts).encode("ascii")


def _decode_json_escapes(text: bytes) -> bytes:
    """Resolve JSON string escapes so encoded credentials cannot hide.

    Applied to the scanned buffer, not to a fixed set of whole-credential
    encodings: escaping a *single* character of a credential defeats plain
    substring matching, and only decode-then-scan catches every partial
    combination.  Unrecognised escapes are preserved verbatim, so this is a
    superset scan and never hides material that the raw buffer already shows.
    """

    decoded = bytearray()
    index = 0
    length = len(text)
    while index < length:
        byte = text[index]
        if byte != _BACKSLASH or index + 1 >= length:
            decoded.append(byte)
            index += 1
            continue
        simple = _JSON_SIMPLE_ESCAPES.get(text[index + 1])
        if simple is not None:
            decoded += simple
            index += 2
            continue
        digits = text[index + 2 : index + 6]
        if text[index + 1] not in (ord("u"), ord("U")) or not _is_hex_quad(digits):
            decoded.append(byte)
            index += 1
            continue
        code = int(digits, 16)
        index += 6
        if 0xD800 <= code <= 0xDBFF and index + 6 <= length:
            low_digits = text[index + 2 : index + 6]
            if (
                text[index] == _BACKSLASH
                and text[index + 1] in (ord("u"), ord("U"))
                and _is_hex_quad(low_digits)
                and 0xDC00 <= int(low_digits, 16) <= 0xDFFF
            ):
                low = int(low_digits, 16) - 0xDC00
                code = 0x10000 + ((code - 0xD800) << 10) + low
                index += 6
        decoded += chr(code).encode("utf-8", "surrogatepass")
    return bytes(decoded)


def _scan_variants(body: bytes) -> tuple[bytes, ...]:
    """Return the raw body plus its progressively JSON-unescaped forms."""

    if _BACKSLASH not in body:
        return (body,)
    variants = [body]
    seen = {body}
    current = body
    for _ in range(_MAX_UNESCAPE_PASSES):
        current = _decode_json_escapes(current)
        if current in seen:
            break
        seen.add(current)
        variants.append(current)
        if _BACKSLASH not in current:
            break
    return tuple(variants)


def _protected_representations(
    values: Sequence[str | bytes],
) -> tuple[tuple[int, tuple[bytes, ...]], ...]:
    """Freeze exact and common reversible credential encodings.

    The filter is an ingress gate, not post-recording redaction: a matching
    response never becomes an ``AgentReply`` and therefore cannot enter an
    event, raw blob, report, replay artifact, or exception body.
    """

    protected: set[bytes] = set()
    for index, value in enumerate(values):
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if not isinstance(raw, bytes) or not raw:
            raise HTTPAgentConfigurationError(
                f"protected response value {index} must be non-empty bytes or text"
            )
        protected.update(
            {
                raw,
                raw.hex().encode("ascii"),
                raw.hex().upper().encode("ascii"),
                base64.b64encode(raw),
                base64.b64encode(raw).rstrip(b"="),
                base64.urlsafe_b64encode(raw),
                base64.urlsafe_b64encode(raw).rstrip(b"="),
                quote_from_bytes(raw).encode("ascii"),
            }
        )
        protected.update(
            escaped
            for escaped in (
                _json_escaped(raw, upper=False),
                _json_escaped(raw, upper=True),
            )
            if escaped
        )
    fingerprints: dict[int, set[bytes]] = {}
    for representation in protected:
        fingerprints.setdefault(len(representation), set()).add(
            hashlib.sha256(representation).digest()
        )
    return tuple(
        (
            length,
            tuple(sorted(digests)),
        )
        for length, digests in sorted(fingerprints.items(), reverse=True)
    )


def _contains_protected_representation(
    body: bytes,
    fingerprints: tuple[tuple[int, tuple[bytes, ...]], ...],
) -> bool:
    """Compare response windows while retaining no credential plaintext.

    The raw bytes and every JSON-unescaped form of them are scanned, so a
    credential hidden behind partial ``\\uXXXX`` escaping fails closed.
    """

    if not fingerprints:
        return False
    for variant in _scan_variants(body):
        view = memoryview(variant)
        for length, expected_digests in fingerprints:
            if length > len(variant):
                continue
            for start in range(len(variant) - length + 1):
                observed = hashlib.sha256(view[start : start + length]).digest()
                if any(
                    hmac.compare_digest(observed, expected)
                    for expected in expected_digests
                ):
                    return True
    return False


class HTTPAgent:
    """IC-6 HTTP agent adapter with an absolute per-attempt deadline.

    The deadline is read from the frozen observation field, keeping the
    bundle run configuration as the sole source of truth.  A daemon worker
    owns the blocking stdlib HTTP client while the caller waits against one
    absolute monotonic deadline.  This prevents a peer from extending the
    budget indefinitely by sending a byte just before each socket timeout.
    """

    def __init__(
        self,
        origin: str,
        *,
        protected_response_values: Sequence[str | bytes] = (),
    ) -> None:
        parsed = _validate_origin(origin)
        self._host: Final = cast(str, parsed.hostname)
        self._port: Final = parsed.port or 80
        host_for_url = (
            f"[{self._host}]" if ":" in self._host else self._host
        )
        self._origin: Final = f"http://{host_for_url}:{self._port}"
        self._endpoint: Final = self._origin + _DECIDE_PATH
        self._protected_response_fingerprints: Final = _protected_representations(
            protected_response_values
        )

    @property
    def endpoint(self) -> str:
        """The normalized credential-free ``POST /decide`` endpoint."""

        return self._endpoint

    def decide(self, request: dict[str, object]) -> AgentReply:
        """Send one canonical IC-6 attempt and return its verbatim response.

        2xx responses are received normally.  4xx responses are also returned
        so the IC-3 parser can classify them and the episode loop can issue its
        one validator-informed retry.  5xx and non-HTTP transport failures
        raise :class:`HTTPAgentError`, which the loop records as
        ``agent_error`` without retry.
        """

        _validate_request_envelope(request)
        response_deadline_ms = _deadline_ms(request)
        request_body = canonical_bytes(request)
        started_at_ns = time.monotonic_ns()
        deadline_ns = started_at_ns + response_deadline_ms * 1_000_000
        outcomes: queue.Queue[_WorkerOutcome] = queue.Queue(maxsize=1)
        guard = _ConnectionGuard()

        worker = threading.Thread(
            target=self._exchange,
            args=(
                request_body,
                deadline_ns,
                outcomes,
                guard,
            ),
            name="tradeevolve-http-decision",
            daemon=True,
        )
        worker.start()

        remaining_seconds = max(
            0.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        )
        try:
            outcome = outcomes.get(timeout=remaining_seconds)
        except queue.Empty:
            # Resolve the queue/deadline boundary deterministically: a worker
            # that completed before the deadline wins even if this thread was
            # scheduled a moment late.
            try:
                outcome = outcomes.get_nowait()
            except queue.Empty:
                guard.abort()
                raise DecisionTimeout(
                    f"POST {self._endpoint} did not complete within the "
                    f"absolute {response_deadline_ms} ms IC-6 deadline; "
                    "timeouts are not retried"
                ) from None

        if outcome.finished_at_ns > deadline_ns:
            guard.abort()
            raise DecisionTimeout(
                f"POST {self._endpoint} completed after the absolute "
                f"{response_deadline_ms} ms IC-6 deadline; timeouts are not retried"
            )

        if isinstance(outcome, _TransportFailure):
            if isinstance(outcome.error, TimeoutError):
                raise DecisionTimeout(
                    f"POST {self._endpoint} did not complete within the "
                    f"absolute {response_deadline_ms} ms IC-6 deadline; "
                    "timeouts are not retried"
                ) from outcome.error
            raise HTTPAgentError(
                f"POST {self._endpoint} failed before a complete HTTP response: "
                f"{type(outcome.error).__name__}: {outcome.error}; "
                "IC-6 records this as agent_error and does not retry",
                endpoint=self._endpoint,
            ) from outcome.error

        if _contains_protected_representation(
            outcome.body,
            self._protected_response_fingerprints,
        ):
            raise HTTPAgentSecretError(
                "agent response contained protected credential material; "
                "the complete response was discarded before evidence capture",
                endpoint=self._endpoint,
            )

        latency_ms = max(
            0, (outcome.finished_at_ns - started_at_ns) // 1_000_000
        )
        if 200 <= outcome.status <= 299 or 400 <= outcome.status <= 499:
            return AgentReply(
                body=outcome.body,
                latency_ms=latency_ms,
                http_status=outcome.status,
                transport="http",
            )
        classification: Literal["server", "unexpected"] = (
            "server" if 500 <= outcome.status <= 599 else "unexpected"
        )
        raise HTTPAgentError(
            f"POST {self._endpoint} returned {classification} HTTP status "
            f"{outcome.status}; IC-6 records this as agent_error and does not retry",
            endpoint=self._endpoint,
            http_status=outcome.status,
            body=outcome.body,
        )

    def _exchange(
        self,
        request_body: bytes,
        deadline_ns: int,
        outcomes: queue.Queue[_WorkerOutcome],
        guard: _ConnectionGuard,
    ) -> None:
        timeout_seconds = max(
            0.001, (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        )
        connection = http.client.HTTPConnection(
            self._host,
            self._port,
            timeout=timeout_seconds,
        )
        if not guard.install(connection):
            connection.close()
            return
        try:
            connection.request(
                "POST",
                _DECIDE_PATH,
                body=request_body,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            body = _read_capped(response)
            outcome: _WorkerOutcome = _HTTPResult(
                status=response.status,
                body=body,
                finished_at_ns=time.monotonic_ns(),
            )
        except Exception as exc:
            outcome = _TransportFailure(
                error=exc,
                finished_at_ns=time.monotonic_ns(),
            )
        try:
            outcomes.put_nowait(outcome)
        except queue.Full:
            # The caller has already returned at the absolute deadline.
            pass
        finally:
            connection.close()
