# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import socket
import struct
from pathlib import Path

import pytest

from sandbox.docker.guard.guard_main import _assert_ipv6_disabled
from sandbox.firewall import FirewallPlan, parse_dns_qname, parse_original_destination


def _dns_query(name: str) -> bytes:
    labels = b"".join(
        bytes([len(label)]) + label.encode("ascii") for label in name.split(".")
    )
    return (
        b"\x12\x34"
        + b"\x01\x00"
        + b"\x00\x01"
        + b"\x00\x00\x00\x00\x00\x00"
        + labels
        + b"\x00\x00\x01\x00\x01"
    )


def test_firewall_routes_direct_tcp_and_dns_to_collectors_then_rejects_rest() -> None:
    plan = FirewallPlan(agent_uid=65532, gateway_ipv4="172.30.240.2")
    rules = plan.restore_text()

    assert "--uid-owner 65532" in rules
    assert "-d 127.0.0.1/32 -p tcp --dport 8000 -j RETURN" in rules
    assert "-d 172.30.240.2/32 -p tcp --dport 3128 -j RETURN" in rules
    assert "-p tcp -j REDIRECT --to-ports 15001" in rules
    assert "-p udp -j REDIRECT --to-ports 15002" in rules
    assert "-A TE_FILTER -p tcp --dport 15001 -j RETURN" in rules
    assert "-A TE_FILTER -p udp --dport 15002 -j RETURN" in rules
    assert "-A TE_FILTER -j REJECT" in rules
    checks = {" ".join(command) for command in plan.check_commands()}
    assert len(checks) == 13
    assert any(
        "-d 127.0.0.1/32 -o lo -p tcp --dport 8000 -j RETURN"
        in command
        for command in checks
    )
    assert any(
        "-C TE_FILTER -p tcp --dport 15001 -j RETURN" in command
        for command in checks
    )
    assert any(
        "-C TE_FILTER -p udp --dport 15002 -j RETURN" in command
        for command in checks
    )
    assert plan.sha256().startswith("sha256:")


@pytest.mark.parametrize(
    "gateway",
    ["example.com", "127.0.0.1; ACCEPT", "::1", "0.0.0.0", "224.0.0.1"],
)
def test_firewall_rejects_nonliteral_or_nonsensical_gateway(gateway: str) -> None:
    with pytest.raises(ValueError):
        FirewallPlan(agent_uid=65532, gateway_ipv4=gateway)


def test_original_destination_parser_uses_network_byte_order() -> None:
    raw = (
        struct.pack("=H", socket.AF_INET)
        + (443).to_bytes(2, "big")
        + socket.inet_aton("203.0.113.8")
        + (b"\x00" * 8)
    )
    assert parse_original_destination(raw) == ("203.0.113.8", 443)


def test_dns_parser_extracts_encoded_exfil_name_for_hashing() -> None:
    assert parse_dns_qname(_dns_query("keymaterial.exfil.example")) == (
        "keymaterial.exfil.example"
    )
    with pytest.raises(ValueError):
        parse_dns_qname(b"\x00" * 11)


def _proc_fixture(root: Path, *, interface: str = "lo", route: str = "lo") -> None:
    for relative in (
        "sys/net/ipv6/conf/all/disable_ipv6",
        "sys/net/ipv6/conf/default/disable_ipv6",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("1\n", encoding="ascii")
    net = root / "net"
    net.mkdir(parents=True, exist_ok=True)
    (net / "if_inet6").write_text(
        f"{'0' * 32} 01 80 10 80 {interface}\n",
        encoding="ascii",
    )
    (net / "ipv6_route").write_text(
        f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} 00000000 00 00 00000000 {route}\n",
        encoding="ascii",
    )


def test_ipv6_proof_accepts_only_disabled_loopback_namespace(tmp_path: Path) -> None:
    _proc_fixture(tmp_path)
    _assert_ipv6_disabled(tmp_path)


@pytest.mark.parametrize(
    ("interface", "route"),
    [("eth0", "lo"), ("lo", "eth0")],
)
def test_ipv6_proof_refuses_any_nonloopback_surface(
    tmp_path: Path,
    interface: str,
    route: str,
) -> None:
    _proc_fixture(tmp_path, interface=interface, route=route)
    with pytest.raises(RuntimeError):
        _assert_ipv6_disabled(tmp_path)
