# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json

import pytest

from core.action import ActionParseResult, parse_action


def _body(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"x" * 65_537, "oversize"),
        (b"\xff", "oversize"),
        (b"{", "invalid_json"),
        (b'{"schema":"action/v1"} trailing', "invalid_json"),
        (b'{"schema":"action/v1","schema":"action/v1","target":{"BTC":"0"}}', "invalid_json"),
        (b"[]", "unknown_schema"),
        (_body({"target": {"BTC": "0"}}), "unknown_schema"),
        (_body({"schema": "action/v2", "target": {"BTC": "0"}}), "unknown_schema"),
        (_body({"schema": "action/v1"}), "schema_invalid"),
        (
            _body(
                {
                    "schema": "action/v1",
                    "target": {"BTC": "0"},
                    "surprise": True,
                }
            ),
            "unknown_field",
        ),
        (
            _body(
                {
                    "schema": "action/v1",
                    "intent_kind": "purchase_intent",
                    "target": {"BTC": "0"},
                }
            ),
            "schema_invalid",
        ),
        (
            _body({"schema": "action/v1", "target": {"bad-alias": "0"}}),
            "schema_invalid",
        ),
        (
            _body({"schema": "action/v1", "target": {"ETH": "not-decimal"}}),
            "unknown_market",
        ),
        (
            b'{"schema":"action/v1","target":{"BTC":1.5}}',
            "float_target",
        ),
        (
            _body({"schema": "action/v1", "target": {"BTC": "01"}}),
            "invalid_target_format",
        ),
        (
            _body({"schema": "action/v1", "target": {"BTC": True}}),
            "invalid_target_format",
        ),
        (
            _body({"schema": "action/v1", "target": {"BTC": 1000}}),
            "target_out_of_range",
        ),
        (
            _body(
                {
                    "schema": "action/v1",
                    "target": {"BTC": "0"},
                    "max_slippage_bps": -1,
                }
            ),
            "invalid_slippage",
        ),
        (
            _body(
                {
                    "schema": "action/v1",
                    "target": {"BTC": "0"},
                    "max_slippage_bps": None,
                }
            ),
            "invalid_slippage",
        ),
        (
            _body(
                {
                    "schema": "action/v1",
                    "target": {"BTC": "0"},
                    "comment": None,
                }
            ),
            "schema_invalid",
        ),
        (
            _body(
                {
                    "schema": "action/v1",
                    "target": {"BTC": "0"},
                    "usage": None,
                }
            ),
            "schema_invalid",
        ),
        (
            _body(
                {
                    "schema": "action/v1",
                    "target": {"BTC": "0"},
                    "ext": None,
                }
            ),
            "schema_invalid",
        ),
        (
            b'{"schema":"action/v1","target":{"BTC":"0"},"max_slippage_bps":1.5}',
            "invalid_slippage",
        ),
        (
            _body(
                {
                    "schema": "action/v1",
                    "target": {"BTC": "0"},
                    "max_slippage_bps": True,
                }
            ),
            "invalid_slippage",
        ),
    ],
)
def test_v1_to_v8_first_failure_is_stable(body: bytes, reason: str) -> None:
    result = parse_action(body, {"BTC"})
    assert not result.accepted
    assert result.reason == reason


def test_valid_decimal_strings_and_bare_integers_canonicalize_exactly() -> None:
    result = parse_action(
        _body(
            {
                "schema": "action/v1",
                "target": {"ETH": -2, "BTC": "-0.2500"},
                "max_slippage_bps": 30,
                "comment": "not copied into the canonical action",
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "ext": {"x_note": "kept only in raw", "x_count": 4},
            }
        ),
        {"BTC", "ETH"},
        from_attempt=2,
    )
    assert result.reason is None
    assert result.action is not None
    assert result.action.to_mapping() == {
        "intent_kind": "leverage_target",
        "target_lev_1e4": {"BTC": -2500, "ETH": -20000},
        "max_slippage_bps": 30,
        "from_attempt": 2,
    }


@pytest.mark.parametrize(
    ("wire", "canonical"),
    [
        ("0", 0),
        ("-0", 0),
        ("1", 10_000),
        ("1.5", 15_000),
        ("0.0001", 1),
        ("-999.9999", -9_999_999),
    ],
)
def test_decimal_grammar_uses_integer_arithmetic(
    wire: str,
    canonical: int,
) -> None:
    result = parse_action(
        _body({"schema": "action/v1", "target": {"BTC": wire}}),
        {"BTC"},
    )
    assert result.action is not None
    assert result.action.target_lev_1e4["BTC"] == canonical


def test_wire_absences_canonicalize_to_contract_defaults() -> None:
    result = parse_action(
        b'{"schema":"action/v1","target":{"BTC":0}}',
        {"BTC"},
    )
    assert result.action is not None
    assert result.action.intent_kind == "leverage_target"
    assert result.action.max_slippage_bps is None


def test_ten_thousand_malformed_bodies_never_raise() -> None:
    prefixes = (
        b"",
        b"{",
        b"[",
        b"\xff",
        b'{"schema":',
        b'{"schema":"action/v1","target":{"BTC":',
        b'{"schema":"action/v1","unknown":',
    )
    for index in range(10_000):
        body = prefixes[index % len(prefixes)] + str(index).encode()
        result = parse_action(body, {"BTC"})
        assert isinstance(result, ActionParseResult)
