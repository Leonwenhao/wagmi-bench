# SPDX-License-Identifier: Apache-2.0
"""Small dependency-free helpers shared by the HTTP reference agents."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Protocol, TypeGuard, cast

JsonObject = dict[str, object]

_MARKET_ALIAS = re.compile(r"[A-Z0-9]{1,12}\Z")
_TARGET = re.compile(r"-?(0|[1-9][0-9]{0,2})(\.[0-9]{1,4})?\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ACTION_FIELDS = frozenset(
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
_MAX_EXACT_JSON_INT = 9_007_199_254_740_991


class AgentContractError(ValueError):
    """An input or output violates the frozen IC-3/IC-6 wire contract."""


class DecisionPolicy(Protocol):
    """Policy hosted by :mod:`agents.server`."""

    def decide(self, request: Mapping[str, object]) -> JsonObject:
        """Return one bare ``action/v1`` document."""


def is_exact_int(value: object) -> TypeGuard[int]:
    """Return true for JSON integers while excluding booleans."""

    return isinstance(value, int) and not isinstance(value, bool)


def require_mapping(value: object, field: str) -> Mapping[str, object]:
    """Return a string-keyed mapping or raise a credential-free error."""

    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise AgentContractError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def validate_runner_request(
    request: Mapping[str, object],
) -> Mapping[str, object]:
    """Validate the closed IC-6 wrapper and return its observation only."""

    expected = {"schema", "attempt", "observation", "retry"}
    if set(request) != expected:
        raise AgentContractError("runner_request/v1 has unexpected fields")
    if request.get("schema") != "runner_request/v1":
        raise AgentContractError("runner request has an unknown schema")
    attempt = request.get("attempt")
    retry = request.get("retry")
    if not is_exact_int(attempt) or attempt not in (1, 2):
        raise AgentContractError("runner request attempt must be 1 or 2")
    if attempt == 1 and retry is not None:
        raise AgentContractError("attempt 1 requires retry=null")
    if attempt == 2:
        retry_map = require_mapping(retry, "retry")
        if set(retry_map) != {"reason", "detail", "prior_raw_sha256"}:
            raise AgentContractError("attempt 2 retry feedback has unexpected fields")
        if not isinstance(retry_map.get("reason"), str):
            raise AgentContractError("retry.reason must be text")
        if not isinstance(retry_map.get("detail"), str):
            raise AgentContractError("retry.detail must be text")
        prior_hash = retry_map.get("prior_raw_sha256")
        if not isinstance(prior_hash, str) or _SHA256.fullmatch(prior_hash) is None:
            raise AgentContractError("retry.prior_raw_sha256 is invalid")
    observation = require_mapping(request.get("observation"), "observation")
    if observation.get("schema") != "observation/v1":
        raise AgentContractError("observation has an unknown schema")
    require_mapping(observation.get("episode"), "observation.episode")
    markets = require_mapping(observation.get("markets"), "observation.markets")
    if not markets:
        raise AgentContractError("observation.markets must not be empty")
    for alias, market in markets.items():
        if _MARKET_ALIAS.fullmatch(alias) is None:
            raise AgentContractError("observation contains an invalid market alias")
        require_mapping(market, f"observation.markets.{alias}")
    require_mapping(observation.get("risk"), "observation.risk")
    return observation


def runner_retry_feedback(
    request: Mapping[str, object],
) -> JsonObject | None:
    """Return the frozen attempt-2 correction channel after full validation."""

    validate_runner_request(request)
    if request["attempt"] == 1:
        return None
    return dict(require_mapping(request["retry"], "retry"))


def market_aliases(observation: Mapping[str, object]) -> tuple[str, ...]:
    """Return the observation's market aliases in deterministic order."""

    markets = require_mapping(observation.get("markets"), "observation.markets")
    return tuple(sorted(markets))


def episode_turn(observation: Mapping[str, object]) -> int:
    """Read the non-negative decision turn."""

    episode = require_mapping(observation.get("episode"), "observation.episode")
    turn = episode.get("turn")
    if not is_exact_int(turn) or turn < 0:
        raise AgentContractError("observation.episode.turn must be non-negative")
    return turn


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """Encode a float-free wire object deterministically."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AgentContractError("response is not JSON serializable") from exc


def observation_json(observation: Mapping[str, object]) -> str:
    """Serialize exactly the observation object for an LLM user message."""

    return canonical_json_bytes(observation).decode("utf-8")


def _reject_float(token: str) -> None:
    raise AgentContractError(
        f"fractional JSON number is forbidden in action output: {token}"
    )


def _reject_constant(token: str) -> None:
    raise AgentContractError(f"non-finite JSON token is forbidden: {token}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise AgentContractError("action output has a duplicate JSON key")
        result[key] = value
    return result


def decode_action_object(raw: str) -> JsonObject:
    """Strictly decode one provider response as a JSON object."""

    try:
        value = cast(
            object,
            json.loads(
                raw,
                parse_float=_reject_float,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            ),
        )
    except json.JSONDecodeError as exc:
        raise AgentContractError("provider output is not one JSON document") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise AgentContractError("provider output must be a JSON object")
    return cast(JsonObject, value)


def _valid_extension(value: object) -> bool:
    """Validate the frozen action/v1 JCS-safe extension value grammar."""

    pending: list[object] = [value]
    while pending:
        current = pending.pop()
        if current is None or isinstance(current, (bool, str)):
            continue
        if is_exact_int(current):
            if abs(current) > _MAX_EXACT_JSON_INT:
                return False
            continue
        if isinstance(current, list):
            pending.extend(cast(list[object], current))
            continue
        if isinstance(current, dict):
            mapping = cast(dict[object, object], current)
            if not all(isinstance(key, str) for key in mapping):
                return False
            pending.extend(mapping.values())
            continue
        return False
    return True


def validate_action_document(
    action: Mapping[str, object],
    aliases: tuple[str, ...],
    *,
    require_all_markets: bool = False,
) -> JsonObject:
    """Validate the strict subset emitted by the reference agents."""

    if set(action) - _ACTION_FIELDS:
        raise AgentContractError("action contains an unknown top-level field")
    if action.get("schema") != "action/v1":
        raise AgentContractError("action schema must be action/v1")
    if action.get("intent_kind", "leverage_target") != "leverage_target":
        raise AgentContractError("action intent_kind is not supported in v1")
    target = require_mapping(action.get("target"), "action.target")
    if not 1 <= len(target) <= 16:
        raise AgentContractError("action.target must contain 1 through 16 markets")
    target_aliases = set(target)
    declared = set(aliases)
    if not target_aliases <= declared:
        raise AgentContractError("action.target contains an unknown market")
    if require_all_markets and target_aliases != declared:
        raise AgentContractError("baseline action must target every observed market")
    for alias, value in target.items():
        if _MARKET_ALIAS.fullmatch(alias) is None:
            raise AgentContractError("action.target contains an invalid alias")
        if is_exact_int(value):
            if not -999 <= value <= 999:
                raise AgentContractError("integer target is outside the v1 bound")
        elif not isinstance(value, str) or _TARGET.fullmatch(value) is None:
            raise AgentContractError(
                "target leverage must be an integer or decimal string"
            )
    slippage = action.get("max_slippage_bps")
    if slippage is not None and (
        not is_exact_int(slippage) or not 0 <= slippage <= 10_000
    ):
        raise AgentContractError("max_slippage_bps is outside 0 through 10000")
    comment = action.get("comment")
    if comment is not None and (
        not isinstance(comment, str) or len(comment) > 2_000
    ):
        raise AgentContractError("action.comment is invalid")
    usage = action.get("usage")
    if usage is not None:
        usage_map = require_mapping(usage, "action.usage")
        if set(usage_map) - {"input_tokens", "output_tokens"}:
            raise AgentContractError("action.usage contains an unknown field")
        if any(
            not is_exact_int(value) or value < 0
            for value in usage_map.values()
        ):
            raise AgentContractError("action.usage values must be non-negative")
    if "ext" in action:
        ext = action["ext"]
        if not isinstance(ext, dict) or not _valid_extension(ext):
            raise AgentContractError("action.ext is invalid")
    return dict(action)
