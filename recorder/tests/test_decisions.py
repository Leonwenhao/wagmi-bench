# SPDX-License-Identifier: Apache-2.0
"""IC-5 decision-record projection over the full golden engine streams."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from core.config import EpisodeConfig
from core.engine import EpisodeResult, run_episode
from harness.scripted import ScriptedFixtureAgent
from recorder.decisions import (
    DecisionProjectionError,
    generate_decision_records,
)
from spec.canonical import canonical_bytes

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "fixtures" / "golden-mini"
SCHEMA_DIR = ROOT / "spec" / "schemas"

EVENT_SCHEMA = json.loads(
    (SCHEMA_DIR / "event.v1.schema.json").read_text(encoding="utf-8")
)
DECISION_SCHEMA = json.loads(
    (SCHEMA_DIR / "decision_record.v1.schema.json").read_text(
        encoding="utf-8"
    )
)
REGISTRY = Registry().with_resources(
    (
        cast(str, schema["$id"]),
        Resource.from_contents(schema),
    )
    for schema in (EVENT_SCHEMA, DECISION_SCHEMA)
)
VALIDATOR = Draft202012Validator(DECISION_SCHEMA, registry=REGISTRY)


def _config() -> EpisodeConfig:
    value = json.loads(
        (GOLDEN / "episode_config.json").read_text(encoding="utf-8")
    )
    return EpisodeConfig.from_mapping(cast(dict[str, object], value))


def _run_golden(episode: str) -> EpisodeResult:
    if episode == "main":
        pack_dir = GOLDEN / "pack"
        actions_path = GOLDEN / "actions.jsonl"
        run_id = "run_00000000000000a1"
    else:
        pack_dir = GOLDEN / "variant-liquidation" / "pack"
        actions_path = (
            GOLDEN / "variant-liquidation" / "actions.jsonl"
        )
        run_id = "run_00000000000000b1"
    return run_episode(
        pack_dir=pack_dir,
        agent=ScriptedFixtureAgent.from_path(actions_path),
        config=_config(),
        run_id=run_id,
        episode_id="ep_0123456789abcdef",
    )


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _turn_events(
    result: EpisodeResult, turn: int
) -> list[dict[str, object]]:
    return [
        event
        for event in result.events
        if event.get("turn") == turn
    ]


@pytest.mark.parametrize("episode", ["main", "variant-liquidation"])
def test_decisions_are_schema_valid_deterministic_event_views(
    episode: str,
) -> None:
    result = _run_golden(episode)
    records = generate_decision_records(
        result.events, result.ledger_primary, result.run_id
    )
    regenerated = generate_decision_records(
        result.events, result.ledger_primary, result.run_id
    )

    assert [canonical_bytes(record) for record in records] == [
        canonical_bytes(record) for record in regenerated
    ]
    assert len(records) == len(result.ledger_primary) - 1

    ledger_by_turn = {
        cast(int, row["turn"]): row
        for row in result.ledger_primary
        if row["turn"] is not None
    }
    for turn, record in enumerate(records):
        errors = list(VALIDATOR.iter_errors(record))
        assert not errors, [error.message for error in errors]

        events = _turn_events(result, turn)
        first_seq = cast(int, events[0]["seq"])
        last_seq = cast(int, events[-1]["seq"])
        assert record["event_seq_range"] == {
            "first_seq": first_seq,
            "last_seq": last_seq,
        }
        assert events[0]["type"] == "ObservationEmitted"
        assert [event["seq"] for event in events] == list(
            range(first_seq, last_seq + 1)
        )

        saw_event = next(
            event
            for event in events
            if event["type"] == "ObservationEmitted"
        )
        assert record["bar_index"] == saw_event["bar_index"]
        assert record["ts"] == saw_event["ts"]
        assert record["saw"] == saw_event["payload"]
        assert _as_mapping(record["said"])["attempts"] == [
            event["payload"]
            for event in events
            if event["type"] == "AgentResponded"
        ]
        parsed = [
            event["payload"]
            for event in events
            if event["type"] == "ActionParsed"
        ]
        rejected = [
            event["payload"]
            for event in events
            if event["type"] == "ActionRejected"
        ]
        meant = _as_mapping(record["meant"])
        if parsed:
            assert meant == {
                "status": "parsed",
                "action": parsed[0],
                "rejected": None,
            }
        else:
            assert meant == {
                "status": "rejected",
                "action": None,
                "rejected": rejected[0],
            }
        assert record["rules"] == [
            event["payload"]
            for event in events
            if event["type"] == "RiskCheck"
        ]
        happened = _as_mapping(record["happened"])
        assert happened["fills"] == [
            event["payload"]
            for event in events
            if event["type"] == "OrderFilled"
        ]
        assert happened["cancels"] == [
            event["payload"]
            for event in events
            if event["type"] == "OrderCancelled"
        ]
        liquidations = [
            event["payload"]
            for event in events
            if event["type"] == "LiquidationTriggered"
        ]
        post_kill_attempts = [
            event["payload"]
            for event in events
            if event["type"] == "PostKillSwitchAttempt"
        ]
        assert happened["liquidation"] == (
            liquidations[0] if liquidations else None
        )
        assert happened["post_kill_switch_attempt"] == (
            post_kill_attempts[0] if post_kill_attempts else None
        )
        cost = _as_mapping(record["cost_to_hold"])
        assert cost["funding"] == [
            event["payload"]
            for event in events
            if event["type"] == "FundingApplied"
        ]
        margins = [
            event["payload"]
            for event in events
            if event["type"] == "MarginUpdate"
        ]
        assert cost["margin_after"] == (
            margins[-1] if margins else None
        )

        account = _as_mapping(record["account_after"])
        ledger = ledger_by_turn[turn]
        for field in (
            "nav_micro",
            "cash_micro",
            "realized_pnl_micro",
            "d_nav_micro",
            "d_price_pnl_micro",
            "d_funding_micro",
            "d_fees_micro",
            "d_liq_penalty_micro",
        ):
            assert account[field] == ledger[field]


def test_rejected_timeout_and_retry_turns_are_total_records() -> None:
    result = _run_golden("main")
    records = generate_decision_records(
        result.events, result.ledger_primary, result.run_id
    )

    timeout = _as_mapping(records[1]["meant"])
    timeout_rejection = _as_mapping(timeout["rejected"])
    assert timeout["status"] == "rejected"
    assert timeout_rejection["reason"] == "timeout"
    assert _as_mapping(records[1]["said"])["attempts"] == []
    assert records[1]["rules"] == []

    malformed = _as_mapping(records[4]["meant"])
    malformed_rejection = _as_mapping(malformed["rejected"])
    assert malformed_rejection["reason"] == "invalid_json"
    assert malformed_rejection["attempts"] == 2
    attempts = cast(
        list[object], _as_mapping(records[4]["said"])["attempts"]
    )
    assert [
        _as_mapping(attempt)["attempt"] for attempt in attempts
    ] == [1, 2]


def test_closing_margin_and_liquidation_mirror_last_turn_events() -> None:
    result = _run_golden("variant-liquidation")
    records = generate_decision_records(
        result.events, result.ledger_primary, result.run_id
    )
    terminal = records[-1]
    events = _turn_events(result, cast(int, terminal["turn"]))

    liquidation = next(
        event["payload"]
        for event in events
        if event["type"] == "LiquidationTriggered"
    )
    margin_updates = [
        event["payload"]
        for event in events
        if event["type"] == "MarginUpdate"
    ]
    happened = _as_mapping(terminal["happened"])
    cost = _as_mapping(terminal["cost_to_hold"])
    assert happened["liquidation"] == liquidation
    assert cost["margin_after"] == margin_updates[-1]
    assert _as_mapping(cost["margin_after"])[
        "position_qty_base_1e8"
    ] == 0


def test_flat_all_holding_bar_has_null_margin_after() -> None:
    result = _run_golden("main")
    records = generate_decision_records(
        result.events, result.ledger_primary, result.run_id
    )
    assert _as_mapping(records[12]["cost_to_hold"])[
        "margin_after"
    ] is None


def test_projection_rejects_gapped_event_stream() -> None:
    result = _run_golden("main")
    broken = list(result.events)
    del broken[1]
    with pytest.raises(
        DecisionProjectionError, match="event seq is not contiguous"
    ):
        generate_decision_records(
            broken, result.ledger_primary, result.run_id
        )
