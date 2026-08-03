# SPDX-License-Identifier: Apache-2.0
"""Stored-byte verifier for IC-5 evidence bundles.

The public verdict is deliberately three-valued:

* ``COMPLETE`` -- a valid finalize-only seal, EpisodeEnd, schemas, files,
  blobs, chain heads/counts, and regenerated decision records all agree.
* ``TRUNCATED`` -- no seal exists, but every committed record/link pair is a
  valid contiguous prefix.  A single record-first unlinked tail is reported
  as uncommitted crash residue, never as part of the verified prefix.
* ``CORRUPT`` -- anything else, with the first bad stream/seq/path named.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast

from jsonschema import Draft202012Validator

from recorder.decisions import (
    DecisionProjectionError,
    generate_decision_records,
)
from recorder.writer import (
    _REGISTRY,
    _SCHEMAS,
    JsonObject,
    RecorderError,
    RecorderValidationError,
    validate_instance,
)
from spec.canonical import (
    ChainLink,
    ChainVerificationError,
    canonical_bytes,
    chain_genesis,
    run_config_sha256,
    seal_root,
    sha256_prefixed,
    verify_chain,
)

Verdict = Literal["COMPLETE", "TRUNCATED", "CORRUPT"]

_HASH: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OBS_NAME: Final = re.compile(r"([0-9]{4})\.json\Z")
_DECISION_NAME: Final = re.compile(r"([0-9]{4})\.json\Z")
_RAW_NAME: Final = re.compile(r"([0-9]{4})-a([12])\.txt\Z")
_SEALED_FILES: Final[frozenset[str]] = frozenset(
    {
        "manifest.json",
        "agent_manifest.json",
        "ledger.jsonl",
        "ledger_stress_2x.jsonl",
        "metrics.json",
        "chain.jsonl",
    }
)
_SEALED_TOP_LEVEL: Final[frozenset[str]] = frozenset(
    {
        "manifest.json",
        "agent_manifest.json",
        "observations",
        "raw",
        "events.jsonl",
        "chain.jsonl",
        "decisions",
        "ledger.jsonl",
        "ledger_stress_2x.jsonl",
        "metrics.json",
        "chain.json",
    }
)
_CHAIN_LINK_SCHEMA: Final = cast(
    Mapping[str, object],
    cast(Mapping[str, object], _SCHEMAS["chain"]["$defs"])["chain_link"],
)
_CHAIN_LINK_VALIDATOR: Final = Draft202012Validator(
    _CHAIN_LINK_SCHEMA,
    registry=_REGISTRY,
)


@dataclass(frozen=True, slots=True)
class TurnInventory:
    """Artifacts present for one turn in a complete or crashed bundle."""

    turn: int
    observation: bool
    raw_attempts: int
    decision: bool
    first_event_seq: int | None
    last_event_seq: int | None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Machine- and reader-facing result of :func:`verify_bundle`."""

    verdict: Verdict
    message: str
    stream: str | None
    seq: int | None
    path: str | None
    root: str | None
    last_good: Mapping[str, int]
    inventory: Mapping[str, int]
    turns: tuple[TurnInventory, ...]

    @property
    def is_complete(self) -> bool:
        return self.verdict == "COMPLETE"


class _VerificationFailure(Exception):
    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        stream: str | None = None,
        seq: int | None = None,
    ) -> None:
        self.message = message
        self.path = path
        self.stream = stream
        self.seq = seq
        super().__init__(message)


def _reject_float(token: str) -> None:
    raise ValueError(f"fractional/exponent JSON number is forbidden: {token}")


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {token}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, *, path: str) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _VerificationFailure(
            f"{path}: invalid UTF-8 at byte {exc.start}",
            path=path,
        ) from exc
    try:
        return cast(
            object,
            json.loads(
                text,
                parse_float=_reject_float,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise _VerificationFailure(
            f"{path}: invalid JSON: {exc}",
            path=path,
        ) from exc


def _safe_bundle_path(root: Path, relative: str) -> Path:
    """Resolve a bundle-relative path without following any symlink component."""

    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise _VerificationFailure(
            f"{relative}: unsafe bundle-relative path",
            path=relative,
        )
    candidate = root
    for part in pure.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise _VerificationFailure(
                f"{relative}: symlink path components are forbidden",
                path=relative,
            )
    try:
        if not candidate.resolve(strict=False).is_relative_to(root.resolve()):
            raise _VerificationFailure(
                f"{relative}: path escapes bundle root",
                path=relative,
            )
    except OSError as exc:
        raise _VerificationFailure(
            f"{relative}: cannot resolve bundle path",
            path=relative,
        ) from exc
    return candidate


def _verify_layout(root: Path, *, sealed: bool) -> None:
    """Reject symlinked stores and undeclared material in a complete bundle."""

    for directory in ("observations", "raw", "decisions"):
        path = _safe_bundle_path(root, directory)
        if not path.is_dir():
            raise _VerificationFailure(
                f"{directory}/: missing or not a real directory",
                path=directory,
            )
    if not sealed:
        return
    allowed = set(_SEALED_TOP_LEVEL)
    if (root / "redaction.json").exists():
        allowed.add("redaction.json")
    actual = {path.name for path in root.iterdir()}
    if actual != allowed:
        difference = sorted(actual ^ allowed)
        offending = difference[0]
        raise _VerificationFailure(
            "sealed top-level layout mismatch: "
            f"missing={sorted(allowed - actual)}, "
            f"extra={sorted(actual - allowed)}",
            path=offending,
        )


def _object(value: object, *, path: str) -> JsonObject:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise _VerificationFailure(f"{path}: expected an object", path=path)
    return cast(JsonObject, value)


def _read_document(
    root: Path,
    relative: str,
    *,
    schema: str | None = None,
    newline: Literal["forbid", "require", "optional"] = "forbid",
) -> tuple[JsonObject, bytes]:
    path = _safe_bundle_path(root, relative)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _VerificationFailure(
            f"{relative}: cannot read required file",
            path=relative,
        ) from exc
    if newline == "require":
        if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
            raise _VerificationFailure(
                f"{relative}: expected exactly one trailing newline",
                path=relative,
            )
        logical = raw[:-1]
    elif newline == "optional":
        logical = raw[:-1] if raw.endswith(b"\n") else raw
        if logical.endswith(b"\n"):
            raise _VerificationFailure(
                f"{relative}: more than one trailing newline",
                path=relative,
            )
    else:
        if raw.endswith(b"\n"):
            raise _VerificationFailure(
                f"{relative}: unexpected trailing newline",
                path=relative,
            )
        logical = raw
    value = _object(_decode_json(logical, path=relative), path=relative)
    try:
        expected = canonical_bytes(value)
    except ValueError as exc:
        raise _VerificationFailure(
            f"{relative}: cannot canonicalize: {exc}",
            path=relative,
        ) from exc
    if logical != expected:
        raise _VerificationFailure(
            f"{relative}: stored bytes are not canonical JCS",
            path=relative,
        )
    if schema is not None:
        try:
            validate_instance(schema, value, context=relative)
        except RecorderValidationError as exc:
            raise _VerificationFailure(str(exc), path=relative) from exc
    return value, raw


@dataclass(frozen=True, slots=True)
class _JsonlRead:
    objects: tuple[JsonObject, ...]
    records: tuple[bytes, ...]
    partial_tail: bytes
    raw: bytes


def _read_jsonl(
    root: Path,
    relative: str,
    *,
    schema: str | None = None,
    sealed: bool,
    stream: str | None = None,
) -> _JsonlRead:
    path = _safe_bundle_path(root, relative)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _VerificationFailure(
            f"{relative}: cannot read required file",
            path=relative,
            stream=stream,
        ) from exc
    pieces = raw.split(b"\n")
    partial = pieces[-1]
    complete = pieces[:-1]
    if sealed and partial:
        raise _VerificationFailure(
            f"{relative}: sealed JSONL has an unterminated final record",
            path=relative,
            stream=stream,
            seq=len(complete),
        )
    objects: list[JsonObject] = []
    records: list[bytes] = []
    for index, line in enumerate(complete):
        if not line:
            raise _VerificationFailure(
                f"{relative}: blank record at line {index + 1}",
                path=relative,
                stream=stream,
                seq=index,
            )
        value = _object(
            _decode_json(line, path=f"{relative}:{index + 1}"),
            path=f"{relative}:{index + 1}",
        )
        try:
            expected = canonical_bytes(value)
        except ValueError as exc:
            raise _VerificationFailure(
                f"{relative}: record {index} cannot canonicalize: {exc}",
                path=relative,
                stream=stream,
                seq=index,
            ) from exc
        if line != expected:
            raise _VerificationFailure(
                f"{relative}: record {index} is not canonical JCS",
                path=relative,
                stream=stream,
                seq=index,
            )
        if schema is not None:
            try:
                validate_instance(
                    schema,
                    value,
                    context=f"{relative} record {index}",
                )
            except RecorderValidationError as exc:
                raise _VerificationFailure(
                    str(exc),
                    path=relative,
                    stream=stream,
                    seq=index,
                ) from exc
        objects.append(value)
        records.append(line)
    return _JsonlRead(
        objects=tuple(objects),
        records=tuple(records),
        partial_tail=partial,
        raw=raw,
    )


def _validate_chain_link(value: JsonObject, *, line: int) -> ChainLink:
    errors = sorted(
        _CHAIN_LINK_VALIDATOR.iter_errors(value),
        key=lambda error: tuple(
            str(item) for item in error.absolute_path
        ),
    )
    if errors:
        raise _VerificationFailure(
            f"chain.jsonl link {line}: {errors[0].message}",
            path="chain.jsonl",
            seq=line,
        )
    return cast(ChainLink, value)


def _read_decisions(
    root: Path,
    *,
    sealed: bool,
) -> tuple[tuple[JsonObject, ...], tuple[bytes, ...]]:
    directory = _safe_bundle_path(root, "decisions")
    candidates = sorted(
        path
        for path in directory.iterdir()
        if sealed or not path.name.startswith(".")
    )
    objects: list[JsonObject] = []
    records: list[bytes] = []
    for expected, path in enumerate(candidates):
        match = _DECISION_NAME.fullmatch(path.name)
        relative = f"decisions/{path.name}"
        if match is None:
            raise _VerificationFailure(
                f"{relative}: unexpected decision artifact",
                path=relative,
                stream="decisions",
                seq=expected,
            )
        number = int(match.group(1))
        if number != expected:
            raise _VerificationFailure(
                f"{relative}: expected decisions/{expected:04d}.json",
                path=relative,
                stream="decisions",
                seq=expected,
            )
        value, raw = _read_document(
            root,
            relative,
            schema="decision_record",
        )
        if value.get("turn") != expected:
            raise _VerificationFailure(
                f"{relative}: decision.turn is {value.get('turn')!r}",
                path=relative,
                stream="decisions",
                seq=expected,
            )
        objects.append(value)
        records.append(raw)
    return tuple(objects), tuple(records)


def _stream_links(
    chain_objects: Sequence[JsonObject],
) -> dict[str, list[ChainLink]]:
    links: dict[str, list[ChainLink]] = {
        "events": [],
        "decisions": [],
    }
    for line, value in enumerate(chain_objects):
        link = _validate_chain_link(value, line=line)
        stream_value = link["stream"]
        if stream_value not in links:
            raise _VerificationFailure(
                f"chain.jsonl link {line}: unknown stream {stream_value!r}",
                path="chain.jsonl",
                seq=line,
            )
        links[stream_value].append(link)
    return links


def _verify_stream(
    stream: Literal["events", "decisions"],
    *,
    genesis: str,
    records: Sequence[bytes],
    links: Sequence[ChainLink],
    sealed_head: Mapping[str, object] | None,
) -> None:
    if sealed_head is None:
        if len(records) < len(links):
            raise _VerificationFailure(
                f"{stream}: {len(links)} links but only "
                f"{len(records)} complete records",
                path=(
                    "events.jsonl"
                    if stream == "events"
                    else f"decisions/{len(records):04d}.json"
                ),
                stream=stream,
                seq=len(records),
            )
        if len(records) - len(links) > 1:
            raise _VerificationFailure(
                f"{stream}: more than one unlinked trailing record",
                path=(
                    "events.jsonl"
                    if stream == "events"
                    else f"decisions/{len(links):04d}.json"
                ),
                stream=stream,
                seq=len(links),
            )
        committed_records = records[: len(links)]
        expected_head = None
        expected_count = None
    else:
        committed_records = records
        sealed_genesis = sealed_head.get("genesis")
        if sealed_genesis != genesis:
            raise _VerificationFailure(
                f"chain.json streams.{stream}.genesis does not match "
                "the recomputed bundle identity",
                path="chain.json",
                stream=stream,
            )
        expected_head_value = sealed_head.get("head")
        expected_count_value = sealed_head.get("count")
        if not isinstance(expected_head_value, str) or not isinstance(
            expected_count_value, int
        ) or isinstance(expected_count_value, bool):
            raise _VerificationFailure(
                f"chain.json streams.{stream}: invalid head/count",
                path="chain.json",
                stream=stream,
            )
        expected_head = expected_head_value
        expected_count = expected_count_value
    try:
        verify_chain(
            stream,
            genesis,
            committed_records,
            links,
            expected_head=expected_head,
            expected_count=expected_count,
        )
    except ChainVerificationError as exc:
        path = (
            "events.jsonl"
            if stream == "events"
            else f"decisions/{exc.seq:04d}.json"
        )
        raise _VerificationFailure(
            str(exc),
            path=path,
            stream=stream,
            seq=exc.seq,
        ) from exc


def _ledger_rows(
    root: Path,
    relative: str,
    *,
    profile: str,
    sealed: bool,
) -> _JsonlRead | None:
    if not (root / relative).exists():
        if sealed:
            raise _VerificationFailure(
                f"{relative}: missing required ledger",
                path=relative,
            )
        return None
    rows = _read_jsonl(
        root,
        relative,
        schema="ledger_row",
        sealed=sealed,
    )
    previous_nav: int | None = None
    for index, row in enumerate(rows.objects):
        if row.get("profile") != profile:
            raise _VerificationFailure(
                f"{relative}: row {index} has profile "
                f"{row.get('profile')!r}",
                path=relative,
                seq=index,
            )
        fields = (
            "d_nav_micro",
            "d_price_pnl_micro",
            "d_funding_micro",
            "d_fees_micro",
            "d_liq_penalty_micro",
            "nav_micro",
        )
        values: dict[str, int] = {}
        for field in fields:
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise _VerificationFailure(
                    f"{relative}: row {index}.{field} is not an integer",
                    path=relative,
                    seq=index,
                )
            values[field] = value
        if values["d_nav_micro"] != (
            values["d_price_pnl_micro"]
            + values["d_funding_micro"]
            + values["d_fees_micro"]
            + values["d_liq_penalty_micro"]
        ):
            raise _VerificationFailure(
                f"{relative}: row {index} violates MATH-2",
                path=relative,
                seq=index,
            )
        if (
            previous_nav is not None
            and values["nav_micro"] - previous_nav
            != values["d_nav_micro"]
        ):
            raise _VerificationFailure(
                f"{relative}: row {index} NAV delta disagrees with prior row",
                path=relative,
                seq=index,
            )
        previous_nav = values["nav_micro"]
    return rows


def _referenced_blobs(
    events: Sequence[JsonObject],
) -> tuple[
    dict[str, tuple[str, int]],
    dict[str, tuple[str, int]],
]:
    observations: dict[str, tuple[str, int]] = {}
    raw: dict[str, tuple[str, int]] = {}
    for event in events:
        event_type = event.get("type")
        payload_value = event.get("payload")
        if not isinstance(payload_value, dict):
            continue
        payload = cast(JsonObject, payload_value)
        turn_value = event.get("turn")
        if event_type == "ObservationEmitted":
            if not isinstance(turn_value, int) or isinstance(turn_value, bool):
                raise _VerificationFailure(
                    "ObservationEmitted has no integer turn",
                    path="events.jsonl",
                    stream="events",
                    seq=cast(int, event.get("seq", 0)),
                )
            relative = payload.get("observation_ref")
            digest = payload.get("observation_sha256")
            expected = f"observations/{turn_value:04d}.json"
            if relative != expected or not isinstance(digest, str):
                raise _VerificationFailure(
                    "ObservationEmitted reference/hash is invalid",
                    path="events.jsonl",
                    stream="events",
                    seq=cast(int, event.get("seq", 0)),
                )
            if relative in observations:
                raise _VerificationFailure(
                    f"duplicate observation reference {relative}",
                    path="events.jsonl",
                    stream="events",
                    seq=cast(int, event.get("seq", 0)),
                )
            observations[relative] = (digest, turn_value)
        elif event_type == "AgentResponded":
            relative = payload.get("raw_ref")
            digest = payload.get("raw_sha256")
            byte_count = payload.get("raw_bytes")
            if (
                not isinstance(relative, str)
                or _RAW_NAME.fullmatch(Path(relative).name) is None
                or not relative.startswith("raw/")
                or not isinstance(digest, str)
                or not isinstance(byte_count, int)
                or isinstance(byte_count, bool)
            ):
                raise _VerificationFailure(
                    "AgentResponded raw reference/hash/bytes is invalid",
                    path="events.jsonl",
                    stream="events",
                    seq=cast(int, event.get("seq", 0)),
                )
            if relative in raw:
                raise _VerificationFailure(
                    f"duplicate raw reference {relative}",
                    path="events.jsonl",
                    stream="events",
                    seq=cast(int, event.get("seq", 0)),
                )
            raw[relative] = (digest, byte_count)
    return observations, raw


def _redactions(
    root: Path,
    *,
    seal: JsonObject | None,
    run_id: str,
    raw_refs: Mapping[str, tuple[str, int]],
) -> dict[str, tuple[str, int]]:
    path = root / "redaction.json"
    if not path.exists():
        return {}
    if seal is None:
        raise _VerificationFailure(
            "redaction.json is only valid for a sealed share bundle",
            path="redaction.json",
        )
    redaction, _ = _read_document(
        root,
        "redaction.json",
        schema="redaction",
        newline="optional",
    )
    if redaction.get("run_id") != run_id:
        raise _VerificationFailure(
            "redaction.json run_id does not match manifest",
            path="redaction.json",
        )
    if redaction.get("parent_root") != seal.get("root"):
        raise _VerificationFailure(
            "redaction.json parent_root does not match chain root",
            path="redaction.json",
        )
    removals_value = redaction.get("removals")
    if not isinstance(removals_value, list):
        raise _VerificationFailure(
            "redaction.json removals is not an array",
            path="redaction.json",
        )
    disclosed: dict[str, tuple[str, int]] = {}
    for index, item in enumerate(removals_value):
        if not isinstance(item, dict):
            raise _VerificationFailure(
                f"redaction.json removal {index} is not an object",
                path="redaction.json",
                seq=index,
            )
        relative = item.get("path")
        digest = item.get("sha256")
        byte_count = item.get("bytes")
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
        ):
            raise _VerificationFailure(
                f"redaction.json removal {index} has invalid fields",
                path="redaction.json",
                seq=index,
            )
        if relative in disclosed:
            raise _VerificationFailure(
                f"redaction.json duplicates {relative}",
                path="redaction.json",
                seq=index,
            )
        committed = raw_refs.get(relative)
        if committed != (digest, byte_count):
            raise _VerificationFailure(
                f"redaction.json removal {relative} disagrees with event",
                path="redaction.json",
                seq=index,
            )
        if (root / relative).exists():
            raise _VerificationFailure(
                f"redaction.json lists present blob {relative}",
                path=relative,
                seq=index,
            )
        disclosed[relative] = (digest, byte_count)
    return disclosed


def _verify_blobs(
    root: Path,
    *,
    events: Sequence[JsonObject],
    seal: JsonObject | None,
    run_id: str,
) -> tuple[int, int, int]:
    observation_refs, raw_refs = _referenced_blobs(events)
    disclosed = _redactions(
        root,
        seal=seal,
        run_id=run_id,
        raw_refs=raw_refs,
    )
    for relative, (digest, _) in observation_refs.items():
        document, raw = _read_document(
            root,
            relative,
            schema="observation",
        )
        del document
        if sha256_prefixed(raw) != digest:
            raise _VerificationFailure(
                f"{relative}: observation hash mismatch",
                path=relative,
            )
    for relative, (digest, byte_count) in raw_refs.items():
        path = _safe_bundle_path(root, relative)
        if not path.exists():
            if relative in disclosed:
                continue
            raise _VerificationFailure(
                f"{relative}: referenced raw blob is missing and undisclosed",
                path=relative,
            )
        if path.is_symlink() or not path.is_file():
            raise _VerificationFailure(
                f"{relative}: raw blob is not a regular file",
                path=relative,
            )
        stored = path.read_bytes()
        if len(stored) != byte_count:
            raise _VerificationFailure(
                f"{relative}: raw byte count mismatch",
                path=relative,
            )
        if sha256_prefixed(stored) != digest:
            raise _VerificationFailure(
                f"{relative}: raw hash mismatch",
                path=relative,
            )

    observations_dir = _safe_bundle_path(root, "observations")
    raw_dir = _safe_bundle_path(root, "raw")
    include_hidden = seal is not None
    actual_observations = {
        f"observations/{path.name}"
        for path in observations_dir.iterdir()
        if include_hidden or not path.name.startswith(".")
    }
    actual_raw = {
        f"raw/{path.name}"
        for path in raw_dir.iterdir()
        if include_hidden or not path.name.startswith(".")
    }
    expected_observations = set(observation_refs)
    observation_mismatch = (
        actual_observations != expected_observations
        if seal is not None
        else not expected_observations <= actual_observations
    )
    if observation_mismatch:
        difference = sorted(actual_observations ^ expected_observations)
        relative = difference[0]
        raise _VerificationFailure(
            f"{relative}: observation store/reference mismatch",
            path=relative,
        )
    expected_present_raw = set(raw_refs) - set(disclosed)
    raw_mismatch = (
        actual_raw != expected_present_raw
        if seal is not None
        else not expected_present_raw <= actual_raw
    )
    if raw_mismatch:
        difference = sorted(actual_raw ^ expected_present_raw)
        relative = difference[0]
        raise _VerificationFailure(
            f"{relative}: raw store/reference mismatch",
            path=relative,
        )
    return len(observation_refs), len(raw_refs), len(disclosed)


def _turn_inventory(
    root: Path,
    events: Sequence[JsonObject],
    decisions: Sequence[JsonObject],
) -> tuple[TurnInventory, ...]:
    observations: set[int] = set()
    if (root / "observations").is_dir():
        for path in (root / "observations").iterdir():
            match = _OBS_NAME.fullmatch(path.name)
            if match is not None:
                observations.add(int(match.group(1)))
    raw_attempts: dict[int, int] = {}
    if (root / "raw").is_dir():
        for path in (root / "raw").iterdir():
            match = _RAW_NAME.fullmatch(path.name)
            if match is not None:
                turn = int(match.group(1))
                raw_attempts[turn] = raw_attempts.get(turn, 0) + 1
    event_ranges: dict[int, tuple[int, int]] = {}
    for event in events:
        turn_value = event.get("turn")
        seq_value = event.get("seq")
        if (
            isinstance(turn_value, int)
            and not isinstance(turn_value, bool)
            and isinstance(seq_value, int)
            and not isinstance(seq_value, bool)
        ):
            current = event_ranges.get(turn_value)
            if current is None:
                event_ranges[turn_value] = (seq_value, seq_value)
            else:
                event_ranges[turn_value] = (current[0], seq_value)
    decision_turns = {
        cast(int, decision["turn"])
        for decision in decisions
        if isinstance(decision.get("turn"), int)
    }
    turns = sorted(
        observations
        | set(raw_attempts)
        | set(event_ranges)
        | decision_turns
    )
    return tuple(
        TurnInventory(
            turn=turn,
            observation=turn in observations,
            raw_attempts=raw_attempts.get(turn, 0),
            decision=turn in decision_turns,
            first_event_seq=(
                event_ranges[turn][0] if turn in event_ranges else None
            ),
            last_event_seq=(
                event_ranges[turn][1] if turn in event_ranges else None
            ),
        )
        for turn in turns
    )


def _sealed_stream_head(
    seal: JsonObject,
    stream: str,
) -> Mapping[str, object]:
    streams = seal.get("streams")
    if not isinstance(streams, dict):
        raise _VerificationFailure(
            "chain.json streams is not an object",
            path="chain.json",
        )
    head = streams.get(stream)
    if not isinstance(head, dict):
        raise _VerificationFailure(
            f"chain.json streams.{stream} is not an object",
            path="chain.json",
            stream=stream,
        )
    return cast(Mapping[str, object], head)


def _verify_seal_files(root: Path, seal: JsonObject) -> None:
    files_value = seal.get("files")
    if not isinstance(files_value, dict):
        raise _VerificationFailure(
            "chain.json files is not an object",
            path="chain.json",
        )
    files = cast(dict[str, object], files_value)
    if set(files) != _SEALED_FILES:
        missing = sorted(_SEALED_FILES - set(files))
        extra = sorted(set(files) - _SEALED_FILES)
        raise _VerificationFailure(
            "chain.json files coverage mismatch: "
            f"missing={missing}, extra={extra}",
            path="chain.json",
        )
    # chain.jsonl is verified record-by-record before this whole-file check,
    # so a changed link localizes to its stream/seq instead of only this file.
    for relative in sorted(files):
        try:
            raw = (root / relative).read_bytes()
        except OSError as exc:
            raise _VerificationFailure(
                f"{relative}: cannot read sealed file",
                path=relative,
            ) from exc
        expected = files[relative]
        actual = sha256_prefixed(raw)
        if expected != actual:
            raise _VerificationFailure(
                f"{relative}: sealed whole-file hash mismatch",
                path=relative,
            )


def _verify_decision_projection(
    *,
    events: Sequence[JsonObject],
    decisions: Sequence[JsonObject],
    decision_bytes: Sequence[bytes],
    ledger: _JsonlRead,
    run_id: str,
) -> None:
    try:
        regenerated = generate_decision_records(
            events,
            ledger.objects,
            run_id,
        )
    except DecisionProjectionError as exc:
        raise _VerificationFailure(
            f"decision regeneration failed: {exc}",
            path="decisions",
            stream="decisions",
        ) from exc
    if len(regenerated) != len(decisions):
        seq = min(len(regenerated), len(decisions))
        raise _VerificationFailure(
            "decision record count differs from regenerated event view",
            path=f"decisions/{seq:04d}.json",
            stream="decisions",
            seq=seq,
        )
    for seq, (expected, stored) in enumerate(
        zip(regenerated, decision_bytes, strict=True)
    ):
        if canonical_bytes(expected) != stored:
            raise _VerificationFailure(
                "stored decision differs from canonical event-derived view",
                path=f"decisions/{seq:04d}.json",
                stream="decisions",
                seq=seq,
            )


def verify_bundle(bundle_dir: str | Path) -> VerificationResult:
    """Verify a bundle from stored bytes and return a three-valued verdict."""

    root = Path(bundle_dir)
    empty_inventory: dict[str, int] = {}
    empty_last_good = {"events": -1, "decisions": -1}
    if not root.is_dir() or root.is_symlink():
        return VerificationResult(
            verdict="CORRUPT",
            message=f"{root}: bundle directory is missing or invalid",
            stream=None,
            seq=None,
            path=str(root),
            root=None,
            last_good=empty_last_good,
            inventory=empty_inventory,
            turns=(),
        )

    events_for_inventory: tuple[JsonObject, ...] = ()
    decisions_for_inventory: tuple[JsonObject, ...] = ()
    links_for_inventory: dict[str, list[ChainLink]] = {
        "events": [],
        "decisions": [],
    }
    root_value: str | None = None
    try:
        manifest, manifest_bytes = _read_document(
            root,
            "manifest.json",
            schema="bundle_manifest",
        )
        agent_manifest, agent_bytes = _read_document(
            root,
            "agent_manifest.json",
            schema="agent_manifest",
        )
        del manifest_bytes, agent_manifest
        agent_hash = sha256_prefixed(agent_bytes)
        if manifest.get("agent_manifest_sha256") != agent_hash:
            raise _VerificationFailure(
                "agent_manifest.json hash does not match manifest",
                path="agent_manifest.json",
            )
        run_id_value = manifest.get("run_id")
        if not isinstance(run_id_value, str):
            raise _VerificationFailure(
                "manifest.run_id is not a string",
                path="manifest.json",
            )
        run_id = run_id_value
        pack_value = manifest.get("pack")
        run_config_value = manifest.get("run_config")
        if not isinstance(pack_value, dict) or not isinstance(
            run_config_value, dict
        ):
            raise _VerificationFailure(
                "manifest pack/run_config is invalid",
                path="manifest.json",
            )
        pack_hash = pack_value.get("content_hash")
        if not isinstance(pack_hash, str):
            raise _VerificationFailure(
                "manifest.pack.content_hash is invalid",
                path="manifest.json",
            )
        config_hash = run_config_sha256(
            cast(Mapping[str, object], run_config_value)
        )
        event_genesis = chain_genesis(
            "events",
            run_id=run_id,
            pack_content_hash=pack_hash,
            agent_manifest_sha256=agent_hash,
            run_config_sha256=config_hash,
        )
        decision_genesis = chain_genesis(
            "decisions",
            run_id=run_id,
            pack_content_hash=pack_hash,
            agent_manifest_sha256=agent_hash,
            run_config_sha256=config_hash,
        )

        seal_path = root / "chain.json"
        if seal_path.is_symlink():
            raise _VerificationFailure(
                "chain.json: symlinks are forbidden",
                path="chain.json",
            )
        sealed = seal_path.exists()
        seal: JsonObject | None = None
        if sealed:
            seal, _ = _read_document(
                root,
                "chain.json",
                schema="chain",
            )
            if seal.get("run_id") != run_id:
                raise _VerificationFailure(
                    "chain.json run_id does not match manifest",
                    path="chain.json",
                )
            if seal.get("complete") is not True:
                raise _VerificationFailure(
                    "chain.json complete is not true",
                    path="chain.json",
                )
            expected_root = seal_root(seal)
            if seal.get("root") != expected_root:
                raise _VerificationFailure(
                    "chain.json root mismatch",
                    path="chain.json",
                )
            root_value = expected_root
        _verify_layout(root, sealed=sealed)

        events_read = _read_jsonl(
            root,
            "events.jsonl",
            schema="event",
            sealed=sealed,
            stream="events",
        )
        events_for_inventory = events_read.objects
        for index, event in enumerate(events_read.objects):
            if event.get("seq") != index:
                raise _VerificationFailure(
                    f"events.jsonl: event seq is {event.get('seq')!r}, "
                    f"expected {index}",
                    path="events.jsonl",
                    stream="events",
                    seq=index,
                )
        decisions, decision_records = _read_decisions(root, sealed=sealed)
        decisions_for_inventory = decisions
        chain_read = _read_jsonl(
            root,
            "chain.jsonl",
            sealed=sealed,
        )
        links = _stream_links(chain_read.objects)
        links_for_inventory = links
        event_head = (
            _sealed_stream_head(seal, "events")
            if seal is not None
            else None
        )
        decision_head = (
            _sealed_stream_head(seal, "decisions")
            if seal is not None
            else None
        )
        _verify_stream(
            "events",
            genesis=event_genesis,
            records=events_read.records,
            links=links["events"],
            sealed_head=event_head,
        )
        _verify_stream(
            "decisions",
            genesis=decision_genesis,
            records=decision_records,
            links=links["decisions"],
            sealed_head=decision_head,
        )

        primary = _ledger_rows(
            root,
            "ledger.jsonl",
            profile="primary",
            sealed=sealed,
        )
        stress = _ledger_rows(
            root,
            "ledger_stress_2x.jsonl",
            profile="stress_2x",
            sealed=sealed,
        )
        if primary is not None and stress is not None:
            if len(primary.objects) != len(stress.objects):
                raise _VerificationFailure(
                    "primary/stress ledger row count mismatch",
                    path="ledger_stress_2x.jsonl",
                )
        metrics_bytes: bytes | None = None
        if (root / "metrics.json").exists():
            metrics, metrics_bytes = _read_document(
                root,
                "metrics.json",
                schema="metrics",
                newline="require",
            )
            if metrics.get("run_id") != run_id:
                raise _VerificationFailure(
                    "metrics.json run_id does not match manifest",
                    path="metrics.json",
                )
        elif sealed:
            raise _VerificationFailure(
                "metrics.json: missing required file",
                path="metrics.json",
            )

        committed_event_count = len(links["events"])
        committed_events = events_read.objects[:committed_event_count]
        observation_count, raw_count, disclosed_count = _verify_blobs(
            root,
            events=committed_events,
            seal=seal,
            run_id=run_id,
        )

        if seal is None:
            inventory = {
                "events": committed_event_count,
                "event_records_present": len(events_read.records),
                "decisions": len(links["decisions"]),
                "decision_records_present": len(decision_records),
                "observations": observation_count,
                "raw": raw_count,
                "raw_disclosed": disclosed_count,
            }
            turns = _turn_inventory(
                root,
                committed_events,
                decisions[: len(links["decisions"])],
            )
            residue = []
            if len(events_read.records) > committed_event_count:
                residue.append("one unlinked event")
            if len(decision_records) > len(links["decisions"]):
                residue.append("one unlinked decision")
            if events_read.partial_tail:
                residue.append("partial event bytes")
            if chain_read.partial_tail:
                residue.append("partial chain-link bytes")
            suffix = (
                "; crash residue: " + ", ".join(residue)
                if residue
                else ""
            )
            return VerificationResult(
                verdict="TRUNCATED",
                message=(
                    "unsealed bundle has a valid committed prefix"
                    + suffix
                ),
                stream=None,
                seq=None,
                path=None,
                root=None,
                last_good={
                    "events": len(links["events"]) - 1,
                    "decisions": len(links["decisions"]) - 1,
                },
                inventory=inventory,
                turns=turns,
            )

        if not events_read.objects:
            raise _VerificationFailure(
                "sealed event stream is empty",
                path="events.jsonl",
                stream="events",
                seq=0,
            )
        final_event = events_read.objects[-1]
        if final_event.get("type") != "EpisodeEnd":
            raise _VerificationFailure(
                "sealed event stream does not end with EpisodeEnd",
                path="events.jsonl",
                stream="events",
                seq=len(events_read.objects) - 1,
            )
        if metrics_bytes is None:
            raise _VerificationFailure(
                "metrics.json bytes unavailable",
                path="metrics.json",
            )
        final_payload = final_event.get("payload")
        if not isinstance(final_payload, dict) or final_payload.get(
            "metrics_sha256"
        ) != sha256_prefixed(metrics_bytes):
            raise _VerificationFailure(
                "EpisodeEnd.metrics_sha256 does not match metrics.json",
                path="events.jsonl",
                stream="events",
                seq=len(events_read.objects) - 1,
            )
        if primary is None:
            raise _VerificationFailure(
                "primary ledger unavailable",
                path="ledger.jsonl",
            )
        _verify_decision_projection(
            events=events_read.objects,
            decisions=decisions,
            decision_bytes=decision_records,
            ledger=primary,
            run_id=run_id,
        )
        _verify_seal_files(root, seal)
        blobs_value = seal.get("blobs")
        if not isinstance(blobs_value, dict):
            raise _VerificationFailure(
                "chain.json blobs is not an object",
                path="chain.json",
            )
        obs_block = blobs_value.get("observations")
        raw_block = blobs_value.get("raw")
        if (
            not isinstance(obs_block, dict)
            or obs_block.get("count") != observation_count
        ):
            raise _VerificationFailure(
                "chain.json observation blob count mismatch",
                path="chain.json",
            )
        if (
            not isinstance(raw_block, dict)
            or raw_block.get("count") != raw_count
        ):
            raise _VerificationFailure(
                "chain.json raw blob count mismatch",
                path="chain.json",
            )
        inventory = {
            "events": len(events_read.objects),
            "event_records_present": len(events_read.records),
            "decisions": len(decisions),
            "decision_records_present": len(decision_records),
            "observations": observation_count,
            "raw": raw_count,
            "raw_disclosed": disclosed_count,
            "ledger_rows": len(primary.objects),
        }
        turns = _turn_inventory(root, events_read.objects, decisions)
        disclosure = (
            f"; {disclosed_count} raw blobs absent and disclosed"
            if disclosed_count
            else ""
        )
        return VerificationResult(
            verdict="COMPLETE",
            message="sealed bundle verifies completely" + disclosure,
            stream=None,
            seq=None,
            path=None,
            root=root_value,
            last_good={
                "events": len(events_read.objects) - 1,
                "decisions": len(decisions) - 1,
            },
            inventory=inventory,
            turns=turns,
        )
    except (
        _VerificationFailure,
        RecorderError,
        OSError,
        ValueError,
    ) as exc:
        if isinstance(exc, _VerificationFailure):
            failure = exc
        else:
            failure = _VerificationFailure(str(exc))
        inventory = {
            "events": len(links_for_inventory["events"]),
            "event_records_present": len(events_for_inventory),
            "decisions": len(links_for_inventory["decisions"]),
            "decision_records_present": len(decisions_for_inventory),
        }
        turns = _turn_inventory(
            root,
            events_for_inventory,
            decisions_for_inventory,
        )
        last_good = {
            "events": len(links_for_inventory["events"]) - 1,
            "decisions": len(links_for_inventory["decisions"]) - 1,
        }
        if failure.stream in last_good and failure.seq is not None:
            last_good[failure.stream] = failure.seq - 1
        return VerificationResult(
            verdict="CORRUPT",
            message=failure.message,
            stream=failure.stream,
            seq=failure.seq,
            path=failure.path,
            root=root_value,
            last_good=last_good,
            inventory=inventory,
            turns=turns,
        )
