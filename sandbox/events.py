# SPDX-License-Identifier: Apache-2.0
"""Strict, secret-safe bridge from sandbox JSONL to harness event payloads."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Iterable

from harness.protocol import HarnessEvent

_DESTINATION_RE = re.compile(
    r"^(?:domain|dns|dns-server|ipv4|opaque)-sha256:[0-9a-f]{64}$"
)
_WITNESSES = {
    "proxy_connect",
    "proxy_resolution",
    "proxy_sni",
    "kernel_redirect",
}
_PROTOCOLS = {"https", "dns", "tcp", "udp", "other"}
_MAX_LINE_BYTES = 4096


class BlockRecordError(ValueError):
    """A collector line is malformed or could leak raw destination text."""


def _object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise BlockRecordError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_float(_value: str) -> object:
    raise BlockRecordError("fractional JSON numbers are forbidden")


@dataclass(frozen=True)
class ParsedBlockRecord:
    """Validated source fact before virtual-clock/turn assignment."""

    witness: str
    destination: str
    port: int | None
    protocol: str
    count: int

    def event_payload(self) -> dict[str, object]:
        return {
            "destination": self.destination,
            "port": self.port,
            "protocol": self.protocol,
            "count": self.count,
        }


def parse_block_record(line: str | bytes) -> ParsedBlockRecord:
    """Parse one collector line and reject any raw destination."""

    raw = line.encode("utf-8") if isinstance(line, str) else line
    raw = raw.rstrip(b"\r\n")
    if not raw or len(raw) > _MAX_LINE_BYTES:
        raise BlockRecordError("collector line is empty or oversized")
    try:
        decoded = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BlockRecordError("collector line must be ASCII") from exc
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_object_no_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except json.JSONDecodeError as exc:
        raise BlockRecordError("collector line is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "witness",
        "event_payload",
    }:
        raise BlockRecordError("collector envelope has unexpected fields")
    if value["schema"] != "sandbox_egress_block/v1":
        raise BlockRecordError("collector schema mismatch")
    witness = value["witness"]
    if not isinstance(witness, str) or witness not in _WITNESSES:
        raise BlockRecordError("collector witness is invalid")
    payload = value["event_payload"]
    if not isinstance(payload, dict) or set(payload) != {
        "destination",
        "port",
        "protocol",
        "count",
    }:
        raise BlockRecordError("collector payload has unexpected fields")
    destination = payload["destination"]
    port = payload["port"]
    protocol = payload["protocol"]
    count = payload["count"]
    if not isinstance(destination, str) or not _DESTINATION_RE.fullmatch(destination):
        raise BlockRecordError("destination must be a secret-safe digest token")
    if port is not None and (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 0 <= port <= 65_535
    ):
        raise BlockRecordError("collector port is invalid")
    if not isinstance(protocol, str) or protocol not in _PROTOCOLS:
        raise BlockRecordError("collector protocol is invalid")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
    ):
        raise BlockRecordError("collector count is invalid")
    return ParsedBlockRecord(
        witness=witness,
        destination=destination,
        port=port,
        protocol=protocol,
        count=count,
    )


class BlockEventBuffer:
    """Cursor-deduplicated buffer for interleaved gateway/guard logs.

    Docker log cursors are source-specific and supplied by the caller.  Facts
    are never deduplicated by payload because repeated attempts are evidence.
    """

    def __init__(self) -> None:
        self._records: list[ParsedBlockRecord] = []
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def append(self, *, source: str, cursor: str, line: str | bytes) -> bool:
        if not source or not cursor:
            raise ValueError("source and cursor must be non-empty")
        parsed = parse_block_record(line)
        identity = (source, cursor)
        with self._lock:
            if identity in self._seen:
                return False
            self._seen.add(identity)
            self._records.append(parsed)
        return True

    def extend(
        self,
        records: Iterable[tuple[str, str, str | bytes]],
    ) -> int:
        accepted = 0
        for source, cursor, line in records:
            if self.append(source=source, cursor=cursor, line=line):
                accepted += 1
        return accepted

    def drain_event_payloads(self) -> tuple[dict[str, object], ...]:
        """Drain payloads in ingestion order for ``HarnessEvent`` wrapping."""

        with self._lock:
            drained = tuple(record.event_payload() for record in self._records)
            self._records.clear()
        return drained

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        """Implement ``HarnessEventSource`` without coupling log ingestion."""

        return tuple(
            HarnessEvent(type="EgressBlocked", payload=payload)
            for payload in self.drain_event_payloads()
        )
