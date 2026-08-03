# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib

from sandbox.docker.guard.block_collector import _fact_for_destination


def _token(kind: str, value: bytes) -> str:
    return f"{kind}-sha256:{hashlib.sha256(value).hexdigest()}"


def _dns_query(name: str) -> bytes:
    labels = b"".join(
        bytes([len(label)]) + label.encode("ascii")
        for label in name.split(".")
    )
    return (
        b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        + labels
        + b"\x00\x00\x01\x00\x01"
    )


def test_udp_dns_classification_does_not_depend_on_original_port() -> None:
    name = "encoded.iso2-proof.invalid"
    fact = _fact_for_destination(
        "172.20.0.3",
        15002,
        "udp",
        dns_packet=_dns_query(name),
    )

    assert fact.log_record()["event_payload"] == {
        "destination": _token("dns", name.encode("ascii")),
        "port": None,
        "protocol": "dns",
        "count": 1,
    }


def test_generic_udp_uses_payload_digest_when_original_destination_is_lost() -> None:
    payload = b"tradeevolve-iso2-proof"
    fact = _fact_for_destination(
        "172.20.0.3",
        15002,
        "udp",
        dns_packet=payload,
    )

    assert fact.log_record()["event_payload"] == {
        "destination": _token("opaque", payload),
        "port": None,
        "protocol": "udp",
        "count": 1,
    }
