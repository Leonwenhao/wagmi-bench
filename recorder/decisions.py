# SPDX-License-Identifier: Apache-2.0
"""Pure IC-5 decision-record materialization from canonical engine evidence.

``events.jsonl`` is the primary record.  This module deliberately performs no
I/O and invents no economic values: every decision field is copied from the
owning turn's events, except ``account_after``, which is copied from the
primary ledger row whose ``turn`` closes that decision's holding bar.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

JsonObject = dict[str, object]

_RUN_ID: Final = re.compile(r"run_[0-9a-f]{16}\Z")
_ACCOUNT_FIELDS: Final[tuple[str, ...]] = (
    "nav_micro",
    "cash_micro",
    "realized_pnl_micro",
    "d_nav_micro",
    "d_price_pnl_micro",
    "d_funding_micro",
    "d_fees_micro",
    "d_liq_penalty_micro",
)


class DecisionProjectionError(ValueError):
    """The event/ledger inputs cannot form an unambiguous IC-5 view."""


def _integer(value: object, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DecisionProjectionError(
            f"{context} must be an integer >= {minimum}, got {value!r}"
        )
    return value


def _payload(event: Mapping[str, object], context: str) -> JsonObject:
    value = event.get("payload")
    if not isinstance(value, Mapping):
        raise DecisionProjectionError(f"{context}.payload must be an object")
    if not all(isinstance(key, str) for key in value):
        raise DecisionProjectionError(
            f"{context}.payload contains a non-string key"
        )
    return dict(value)


def _typed_payloads(
    events: Sequence[Mapping[str, object]],
    event_type: str,
    *,
    turn: int,
) -> list[JsonObject]:
    return [
        _payload(event, f"turn {turn} {event_type}")
        for event in events
        if event.get("type") == event_type
    ]


def _one(
    payloads: Sequence[JsonObject],
    event_type: str,
    *,
    turn: int,
) -> JsonObject:
    if len(payloads) != 1:
        raise DecisionProjectionError(
            f"turn {turn} requires exactly one {event_type}, "
            f"found {len(payloads)}"
        )
    return payloads[0]


def _zero_or_one(
    payloads: Sequence[JsonObject],
    event_type: str,
    *,
    turn: int,
) -> JsonObject | None:
    if len(payloads) > 1:
        raise DecisionProjectionError(
            f"turn {turn} permits at most one {event_type}, "
            f"found {len(payloads)}"
        )
    return payloads[0] if payloads else None


def _account_after(row: Mapping[str, object], *, turn: int) -> JsonObject:
    account: JsonObject = {}
    for field in _ACCOUNT_FIELDS:
        account[field] = _integer(
            row.get(field),
            f"primary ledger turn {turn}.{field}",
            minimum=-(2**53 - 1),
        )
    return account


def generate_decision_records(
    events: Sequence[Mapping[str, object]],
    ledger_rows: Sequence[Mapping[str, object]],
    run_id: str,
) -> list[JsonObject]:
    """Generate one ``decision_record/v1`` object per processed turn.

    Inputs are the full IC-4 stream and the primary ``ledger_row/v1`` rows.
    The returned records are ordered by turn and contain no timestamps,
    account values, or outcomes inferred outside those two canonical sources.

    ``DecisionProjectionError`` is raised when a gap, duplicate, mismatched
    turn, missing holding row, or ambiguous singular event would make the
    materialized view non-reproducible.
    """

    if _RUN_ID.fullmatch(run_id) is None:
        raise DecisionProjectionError(f"invalid run_id {run_id!r}")

    events_by_turn: dict[int, list[Mapping[str, object]]] = {}
    for expected_seq, event in enumerate(events):
        seq = _integer(event.get("seq"), f"event[{expected_seq}].seq")
        if seq != expected_seq:
            raise DecisionProjectionError(
                f"event seq is not contiguous: expected {expected_seq}, got {seq}"
            )
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise DecisionProjectionError(
                f"event seq {seq}.type must be a string"
            )
        turn_value = event.get("turn")
        if turn_value is None:
            continue
        turn = _integer(turn_value, f"event seq {seq}.turn")
        events_by_turn.setdefault(turn, []).append(event)

    turns = sorted(events_by_turn)
    if turns != list(range(len(turns))):
        raise DecisionProjectionError(
            f"processed turns must be gap-free from zero, found {turns}"
        )

    ledger_by_turn: dict[int, Mapping[str, object]] = {}
    for row_number, row in enumerate(ledger_rows):
        if row.get("profile") != "primary":
            raise DecisionProjectionError(
                f"ledger row {row_number} is not profile 'primary'"
            )
        turn_value = row.get("turn")
        if turn_value is None:
            continue
        turn = _integer(turn_value, f"ledger row {row_number}.turn")
        if turn in ledger_by_turn:
            raise DecisionProjectionError(
                f"primary ledger has duplicate holding row for turn {turn}"
            )
        ledger_by_turn[turn] = row

    if set(ledger_by_turn) != set(turns):
        missing = sorted(set(turns) - set(ledger_by_turn))
        extra = sorted(set(ledger_by_turn) - set(turns))
        raise DecisionProjectionError(
            "primary ledger/event turn mismatch: "
            f"missing holding rows={missing}, extra holding rows={extra}"
        )

    records: list[JsonObject] = []
    for turn in turns:
        turn_events = events_by_turn[turn]
        seqs = [
            _integer(event.get("seq"), f"turn {turn} event seq")
            for event in turn_events
        ]
        expected_seqs = list(range(seqs[0], seqs[-1] + 1))
        if seqs != expected_seqs:
            raise DecisionProjectionError(
                f"turn {turn} event range is not contiguous: {seqs}"
            )
        if turn_events[0].get("type") != "ObservationEmitted":
            raise DecisionProjectionError(
                f"turn {turn} must start with ObservationEmitted"
            )

        observations = _typed_payloads(
            turn_events, "ObservationEmitted", turn=turn
        )
        saw = _one(observations, "ObservationEmitted", turn=turn)
        observation_event = turn_events[0]
        ts = _integer(
            observation_event.get("ts"),
            f"turn {turn} ObservationEmitted.ts",
        )
        bar_index = _integer(
            observation_event.get("bar_index"),
            f"turn {turn} ObservationEmitted.bar_index",
        )

        attempts = _typed_payloads(
            turn_events, "AgentResponded", turn=turn
        )
        attempt_numbers = [
            _integer(
                attempt.get("attempt"),
                f"turn {turn} AgentResponded.attempt",
                minimum=1,
            )
            for attempt in attempts
        ]
        if attempt_numbers != list(range(1, len(attempts) + 1)):
            raise DecisionProjectionError(
                f"turn {turn} response attempts are not ordered from 1: "
                f"{attempt_numbers}"
            )

        parsed = _typed_payloads(turn_events, "ActionParsed", turn=turn)
        rejected = _typed_payloads(turn_events, "ActionRejected", turn=turn)
        if (len(parsed), len(rejected)) == (1, 0):
            meant: JsonObject = {
                "status": "parsed",
                "action": parsed[0],
                "rejected": None,
            }
        elif (len(parsed), len(rejected)) == (0, 1):
            meant = {
                "status": "rejected",
                "action": None,
                "rejected": rejected[0],
            }
        else:
            raise DecisionProjectionError(
                f"turn {turn} requires ActionParsed XOR ActionRejected"
            )

        rules = _typed_payloads(turn_events, "RiskCheck", turn=turn)
        fills = _typed_payloads(turn_events, "OrderFilled", turn=turn)
        cancels = _typed_payloads(turn_events, "OrderCancelled", turn=turn)
        liquidation = _zero_or_one(
            _typed_payloads(
                turn_events, "LiquidationTriggered", turn=turn
            ),
            "LiquidationTriggered",
            turn=turn,
        )
        post_kill_switch_attempt = _zero_or_one(
            _typed_payloads(
                turn_events, "PostKillSwitchAttempt", turn=turn
            ),
            "PostKillSwitchAttempt",
            turn=turn,
        )
        funding = _typed_payloads(turn_events, "FundingApplied", turn=turn)
        margin_updates = _typed_payloads(
            turn_events, "MarginUpdate", turn=turn
        )

        records.append(
            {
                "schema": "decision_record/v1",
                "run_id": run_id,
                "turn": turn,
                "bar_index": bar_index,
                "ts": ts,
                "saw": saw,
                "said": {"attempts": attempts},
                "meant": meant,
                "rules": rules,
                "happened": {
                    "fills": fills,
                    "cancels": cancels,
                    "liquidation": liquidation,
                    "post_kill_switch_attempt": post_kill_switch_attempt,
                },
                "cost_to_hold": {
                    "funding": funding,
                    # IC-4 may emit several intra-turn snapshots after fills,
                    # funding, liquidation, or a kill-switch flatten.  The
                    # last one is the closing state.  A bar held flat
                    # throughout has no MarginUpdate and therefore null.
                    "margin_after": (
                        margin_updates[-1] if margin_updates else None
                    ),
                },
                "account_after": _account_after(
                    ledger_by_turn[turn], turn=turn
                ),
                "event_seq_range": {
                    "first_seq": seqs[0],
                    "last_seq": seqs[-1],
                },
            }
        )

    return records
