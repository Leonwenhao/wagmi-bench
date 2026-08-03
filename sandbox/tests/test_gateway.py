# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import socket
import threading
from collections.abc import Sequence
from typing import cast

import pytest

from sandbox.gateway import (
    BlockFact,
    EndpointPolicy,
    GatewayServer,
    JsonLineBlockSink,
    PolicyError,
    ResolvedEndpoint,
    decide_connect,
    destination_token,
    extract_tls_sni,
    resolve_public_endpoints,
)


def _client_hello(host: str) -> tuple[bytes, bytes]:
    encoded_host = host.encode("ascii")
    sni_names = b"\x00" + len(encoded_host).to_bytes(2, "big") + encoded_host
    sni_extension = (
        b"\x00\x00"
        + (len(sni_names) + 2).to_bytes(2, "big")
        + len(sni_names).to_bytes(2, "big")
        + sni_names
    )
    body = (
        b"\x03\x03"
        + (b"\x11" * 32)
        + b"\x00"
        + b"\x00\x02"
        + b"\x13\x01"
        + b"\x01\x00"
        + len(sni_extension).to_bytes(2, "big")
        + sni_extension
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    record = b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake
    return body, record


def test_policy_is_exact_and_https_only() -> None:
    policy = EndpointPolicy.from_domains(["api.example.com"])

    assert decide_connect("api.example.com:443", policy).allowed
    assert decide_connect("API.EXAMPLE.COM:443", policy).allowed
    for authority in (
        "sub.api.example.com:443",
        "api.example.com.:443",
        "api.example.com:80",
        "127.0.0.1:443",
        "[::1]:443",
        "user@api.example.com:443",
        "api.example.com",
    ):
        decision = decide_connect(authority, policy)
        assert not decision.allowed
        assert decision.block is not None


@pytest.mark.parametrize(
    "domain",
    [
        "API.example.com",
        "api.example.com.",
        "https://api.example.com",
        "api.example.com:443",
        "127.0.0.1",
        "*.example.com",
        "singlelabel",
    ],
)
def test_manifest_domains_reject_ambiguous_forms(domain: str) -> None:
    with pytest.raises(PolicyError):
        EndpointPolicy.from_domains([domain])


def test_block_log_never_contains_denied_destination_or_secret() -> None:
    secret = "fw-key-DO-NOT-LOG"
    destination = f"{secret}.exfil.example"
    output = io.StringIO()
    sink = JsonLineBlockSink(output)
    sink.emit(
        BlockFact(
            destination=destination_token(destination, kind="domain"),
            port=443,
            protocol="https",
            count=1,
            witness="proxy_connect",
        )
    )

    encoded = output.getvalue()
    assert secret not in encoded
    assert destination not in encoded
    assert encoded.endswith("\n")


def test_tls_sni_parser_requires_one_exact_ascii_hostname() -> None:
    body, _record = _client_hello("api.example.com")
    assert extract_tls_sni(body) == "api.example.com"

    no_extensions = body[:- len(body[-(len("api.example.com") + 9) :])]
    with pytest.raises(PolicyError):
        extract_tls_sni(no_extensions)


def test_resolution_rejects_entire_mixed_public_private_answer_set() -> None:
    def mixed(
        _host: str,
        port: int,
        _family: int,
        _kind: int,
    ) -> Sequence[tuple[int, int, int, str, tuple[object, ...]]]:
        return (
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        )

    with pytest.raises(PolicyError, match="public address space"):
        resolve_public_endpoints("api.example.com", 443, resolver=mixed)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.1.1",
        "192.0.2.1",
        "224.0.0.1",
        "::",
        "::1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_resolution_rejects_non_global_ip_classes(address: str) -> None:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def resolver(
        _host: str,
        port: int,
        _family: int,
        _kind: int,
    ) -> Sequence[tuple[int, int, int, str, tuple[object, ...]]]:
        sockaddr: tuple[object, ...]
        if family == socket.AF_INET6:
            sockaddr = (address, port, 0, 0)
        else:
            sockaddr = (address, port)
        return ((family, socket.SOCK_STREAM, 6, "", sockaddr),)

    with pytest.raises(PolicyError):
        resolve_public_endpoints("api.example.com", 443, resolver=resolver)


def test_gateway_dials_vetted_literal_once_after_matching_sni() -> None:
    body, hello = _client_hello("api.example.com")
    assert extract_tls_sni(body) == "api.example.com"
    output = io.StringIO()
    resolver_calls: list[str] = []
    dialed: list[ResolvedEndpoint] = []
    upstream_gateway, upstream_peer = socket.socketpair()

    def resolver(
        host: str,
        port: int,
        _family: int,
        _kind: int,
    ) -> Sequence[tuple[int, int, int, str, tuple[object, ...]]]:
        resolver_calls.append(host)
        return (
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", port),
            ),
        )

    def dialer(endpoint: ResolvedEndpoint, _timeout: float) -> socket.socket:
        dialed.append(endpoint)
        return upstream_gateway

    try:
        server = GatewayServer(
            ("127.0.0.1", 0),
            policy=EndpointPolicy.from_domains(["api.example.com"]),
            sink=JsonLineBlockSink(output),
            resolver=resolver,
            dialer=dialer,
        )
    except PermissionError:
        pytest.skip("test sandbox forbids loopback bind")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(
        cast(tuple[str, int], server.server_address),
        timeout=2,
    )
    client.settimeout(2)
    upstream_peer.settimeout(2)
    try:
        client.sendall(
            b"CONNECT api.example.com:443 HTTP/1.1\r\n"
            b"Host: api.example.com:443\r\n\r\n"
        )
        response = client.recv(4096)
        assert response.startswith(b"HTTP/1.1 200")
        client.sendall(hello)
        assert upstream_peer.recv(len(hello)) == hello
    finally:
        client.close()
        upstream_peer.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert resolver_calls == ["api.example.com"]
    assert [endpoint.ip for endpoint in dialed] == ["93.184.216.34"]
    assert output.getvalue() == ""


def test_gateway_blocks_sni_mismatch_before_dns_or_dial() -> None:
    _body, hello = _client_hello("other.example.com")
    output = io.StringIO()
    resolver_called = False

    def resolver(
        _host: str,
        _port: int,
        _family: int,
        _kind: int,
    ) -> Sequence[tuple[int, int, int, str, tuple[object, ...]]]:
        nonlocal resolver_called
        resolver_called = True
        return ()

    try:
        server = GatewayServer(
            ("127.0.0.1", 0),
            policy=EndpointPolicy.from_domains(["api.example.com"]),
            sink=JsonLineBlockSink(output),
            resolver=resolver,
        )
    except PermissionError:
        pytest.skip("test sandbox forbids loopback bind")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(
        cast(tuple[str, int], server.server_address),
        timeout=2,
    )
    client.settimeout(2)
    try:
        client.sendall(
            b"CONNECT api.example.com:443 HTTP/1.1\r\n"
            b"Host: api.example.com:443\r\n\r\n"
        )
        assert client.recv(4096).startswith(b"HTTP/1.1 200")
        client.sendall(hello)
        assert client.recv(1) == b""
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert not resolver_called
    assert '"witness":"proxy_sni"' in output.getvalue()
