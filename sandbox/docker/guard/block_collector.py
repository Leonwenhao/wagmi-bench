# SPDX-License-Identifier: Apache-2.0
"""Secret-safe collectors for iptables REDIRECT targets."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import sys
import threading
from dataclasses import dataclass
from typing import TextIO

SO_ORIGINAL_DST = 80
IP_RECVORIGDSTADDR = 20
_MAX_DNS_PACKET = 4096


def _destination_token(value: str | bytes, *, kind: str) -> str:
    raw = value.encode("utf-8", "surrogatepass") if isinstance(value, str) else value
    return f"{kind}-sha256:{hashlib.sha256(raw).hexdigest()}"


@dataclass(frozen=True)
class _BlockFact:
    destination: str
    port: int | None
    protocol: str
    count: int = 1
    witness: str = "kernel_redirect"

    def log_record(self) -> dict[str, object]:
        return {
            "schema": "sandbox_egress_block/v1",
            "witness": self.witness,
            "event_payload": {
                "destination": self.destination,
                "port": self.port,
                "protocol": self.protocol,
                "count": self.count,
            },
        }


class _Sink:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, fact: _BlockFact) -> None:
        line = json.dumps(
            fact.log_record(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


def parse_original_destination(raw: bytes) -> tuple[str, int]:
    """Parse Linux ``sockaddr_in`` bytes returned by SO_ORIGINAL_DST."""

    if len(raw) < 8:
        raise ValueError("truncated sockaddr_in")
    family = struct.unpack_from("=H", raw, 0)[0]
    if family != socket.AF_INET:
        raise ValueError("only IPv4 original destinations are supported")
    port = int.from_bytes(raw[2:4], "big")
    return socket.inet_ntoa(raw[4:8]), port


def parse_dns_qname(packet: bytes) -> str:
    """Parse one uncompressed DNS question name without retaining its bytes."""

    if len(packet) < 12:
        raise ValueError("truncated DNS header")
    question_count = int.from_bytes(packet[4:6], "big")
    if question_count != 1:
        raise ValueError("collector accepts exactly one DNS question")
    offset = 12
    labels: list[str] = []
    total = 0
    while True:
        if offset >= len(packet):
            raise ValueError("truncated DNS qname")
        length = packet[offset]
        offset += 1
        if length == 0:
            break
        if length & 0xC0 or length > 63:
            raise ValueError("compressed or invalid DNS qname")
        if offset + length > len(packet):
            raise ValueError("truncated DNS label")
        raw_label = packet[offset : offset + length]
        offset += length
        total += length + 1
        if total > 253:
            raise ValueError("DNS qname exceeds 253 bytes")
        try:
            label = raw_label.decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise ValueError("DNS qname must be ASCII") from exc
        labels.append(label)
    if not labels or offset + 4 > len(packet):
        raise ValueError("DNS question has no complete qtype/qclass")
    return ".".join(labels)


def _tcp_dns_payload(connection: socket.socket) -> bytes | None:
    connection.settimeout(0.05)
    try:
        prefix = connection.recv(2, socket.MSG_PEEK)
        if len(prefix) != 2:
            return None
        length = int.from_bytes(prefix, "big")
        if length < 12 or length > _MAX_DNS_PACKET:
            return None
        framed = connection.recv(length + 2, socket.MSG_PEEK)
        if len(framed) != length + 2:
            return None
        return framed[2:]
    except OSError:
        return None


def _fact_for_destination(
    address: str,
    port: int,
    protocol: str,
    dns_packet: bytes | None = None,
) -> _BlockFact:
    # Some REDIRECT implementations expose only the post-NAT UDP destination
    # in IP_RECVORIGDSTADDR. Classify UDP from its already-captured payload so
    # DNS and generic datagrams remain portable, deterministic, and secret-safe.
    if protocol == "udp" and dns_packet is not None:
        try:
            qname = parse_dns_qname(dns_packet)
        except ValueError:
            qname = ""
        if qname:
            return _BlockFact(
                destination=_destination_token(qname, kind="dns"),
                port=None,
                protocol="dns",
            )
        return _BlockFact(
            destination=_destination_token(dns_packet, kind="opaque"),
            port=None,
            protocol="udp",
        )
    if port == 53:
        protocol = "dns"
        if dns_packet is not None:
            try:
                qname = parse_dns_qname(dns_packet)
            except ValueError:
                qname = ""
            if qname:
                return _BlockFact(
                    destination=_destination_token(qname, kind="dns"),
                    port=None,
                    protocol="dns",
                )
        return _BlockFact(
            destination=_destination_token(address, kind="dns-server"),
            port=None,
            protocol="dns",
        )
    return _BlockFact(
        destination=_destination_token(address, kind="ipv4"),
        port=port,
        protocol=protocol,
    )


class BlockCollector:
    """TCP and UDP listeners receiving namespace-redirected attempts."""

    def __init__(
        self,
        *,
        tcp_port: int,
        udp_port: int,
        stream: TextIO = sys.stdout,
    ) -> None:
        self._tcp_port = tcp_port
        self._udp_port = udp_port
        self._sink = _Sink(stream)
        self._stop = threading.Event()
        self._tcp_socket: socket.socket | None = None
        self._udp_socket: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("collector already started")
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp.bind(("0.0.0.0", self._tcp_port))
            tcp.listen(128)
            tcp.settimeout(0.2)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp.setsockopt(socket.SOL_IP, IP_RECVORIGDSTADDR, 1)
            udp.bind(("0.0.0.0", self._udp_port))
            udp.settimeout(0.2)
        except BaseException:
            tcp.close()
            udp.close()
            raise
        self._tcp_socket = tcp
        self._udp_socket = udp
        self._threads = [
            threading.Thread(target=self._tcp_loop, name="egress-tcp", daemon=True),
            threading.Thread(target=self._udp_loop, name="egress-udp", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._tcp_socket is not None:
            self._tcp_socket.close()
        if self._udp_socket is not None:
            self._udp_socket.close()
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads.clear()

    def _tcp_loop(self) -> None:
        if self._tcp_socket is None:
            raise AssertionError("TCP collector socket missing")
        while not self._stop.is_set():
            try:
                connection, peer = self._tcp_socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                try:
                    raw = connection.getsockopt(
                        socket.SOL_IP,
                        SO_ORIGINAL_DST,
                        16,
                    )
                    address, port = parse_original_destination(raw)
                except (OSError, ValueError):
                    address, port = peer[0], 0
                dns_packet = _tcp_dns_payload(connection) if port == 53 else None
                self._sink.emit(
                    _fact_for_destination(
                        address,
                        port,
                        "tcp",
                        dns_packet=dns_packet,
                    )
                )
            finally:
                connection.close()

    def _udp_loop(self) -> None:
        if self._udp_socket is None:
            raise AssertionError("UDP collector socket missing")
        while not self._stop.is_set():
            try:
                data, ancillary, _flags, peer = self._udp_socket.recvmsg(
                    _MAX_DNS_PACKET,
                    64,
                )
            except TimeoutError:
                continue
            except OSError:
                return
            address, port = peer[0], 0
            for level, kind, raw in ancillary:
                if level == socket.SOL_IP and kind == IP_RECVORIGDSTADDR:
                    try:
                        address, port = parse_original_destination(raw)
                    except ValueError:
                        pass
                    break
            self._sink.emit(
                _fact_for_destination(
                    address,
                    port,
                    "udp",
                    dns_packet=data,
                )
            )
