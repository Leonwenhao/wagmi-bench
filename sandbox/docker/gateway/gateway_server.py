# SPDX-License-Identifier: Apache-2.0
"""Minimal exact-domain HTTPS CONNECT gateway.

This file is deliberately standalone because its containing directory is the
entire Docker build context.  The gateway never receives model credentials.
It permits an exact allowlisted hostname on TCP/443 only, then verifies that
the first TLS ClientHello carries the same exact SNI before opening the
upstream socket.  Every refusal is emitted as a secret-safe block fact.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import selectors
import socket
import socketserver
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Callable, Iterable, Sequence, TextIO, cast

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_TOKEN_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MAX_CLIENT_HELLO_BYTES = 65_536
_TLS_HANDSHAKE = 22
_CLIENT_HELLO = 1


class PolicyError(ValueError):
    """The declared allowlist or a proxy target is not safe."""


def _canonical_declared_domain(value: str) -> str:
    if not isinstance(value, str):
        raise PolicyError("endpoint domain must be a string")
    if value != value.lower() or not _DOMAIN_RE.fullmatch(value):
        raise PolicyError(
            "endpoint domain must be one exact lowercase ASCII hostname"
        )
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise PolicyError("IP literals are not endpoint domains")


def _canonical_requested_domain(value: str) -> str:
    try:
        ascii_value = value.encode("ascii").decode("ascii").lower()
    except UnicodeError as exc:
        raise PolicyError("requested host must be ASCII") from exc
    if not _DOMAIN_RE.fullmatch(ascii_value):
        raise PolicyError("requested host is not an exact DNS hostname")
    try:
        ipaddress.ip_address(ascii_value)
    except ValueError:
        return ascii_value
    raise PolicyError("requested host cannot be an IP literal")


@dataclass(frozen=True)
class EndpointPolicy:
    """An immutable, exact-hostname allowlist."""

    domains: tuple[str, ...]

    @classmethod
    def from_domains(cls, domains: Iterable[str]) -> EndpointPolicy:
        checked = tuple(_canonical_declared_domain(item) for item in domains)
        if len(set(checked)) != len(checked):
            raise PolicyError("endpoint domains must be unique")
        return cls(domains=checked)

    def allows(self, host: str, port: int) -> bool:
        try:
            canonical = _canonical_requested_domain(host)
        except PolicyError:
            return False
        return port == 443 and canonical in self.domains


def destination_token(value: str | bytes, *, kind: str) -> str:
    """Return a non-reversible destination label safe for evidence bundles.

    A denied DNS label can itself contain a model key.  Hashing every denied
    destination prevents the ISO-2 audit trail from violating SEC-1.
    """

    if not _TOKEN_KIND_RE.fullmatch(kind):
        raise ValueError("invalid destination-token kind")
    raw = value.encode("utf-8", "surrogatepass") if isinstance(value, str) else value
    return f"{kind}-sha256:{hashlib.sha256(raw).hexdigest()}"


@dataclass(frozen=True)
class BlockFact:
    """Unsequenced evidence for one frozen ``EgressBlocked`` payload."""

    destination: str
    port: int | None
    protocol: str
    count: int
    witness: str

    def __post_init__(self) -> None:
        if self.port is not None and not 0 <= self.port <= 65_535:
            raise ValueError("port outside uint16 range")
        if self.protocol not in {"https", "dns", "tcp", "udp", "other"}:
            raise ValueError("unsupported frozen EgressBlocked protocol")
        if self.count < 1:
            raise ValueError("count must be positive")
        if self.witness not in {
            "proxy_connect",
            "proxy_resolution",
            "proxy_sni",
            "kernel_redirect",
        }:
            raise ValueError("unsupported block witness")

    def event_payload(self) -> dict[str, object]:
        """Return exactly the frozen EgressBlocked payload fields."""

        return {
            "destination": self.destination,
            "port": self.port,
            "protocol": self.protocol,
            "count": self.count,
        }

    def log_record(self) -> dict[str, object]:
        """Return the internal collector envelope consumed by the harness."""

        return {
            "schema": "sandbox_egress_block/v1",
            "witness": self.witness,
            "event_payload": self.event_payload(),
        }


class JsonLineBlockSink:
    """Crash-visible, canonical JSONL sink with no wall-clock fields."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, fact: BlockFact) -> None:
        encoded = json.dumps(
            fact.log_record(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            self._stream.write(encoded + "\n")
            self._stream.flush()


@dataclass(frozen=True)
class ConnectDecision:
    """Policy result for a CONNECT authority."""

    allowed: bool
    host: str | None
    port: int | None
    block: BlockFact | None


def _split_connect_authority(authority: str) -> tuple[str, int]:
    if (
        not authority
        or any(ord(char) < 33 or ord(char) == 127 for char in authority)
        or any(char in authority for char in "/@?#")
    ):
        raise PolicyError("malformed CONNECT authority")
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0 or authority[closing + 1 : closing + 2] != ":":
            raise PolicyError("malformed bracketed CONNECT authority")
        host = authority[1:closing]
        port_text = authority[closing + 2 :]
    else:
        if authority.count(":") != 1:
            raise PolicyError("CONNECT authority must include exactly one port")
        host, port_text = authority.rsplit(":", 1)
    if not port_text.isascii() or not port_text.isdigit():
        raise PolicyError("CONNECT port must be decimal")
    port = int(port_text)
    if not 1 <= port <= 65_535:
        raise PolicyError("CONNECT port outside valid range")
    return host, port


def decide_connect(authority: str, policy: EndpointPolicy) -> ConnectDecision:
    """Evaluate one CONNECT target without opening a socket."""

    try:
        requested_host, port = _split_connect_authority(authority)
        canonical_host = _canonical_requested_domain(requested_host)
    except PolicyError:
        return ConnectDecision(
            allowed=False,
            host=None,
            port=None,
            block=BlockFact(
                destination=destination_token(authority, kind="opaque"),
                port=None,
                protocol="other",
                count=1,
                witness="proxy_connect",
            ),
        )
    if not policy.allows(canonical_host, port):
        return ConnectDecision(
            allowed=False,
            host=canonical_host,
            port=port,
            block=BlockFact(
                destination=destination_token(canonical_host, kind="domain"),
                port=port,
                protocol="https" if port == 443 else "tcp",
                count=1,
                witness="proxy_connect",
            ),
        )
    return ConnectDecision(
        allowed=True,
        host=canonical_host,
        port=port,
        block=None,
    )


def _need(data: bytes, offset: int, length: int) -> bytes:
    end = offset + length
    if offset < 0 or length < 0 or end > len(data):
        raise PolicyError("truncated TLS ClientHello")
    return data[offset:end]


def extract_tls_sni(client_hello_body: bytes) -> str:
    """Extract and validate the first host_name SNI from a ClientHello body."""

    offset = 0
    _need(client_hello_body, offset, 2 + 32)
    offset += 2 + 32

    session_id_length = _need(client_hello_body, offset, 1)[0]
    offset += 1
    _need(client_hello_body, offset, session_id_length)
    offset += session_id_length

    cipher_length = int.from_bytes(_need(client_hello_body, offset, 2), "big")
    offset += 2
    if cipher_length < 2 or cipher_length % 2:
        raise PolicyError("invalid TLS cipher-suite vector")
    _need(client_hello_body, offset, cipher_length)
    offset += cipher_length

    compression_length = _need(client_hello_body, offset, 1)[0]
    offset += 1
    if compression_length < 1:
        raise PolicyError("invalid TLS compression vector")
    _need(client_hello_body, offset, compression_length)
    offset += compression_length

    extensions_length = int.from_bytes(_need(client_hello_body, offset, 2), "big")
    offset += 2
    extensions_end = offset + extensions_length
    if extensions_end != len(client_hello_body):
        raise PolicyError("invalid TLS extensions length")

    while offset < extensions_end:
        extension_type = int.from_bytes(_need(client_hello_body, offset, 2), "big")
        extension_length = int.from_bytes(
            _need(client_hello_body, offset + 2, 2), "big"
        )
        offset += 4
        extension_data = _need(client_hello_body, offset, extension_length)
        offset += extension_length
        if extension_type != 0:
            continue
        if len(extension_data) < 5:
            raise PolicyError("invalid TLS SNI extension")
        names_length = int.from_bytes(extension_data[:2], "big")
        if names_length != len(extension_data) - 2:
            raise PolicyError("invalid TLS SNI name-list length")
        name_type = extension_data[2]
        name_length = int.from_bytes(extension_data[3:5], "big")
        if name_type != 0 or name_length != len(extension_data) - 5:
            raise PolicyError("TLS SNI must contain one host_name")
        try:
            requested = extension_data[5:].decode("ascii")
        except UnicodeDecodeError as exc:
            raise PolicyError("TLS SNI must be ASCII") from exc
        return _canonical_requested_domain(requested)
    raise PolicyError("TLS ClientHello has no SNI")


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise PolicyError("connection closed during TLS ClientHello")
        chunks.extend(chunk)
    return bytes(chunks)


def read_client_hello(sock: socket.socket) -> tuple[str, bytes]:
    """Read bounded TLS handshake records until one full ClientHello exists."""

    records = bytearray()
    handshake = bytearray()
    expected_handshake_bytes: int | None = None

    while len(records) < _MAX_CLIENT_HELLO_BYTES:
        header = _recv_exact(sock, 5)
        content_type = header[0]
        record_version = header[1:3]
        record_length = int.from_bytes(header[3:5], "big")
        if (
            content_type != _TLS_HANDSHAKE
            or record_version[0] != 3
            or record_length < 1
            or record_length > 18_432
        ):
            raise PolicyError("CONNECT tunnel did not begin with bounded TLS")
        payload = _recv_exact(sock, record_length)
        records.extend(header)
        records.extend(payload)
        if len(records) > _MAX_CLIENT_HELLO_BYTES:
            raise PolicyError("TLS ClientHello exceeds gateway limit")
        handshake.extend(payload)
        if expected_handshake_bytes is None and len(handshake) >= 4:
            if handshake[0] != _CLIENT_HELLO:
                raise PolicyError("first TLS handshake message is not ClientHello")
            expected_handshake_bytes = 4 + int.from_bytes(handshake[1:4], "big")
            if expected_handshake_bytes > _MAX_CLIENT_HELLO_BYTES:
                raise PolicyError("TLS ClientHello exceeds gateway limit")
        if expected_handshake_bytes is not None and len(handshake) >= expected_handshake_bytes:
            if len(handshake) != expected_handshake_bytes:
                raise PolicyError("unexpected TLS bytes before upstream connection")
            return extract_tls_sni(bytes(handshake[4:])), bytes(records)
    raise PolicyError("TLS ClientHello exceeds gateway limit")


@dataclass(frozen=True)
class ResolvedEndpoint:
    """One already-vetted public address returned for an allowed hostname."""

    family: int
    socket_type: int
    protocol: int
    socket_address: tuple[object, ...]
    ip: str


Resolver = Callable[
    [str, int, int, int],
    Sequence[tuple[int, int, int, str, tuple[object, ...]]],
]
Dialer = Callable[[ResolvedEndpoint, float], socket.socket]


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def resolve_public_endpoints(
    host: str,
    port: int,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> tuple[ResolvedEndpoint, ...]:
    """Resolve once, reject the entire answer set if any address is non-public.

    The returned sockaddr values, not ``host``, are passed to ``connect``.  A
    second DNS lookup therefore cannot swap a vetted public answer for a
    private or loopback address.
    """

    try:
        answers = resolver(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as exc:
        raise PolicyError("allowed endpoint did not resolve") from exc
    if not answers:
        raise PolicyError("allowed endpoint returned no addresses")
    endpoints: list[ResolvedEndpoint] = []
    seen: set[tuple[int, str, int]] = set()
    for family, socket_type, protocol, _canonical_name, socket_address in answers:
        if (
            family not in {socket.AF_INET, socket.AF_INET6}
            or socket_type != socket.SOCK_STREAM
            or len(socket_address) < 2
            or not isinstance(socket_address[0], str)
            or not isinstance(socket_address[1], int)
        ):
            raise PolicyError("allowed endpoint returned an unsupported address")
        ip = socket_address[0]
        answer_port = socket_address[1]
        if answer_port != port or not _is_public_ip(ip):
            raise PolicyError("allowed endpoint resolved outside public address space")
        key = (family, ip, answer_port)
        if key in seen:
            continue
        seen.add(key)
        endpoints.append(
            ResolvedEndpoint(
                family=family,
                socket_type=socket_type,
                protocol=protocol,
                socket_address=socket_address,
                ip=ip,
            )
        )
    if not endpoints:
        raise PolicyError("allowed endpoint returned no usable addresses")
    return tuple(endpoints)


def _default_dialer(endpoint: ResolvedEndpoint, timeout: float) -> socket.socket:
    upstream = socket.socket(
        endpoint.family,
        endpoint.socket_type,
        endpoint.protocol,
    )
    try:
        upstream.settimeout(timeout)
        upstream.connect(endpoint.socket_address)
        return upstream
    except BaseException:
        upstream.close()
        raise


class GatewayServer(socketserver.ThreadingTCPServer):
    """Threaded proxy server with injectable upstream dialing for tests."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        policy: EndpointPolicy,
        sink: JsonLineBlockSink,
        resolver: Resolver = socket.getaddrinfo,
        dialer: Dialer = _default_dialer,
        connect_timeout_s: float = 15.0,
        tunnel_idle_timeout_s: float = 180.0,
    ) -> None:
        self.policy = policy
        self.sink = sink
        self.resolver = resolver
        self.dialer = dialer
        self.connect_timeout_s = connect_timeout_s
        self.tunnel_idle_timeout_s = tunnel_idle_timeout_s
        super().__init__(server_address, GatewayRequestHandler)


class GatewayRequestHandler(BaseHTTPRequestHandler):
    """Fail-closed HTTP proxy surface: CONNECT only."""

    protocol_version = "HTTP/1.1"
    server_version = "TradeEvolveGateway"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        """Suppress BaseHTTPRequestHandler logs, which may contain secrets."""

    def _gateway(self) -> GatewayServer:
        return cast(GatewayServer, self.server)

    def _deny_non_connect(self) -> None:
        raw_target = f"{self.command} {self.path}"
        self._gateway().sink.emit(
            BlockFact(
                destination=destination_token(raw_target, kind="opaque"),
                port=None,
                protocol="other",
                count=1,
                witness="proxy_connect",
            )
        )
        self.send_response_only(403)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    do_GET = _deny_non_connect
    do_HEAD = _deny_non_connect
    do_POST = _deny_non_connect
    do_PUT = _deny_non_connect
    do_DELETE = _deny_non_connect
    do_OPTIONS = _deny_non_connect
    do_PATCH = _deny_non_connect

    def do_CONNECT(self) -> None:  # noqa: N802 - stdlib handler API
        gateway = self._gateway()
        decision = decide_connect(self.path, gateway.policy)
        if not decision.allowed:
            if decision.block is None:
                raise AssertionError("blocked decision must carry evidence")
            gateway.sink.emit(decision.block)
            self.send_response_only(403)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return

        if decision.host is None or decision.port is None:
            raise AssertionError("allowed decision must carry target")
        self.send_response_only(200, "Connection Established")
        self.end_headers()
        self.connection.settimeout(gateway.connect_timeout_s)
        try:
            sni, client_hello = read_client_hello(self.connection)
        except (OSError, PolicyError):
            gateway.sink.emit(
                BlockFact(
                    destination=destination_token(self.path, kind="opaque"),
                    port=decision.port,
                    protocol="https",
                    count=1,
                    witness="proxy_sni",
                )
            )
            self.close_connection = True
            return
        if sni != decision.host:
            gateway.sink.emit(
                BlockFact(
                    destination=destination_token(sni, kind="domain"),
                    port=decision.port,
                    protocol="https",
                    count=1,
                    witness="proxy_sni",
                )
            )
            self.close_connection = True
            return

        try:
            endpoints = resolve_public_endpoints(
                decision.host,
                decision.port,
                resolver=gateway.resolver,
            )
        except PolicyError:
            gateway.sink.emit(
                BlockFact(
                    destination=destination_token(decision.host, kind="domain"),
                    port=decision.port,
                    protocol="https",
                    count=1,
                    witness="proxy_resolution",
                )
            )
            self.close_connection = True
            return
        upstream: socket.socket | None = None
        for endpoint in endpoints:
            try:
                upstream = gateway.dialer(
                    endpoint,
                    gateway.connect_timeout_s,
                )
                break
            except OSError:
                continue
        if upstream is None:
            self.close_connection = True
            return
        try:
            upstream.sendall(client_hello)
            self.connection.settimeout(None)
            upstream.settimeout(None)
            _relay(
                self.connection,
                upstream,
                idle_timeout_s=gateway.tunnel_idle_timeout_s,
            )
        finally:
            upstream.close()
            self.close_connection = True


def _relay(
    client: socket.socket,
    upstream: socket.socket,
    *,
    idle_timeout_s: float,
) -> None:
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    try:
        while True:
            ready = selector.select(timeout=idle_timeout_s)
            if not ready:
                return
            for key, _ in ready:
                source = cast(socket.socket, key.fileobj)
                destination = cast(socket.socket, key.data)
                data = source.recv(65_536)
                if not data:
                    return
                destination.sendall(data)
    except OSError:
        return
    finally:
        selector.close()


def _load_policy(raw_json: str) -> EndpointPolicy:
    try:
        value = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise PolicyError("ENDPOINT_DOMAINS_JSON is not JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyError("ENDPOINT_DOMAINS_JSON must be a string array")
    return EndpointPolicy.from_domains(cast(list[str], value))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeEvolve egress gateway")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3128)
    parser.add_argument(
        "--endpoint-domains-json",
        required=True,
        help="exact lowercase hostname array; never credentials",
    )
    args = parser.parse_args(argv)
    try:
        policy = _load_policy(args.endpoint_domains_json)
    except PolicyError:
        return 2
    if not 1 <= args.port <= 65_535:
        return 2
    sink = JsonLineBlockSink(sys.stdout)
    with GatewayServer((args.listen, args.port), policy=policy, sink=sink) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
