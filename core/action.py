# SPDX-License-Identifier: Apache-2.0
"""Deterministic IC-3 action parsing in the frozen V1-to-V8 order."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Iterable, Literal, Mapping, TypeGuard, cast

MAX_ACTION_BYTES: Final = 65_536
LEV_SCALE_1E4: Final = 10_000
MAX_TARGET_LEV_1E4: Final = 9_999_999
MAX_EXACT_JSON_INT: Final = 9_007_199_254_740_991
TARGET_PATTERN: Final = re.compile(
    r"^-?(0|[1-9][0-9]{0,2})(\.[0-9]{1,4})?$"
)
MARKET_PATTERN: Final = re.compile(r"^[A-Z0-9]{1,12}$")
KNOWN_TOP_LEVEL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "intent_kind",
        "target",
        "max_slippage_bps",
        "comment",
        "usage",
        "ext",
    }
)

ActionReason = Literal[
    "oversize",
    "invalid_json",
    "unknown_schema",
    "schema_invalid",
    "unknown_field",
    "unknown_market",
    "invalid_target_format",
    "float_target",
    "target_out_of_range",
    "invalid_slippage",
]


@dataclass(frozen=True, slots=True)
class _FloatToken:
    """A fractional JSON-number lexeme that never becomes a binary float."""

    raw: str


class _InvalidJson(ValueError):
    pass


def _capture_float(token: str) -> _FloatToken:
    return _FloatToken(token)


def _reject_constant(token: str) -> None:
    raise _InvalidJson(f"non-finite JSON token: {token}")


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJson(f"duplicate key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ParsedAction:
    """Canonical parsed action; this is the representation that is hashed."""

    intent_kind: Literal["leverage_target"]
    target_lev_1e4: Mapping[str, int]
    max_slippage_bps: int | None
    from_attempt: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "intent_kind": self.intent_kind,
            "target_lev_1e4": {
                alias: self.target_lev_1e4[alias]
                for alias in sorted(self.target_lev_1e4)
            },
            "max_slippage_bps": self.max_slippage_bps,
            "from_attempt": self.from_attempt,
        }


@dataclass(frozen=True, slots=True)
class ActionParseResult:
    """Total parse result: malformed agent bytes are data, never exceptions."""

    action: ParsedAction | None
    reason: ActionReason | None

    @property
    def accepted(self) -> bool:
        return self.action is not None


def _reject(reason: ActionReason) -> ActionParseResult:
    return ActionParseResult(action=None, reason=reason)


def _wire_bytes(body: object) -> bytes | None:
    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray):
        return bytes(body)
    if isinstance(body, memoryview):
        return body.tobytes()
    if isinstance(body, str):
        try:
            return body.encode("utf-8")
        except UnicodeEncodeError:
            return None
    return None


def _is_exact_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_extension(value: object) -> bool:
    """Iteratively validate JCS-safe extension values without recursion risk."""

    pending: list[object] = [value]
    while pending:
        current = pending.pop()
        if current is None or isinstance(current, (bool, str)):
            continue
        if _is_exact_int(current):
            if abs(current) > MAX_EXACT_JSON_INT:
                return False
            continue
        if isinstance(current, list):
            pending.extend(cast(list[object], current))
            continue
        if isinstance(current, dict):
            mapping = cast(dict[str, object], current)
            if not all(isinstance(key, str) for key in mapping):
                return False
            pending.extend(mapping.values())
            continue
        return False
    return True


def _v4_shape_is_valid(document: Mapping[str, object]) -> bool:
    intent = document.get("intent_kind", "leverage_target")
    if intent != "leverage_target":
        return False

    target = document.get("target")
    if not isinstance(target, dict):
        return False
    target_map = cast(dict[object, object], target)
    if not 1 <= len(target_map) <= 16:
        return False
    if any(
        not isinstance(alias, str) or MARKET_PATTERN.fullmatch(alias) is None
        for alias in target_map
    ):
        return False

    if "comment" in document:
        comment = document["comment"]
        if not isinstance(comment, str) or len(comment) > 2_000:
            return False

    if "usage" in document:
        usage = document["usage"]
        if not isinstance(usage, dict):
            return False
        usage_map = cast(dict[str, object], usage)
        if set(usage_map) - {"input_tokens", "output_tokens"}:
            return False
        if any(
            not _is_exact_int(value) or value < 0
            for value in usage_map.values()
        ):
            return False

    if "ext" in document:
        ext = document["ext"]
        if not isinstance(ext, dict) or not _valid_extension(ext):
            return False

    # Target value grammar and slippage deliberately remain unchecked here:
    # their dedicated V6/V7/V8 reason codes must win over schema_invalid.
    return True


def _parse_decimal_target(value: str) -> int | None:
    if TARGET_PATTERN.fullmatch(value) is None:
        return None
    negative = value.startswith("-")
    unsigned = value[1:] if negative else value
    if "." in unsigned:
        whole_text, fraction_text = unsigned.split(".", 1)
    else:
        whole_text, fraction_text = unsigned, ""
    fraction_units = int(fraction_text.ljust(4, "0")) if fraction_text else 0
    units = int(whole_text) * LEV_SCALE_1E4 + fraction_units
    return -units if negative else units


def parse_action(
    body: object,
    declared_markets: Iterable[str],
    *,
    from_attempt: int = 1,
) -> ActionParseResult:
    """Parse one IC-3 response without raising on malformed agent bytes.

    The implementation mirrors IC-3 V1 through V8 exactly.  Within a stage,
    market aliases are evaluated in lexical order, making the first failure
    stable even when the wire object used a different key order.
    """

    # V1: length and UTF-8.  The frozen table assigns both failures
    # ``oversize``; there is intentionally no separate invalid_utf8 code.
    raw = _wire_bytes(body)
    if raw is None or len(raw) > MAX_ACTION_BYTES:
        return _reject("oversize")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _reject("oversize")

    # V2: one complete JSON document, no duplicate keys/non-finite numbers.
    try:
        decoded = cast(
            object,
            json.loads(
                text,
                parse_float=_capture_float,
                parse_constant=_reject_constant,
                object_pairs_hook=_object_without_duplicates,
            ),
        )
    except (json.JSONDecodeError, _InvalidJson, RecursionError, ValueError):
        return _reject("invalid_json")

    # V3: exact major schema.
    if not isinstance(decoded, dict):
        return _reject("unknown_schema")
    document = cast(dict[str, object], decoded)
    if document.get("schema") != "action/v1":
        return _reject("unknown_schema")

    # V4: closed top-level shape and all non-V6/V8 schema constraints.
    if set(document) - KNOWN_TOP_LEVEL_FIELDS:
        return _reject("unknown_field")
    try:
        if not _v4_shape_is_valid(document):
            return _reject("schema_invalid")
    except (RecursionError, ValueError):
        return _reject("schema_invalid")

    target = cast(dict[str, object], document["target"])
    aliases = tuple(sorted(target))

    # V5: aliases must be declared by this pack.
    declared = frozenset(declared_markets)
    if any(alias not in declared for alias in aliases):
        return _reject("unknown_market")

    # V6: exact target wire grammar, with fractional-number lexemes kept out
    # of Python's binary-float domain.
    parsed_targets: dict[str, int] = {}
    for alias in aliases:
        value = target[alias]
        if isinstance(value, _FloatToken):
            return _reject("float_target")
        if _is_exact_int(value):
            parsed_targets[alias] = value * LEV_SCALE_1E4
            continue
        if isinstance(value, str):
            parsed = _parse_decimal_target(value)
            if parsed is None:
                return _reject("invalid_target_format")
            parsed_targets[alias] = parsed
            continue
        return _reject("invalid_target_format")

    # V7: structural sanity bound, distinct from risk-gate leverage caps.
    if any(abs(value) > MAX_TARGET_LEV_1E4 for value in parsed_targets.values()):
        return _reject("target_out_of_range")

    # V8: slippage is an exact integer basis-point count.
    slippage: int | None
    if "max_slippage_bps" in document:
        slippage_value = document["max_slippage_bps"]
        if (
            not _is_exact_int(slippage_value)
            or slippage_value < 0
            or slippage_value > 10_000
        ):
            return _reject("invalid_slippage")
        slippage = slippage_value
    else:
        slippage = None

    if (
        not isinstance(from_attempt, int)
        or isinstance(from_attempt, bool)
        or from_attempt < 1
    ):
        raise ValueError("from_attempt must be a positive integer")

    action = ParsedAction(
        intent_kind="leverage_target",
        target_lev_1e4=MappingProxyType(parsed_targets),
        max_slippage_bps=slippage,
        from_attempt=from_attempt,
    )
    return ActionParseResult(action=action, reason=None)
