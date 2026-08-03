# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from harness.protocol import HarnessEventSource
from sandbox.events import (
    BlockEventBuffer,
    BlockRecordError,
    parse_block_record,
)
from sandbox.gateway import BlockFact, destination_token

ROOT = Path(__file__).resolve().parents[2]


def _line(destination: str = "denied.example") -> str:
    fact = BlockFact(
        destination=destination_token(destination, kind="domain"),
        port=443,
        protocol="https",
        count=1,
        witness="proxy_connect",
    )
    return json.dumps(
        fact.log_record(),
        sort_keys=True,
        separators=(",", ":"),
    )


def test_collector_record_maps_to_frozen_egress_payload() -> None:
    parsed = parse_block_record(_line())
    event = {
        "schema": "event/v1",
        "seq": 0,
        "ts": 1,
        "turn": 0,
        "bar_index": 0,
        "source": "harness",
        "type": "EgressBlocked",
        "payload": parsed.event_payload(),
    }
    schema = json.loads(
        (ROOT / "spec/schemas/event.v1.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(event)


def test_parser_rejects_raw_destination_even_if_other_fields_are_valid() -> None:
    raw = json.loads(_line())
    raw["event_payload"]["destination"] = "key-secret.exfil.example"
    with pytest.raises(BlockRecordError, match="digest token"):
        parse_block_record(json.dumps(raw))


def test_parser_rejects_duplicates_and_fractional_numbers() -> None:
    valid = _line()
    with pytest.raises(BlockRecordError, match="duplicate"):
        parse_block_record(valid[:-1] + ',"schema":"sandbox_egress_block/v1"}')
    with pytest.raises(BlockRecordError, match="fractional"):
        parse_block_record(valid.replace('"count":1', '"count":1.5'))


def test_buffer_deduplicates_cursors_not_attempt_payloads() -> None:
    buffer = BlockEventBuffer()
    assert isinstance(buffer, HarnessEventSource)
    line = _line()
    assert buffer.append(source="guard", cursor="1", line=line)
    assert not buffer.append(source="guard", cursor="1", line=line)
    assert buffer.append(source="guard", cursor="2", line=line)

    drained = buffer.drain_harness_events()
    assert len(drained) == 2
    assert drained[0].type == "EgressBlocked"
    assert drained[0].payload == drained[1].payload
    assert buffer.drain_event_payloads() == ()
