# SPDX-License-Identifier: Apache-2.0
"""Generate and verify the agent network-namespace firewall.

The trusted guard installs these rules before the untrusted agent joins its
network namespace.  Rules apply only to the fixed unprivileged agent UID:

* established response traffic remains possible;
* the exact loopback agent HTTP service remains reachable for health checks;
* one TCP connection to the exact-domain gateway is allowed;
* every other new TCP or UDP attempt is redirected to local collectors;
* all remaining protocols are rejected as defense in depth.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass

_CHAIN_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")


def _port(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1024 <= value <= 65_535:
        raise ValueError(f"{name} must be an unprivileged TCP/UDP port")
    return value


def _uid(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2**31 - 1:
        raise ValueError("agent_uid must be a positive integer")
    return value


@dataclass(frozen=True)
class FirewallPlan:
    """Validated iptables rules for one agent namespace."""

    agent_uid: int
    gateway_ipv4: str
    agent_http_port: int = 8000
    gateway_port: int = 3128
    collector_tcp_port: int = 15001
    collector_udp_port: int = 15002
    nat_chain: str = "TE_EGRESS"
    filter_chain: str = "TE_FILTER"

    def __post_init__(self) -> None:
        _uid(self.agent_uid)
        try:
            address = ipaddress.ip_address(self.gateway_ipv4)
        except ValueError as exc:
            raise ValueError("gateway_ipv4 must be a literal IPv4 address") from exc
        if address.version != 4 or address.is_unspecified or address.is_multicast:
            raise ValueError("gateway_ipv4 must be one routable namespace IPv4")
        _port(self.gateway_port, "gateway_port")
        _port(self.agent_http_port, "agent_http_port")
        _port(self.collector_tcp_port, "collector_tcp_port")
        _port(self.collector_udp_port, "collector_udp_port")
        if len(
            {
                self.gateway_port,
                self.agent_http_port,
                self.collector_tcp_port,
                self.collector_udp_port,
            }
        ) != 4:
            raise ValueError(
                "agent HTTP, gateway, and collector ports must be distinct"
            )
        if not _CHAIN_RE.fullmatch(self.nat_chain):
            raise ValueError("invalid nat chain name")
        if not _CHAIN_RE.fullmatch(self.filter_chain):
            raise ValueError("invalid filter chain name")

    def restore_text(self) -> str:
        """Return an injection-safe iptables-restore program."""

        uid = str(self.agent_uid)
        gateway = f"{self.gateway_ipv4}/32"
        agent_http_port = str(self.agent_http_port)
        gateway_port = str(self.gateway_port)
        tcp_port = str(self.collector_tcp_port)
        udp_port = str(self.collector_udp_port)
        lines = [
            "*nat",
            f":{self.nat_chain} - [0:0]",
            (
                f"-A OUTPUT -m owner --uid-owner {uid} -p tcp "
                f"-m conntrack --ctstate NEW -j {self.nat_chain}"
            ),
            f"-A OUTPUT -m owner --uid-owner {uid} -p udp -j {self.nat_chain}",
            (
                f"-A {self.nat_chain} -d 127.0.0.1/32 -p tcp "
                f"--dport {agent_http_port} -j RETURN"
            ),
            (
                f"-A {self.nat_chain} -d {gateway} -p tcp "
                f"--dport {gateway_port} -j RETURN"
            ),
            (
                f"-A {self.nat_chain} -p tcp -j REDIRECT "
                f"--to-ports {tcp_port}"
            ),
            (
                f"-A {self.nat_chain} -p udp -j REDIRECT "
                f"--to-ports {udp_port}"
            ),
            "COMMIT",
            "*filter",
            f":{self.filter_chain} - [0:0]",
            f"-A OUTPUT -m owner --uid-owner {uid} -j {self.filter_chain}",
            (
                f"-A {self.filter_chain} -m conntrack "
                "--ctstate ESTABLISHED,RELATED -j RETURN"
            ),
            (
                f"-A {self.filter_chain} -d 127.0.0.1/32 -o lo -p tcp "
                f"--dport {agent_http_port} -j RETURN"
            ),
            (
                f"-A {self.filter_chain} -d {gateway} -p tcp "
                f"--dport {gateway_port} -j RETURN"
            ),
            (
                f"-A {self.filter_chain} -p tcp "
                f"--dport {tcp_port} -j RETURN"
            ),
            (
                f"-A {self.filter_chain} -p udp "
                f"--dport {udp_port} -j RETURN"
            ),
            f"-A {self.filter_chain} -j REJECT",
            "COMMIT",
            "",
        ]
        return "\n".join(lines)

    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.restore_text().encode("ascii")).hexdigest()

    def check_commands(self) -> tuple[tuple[str, ...], ...]:
        """Return shell-free checks proving every required rule is present."""

        uid = str(self.agent_uid)
        gateway = f"{self.gateway_ipv4}/32"
        agent_http_port = str(self.agent_http_port)
        gateway_port = str(self.gateway_port)
        tcp_port = str(self.collector_tcp_port)
        udp_port = str(self.collector_udp_port)
        return (
            (
                "iptables",
                "-t",
                "nat",
                "-C",
                "OUTPUT",
                "-m",
                "owner",
                "--uid-owner",
                uid,
                "-p",
                "tcp",
                "-m",
                "conntrack",
                "--ctstate",
                "NEW",
                "-j",
                self.nat_chain,
            ),
            (
                "iptables",
                "-t",
                "nat",
                "-C",
                "OUTPUT",
                "-m",
                "owner",
                "--uid-owner",
                uid,
                "-p",
                "udp",
                "-j",
                self.nat_chain,
            ),
            (
                "iptables",
                "-t",
                "nat",
                "-C",
                self.nat_chain,
                "-d",
                "127.0.0.1/32",
                "-p",
                "tcp",
                "--dport",
                agent_http_port,
                "-j",
                "RETURN",
            ),
            (
                "iptables",
                "-t",
                "nat",
                "-C",
                self.nat_chain,
                "-d",
                gateway,
                "-p",
                "tcp",
                "--dport",
                gateway_port,
                "-j",
                "RETURN",
            ),
            (
                "iptables",
                "-t",
                "nat",
                "-C",
                self.nat_chain,
                "-p",
                "tcp",
                "-j",
                "REDIRECT",
                "--to-ports",
                tcp_port,
            ),
            (
                "iptables",
                "-t",
                "nat",
                "-C",
                self.nat_chain,
                "-p",
                "udp",
                "-j",
                "REDIRECT",
                "--to-ports",
                udp_port,
            ),
            (
                "iptables",
                "-t",
                "filter",
                "-C",
                "OUTPUT",
                "-m",
                "owner",
                "--uid-owner",
                uid,
                "-j",
                self.filter_chain,
            ),
            (
                "iptables",
                "-t",
                "filter",
                "-C",
                self.filter_chain,
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "RETURN",
            ),
            (
                "iptables",
                "-t",
                "filter",
                "-C",
                self.filter_chain,
                "-d",
                "127.0.0.1/32",
                "-o",
                "lo",
                "-p",
                "tcp",
                "--dport",
                agent_http_port,
                "-j",
                "RETURN",
            ),
            (
                "iptables",
                "-t",
                "filter",
                "-C",
                self.filter_chain,
                "-d",
                gateway,
                "-p",
                "tcp",
                "--dport",
                gateway_port,
                "-j",
                "RETURN",
            ),
            (
                "iptables",
                "-t",
                "filter",
                "-C",
                self.filter_chain,
                "-p",
                "tcp",
                "--dport",
                tcp_port,
                "-j",
                "RETURN",
            ),
            (
                "iptables",
                "-t",
                "filter",
                "-C",
                self.filter_chain,
                "-p",
                "udp",
                "--dport",
                udp_port,
                "-j",
                "RETURN",
            ),
            (
                "iptables",
                "-t",
                "filter",
                "-C",
                self.filter_chain,
                "-j",
                "REJECT",
            ),
        )
