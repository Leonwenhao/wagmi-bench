# SPDX-License-Identifier: Apache-2.0
"""Offline, exact-version economic replay of a sealed IC-5 bundle.

Replay deliberately does not contact the original agent.  It reconstructs an
in-process adapter from the bundle's committed raw responses and terminal
timeout/error events, then asks the deterministic engine to consume that
recorded trace.  The stored ledgers, metrics, and derived decision records are
the comparison oracle; any byte difference is a refusal, never a warning.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeGuard, cast

from core.config import EpisodeConfig
from core.engine import ENGINE_VERSION, EpisodeResult, run_episode
from core.pack import PackData, load_pack
from harness.protocol import AgentReply, DecisionTimeout, HarnessEvent
from spec.canonical import canonical_bytes, content_hash, sha256_prefixed

JsonObject = dict[str, object]

_VERSIONS_PATH: Final = (
    Path(__file__).resolve().parents[1] / "spec" / "schemas" / "VERSIONS.json"
)
_RAW_PREFIX: Final = "raw/"


class ReplayError(RuntimeError):
    """A bundle cannot be replayed exactly."""


class ReplayCompatibilityError(ReplayError):
    """The pinned replay inputs do not match this installation."""


class ReplayMismatchError(ReplayError):
    """A regenerated artifact differs from its stored bytes."""


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Successful byte-comparison receipt."""

    run_id: str
    bundle_root: str
    files_compared: tuple[str, ...]
    decisions_compared: int


@dataclass(frozen=True, slots=True)
class _TerminalDecision:
    kind: str
    payload: JsonObject


@dataclass(frozen=True, slots=True)
class _RecordedTurn:
    replies: Mapping[int, AgentReply]
    harness_events: tuple[HarnessEvent, ...]
    terminal: _TerminalDecision


def _reject_float(token: str) -> None:
    raise ReplayError(f"fractional JSON number is forbidden: {token}")


def _reject_constant(token: str) -> None:
    raise ReplayError(f"non-finite JSON number is forbidden: {token}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, *, source: Path) -> object:
    try:
        return cast(
            object,
            json.loads(
                raw.decode("utf-8"),
                parse_float=_reject_float,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            ),
        )
    except UnicodeDecodeError as exc:
        raise ReplayError(f"{source}: invalid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{source}: invalid JSON at byte {exc.pos}") from exc


def _load_object(path: Path) -> JsonObject:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReplayError(f"cannot read required bundle file: {path}") from exc
    value = _decode_json(raw, source=path)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ReplayError(f"{path}: expected a JSON object")
    return cast(JsonObject, value)


def _load_jsonl(path: Path) -> list[JsonObject]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReplayError(f"cannot read required bundle file: {path}") from exc
    rows: list[JsonObject] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line:
            raise ReplayError(f"{path}:{line_number}: blank JSONL line")
        value = _decode_json(line, source=path)
        if not isinstance(value, dict) or not all(
            isinstance(key, str) for key in value
        ):
            raise ReplayError(f"{path}:{line_number}: expected a JSON object")
        rows.append(cast(JsonObject, value))
    return rows


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ReplayError(f"{field} must be an object")
    return cast(JsonObject, value)


def _as_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ReplayError(f"{field} must be a string")
    return value


def _as_int(value: object, field: str, *, minimum: int = 0) -> int:
    if not _is_int(value) or value < minimum:
        raise ReplayError(f"{field} must be an integer >= {minimum}")
    return value


def _safe_bundle_file(bundle_dir: Path, relative: str) -> Path:
    if not relative.startswith(_RAW_PREFIX):
        raise ReplayError(f"recorded response ref is outside raw/: {relative}")
    candidate = bundle_dir / relative
    if candidate.is_symlink():
        raise ReplayError(f"recorded response ref must not be a symlink: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ReplayError(f"recorded response blob is missing: {relative}") from exc
    if not resolved.is_relative_to(bundle_dir.resolve()):
        raise ReplayError(f"recorded response ref escapes bundle: {relative}")
    if not resolved.is_file():
        raise ReplayError(f"recorded response ref is not a file: {relative}")
    return resolved


def _require_complete(bundle_dir: Path) -> str:
    """Delegate chain/schema verification without coupling import order."""

    try:
        from recorder.verify import verify_bundle
    except ImportError as exc:
        raise ReplayError("bundle verifier is unavailable") from exc

    receipt = verify_bundle(bundle_dir)
    if not receipt.is_complete:
        detail = receipt.message
        if receipt.path is not None:
            detail += f" path={receipt.path}"
        raise ReplayError(
            f"replay requires a COMPLETE bundle; "
            f"verifier returned {receipt.verdict}: {detail}"
        )
    root = receipt.root
    if not isinstance(root, str):
        raise ReplayError("complete bundle verification returned no root")
    return root


def _current_spec_versions() -> JsonObject:
    registry = _load_object(_VERSIONS_PATH)
    return _as_object(registry.get("versions"), "VERSIONS.json.versions")


def _pack_content_hash(pack: PackData) -> str:
    files = pack.manifest.get("files")
    if not isinstance(files, list):
        raise ReplayError("pack manifest files must be an array")
    projection_rows: list[JsonObject] = []
    for index, value in enumerate(files):
        entry = _as_object(value, f"pack.files[{index}]")
        relative = _as_str(entry.get("path"), f"pack.files[{index}].path")
        path = (pack.root / relative).resolve()
        if not path.is_relative_to(pack.root.resolve()):
            raise ReplayError(f"pack series path escapes pack directory: {relative}")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ReplayError(f"cannot read pack series: {relative}") from exc
        records = len(raw.splitlines())
        projection_rows.append(
            {
                "path": relative,
                "sha256": sha256_prefixed(raw),
                "bytes": len(raw),
                "records": records,
            }
        )
    projection_rows.sort(key=lambda row: cast(str, row["path"]))
    return content_hash(
        {
            "schema": "pack_content/v1",
            "files": projection_rows,
        }
    )


def _check_compatibility(
    manifest: Mapping[str, object],
    *,
    pack_dir: Path,
) -> PackData:
    recorded_engine = _as_str(
        manifest.get("engine_version"),
        "manifest.engine_version",
    )
    if recorded_engine != ENGINE_VERSION:
        raise ReplayCompatibilityError(
            "engine_version mismatch: "
            f"bundle requires {recorded_engine!r}, installed engine is "
            f"{ENGINE_VERSION!r}; install wagmibench=={recorded_engine}"
        )

    recorded_versions = _as_object(
        manifest.get("spec_versions"),
        "manifest.spec_versions",
    )
    current_versions = _current_spec_versions()
    if recorded_versions != current_versions:
        raise ReplayCompatibilityError(
            "spec_versions mismatch: "
            f"bundle={recorded_versions!r}, installed={current_versions!r}; "
            f"install wagmibench=={recorded_engine}"
        )

    pack_ref = _as_object(manifest.get("pack"), "manifest.pack")
    recorded_pack_id = _as_str(pack_ref.get("pack_id"), "manifest.pack.pack_id")
    recorded_content_hash = _as_str(
        pack_ref.get("content_hash"),
        "manifest.pack.content_hash",
    )
    recorded_manifest_hash = _as_str(
        pack_ref.get("manifest_sha256"),
        "manifest.pack.manifest_sha256",
    )
    manifest_path = pack_dir / "manifest.json"
    try:
        pack_manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise ReplayCompatibilityError(
            f"cannot read replay pack manifest: {manifest_path}"
        ) from exc
    actual_manifest_hash = sha256_prefixed(pack_manifest_raw)
    if actual_manifest_hash != recorded_manifest_hash:
        raise ReplayCompatibilityError(
            "pack.manifest_sha256 mismatch: "
            f"bundle={recorded_manifest_hash}, found={actual_manifest_hash} "
            f"at {manifest_path}"
        )

    try:
        pack = load_pack(pack_dir)
    except (OSError, ValueError) as exc:
        raise ReplayCompatibilityError(
            f"replay pack failed integrity loading: {exc}"
        ) from exc
    if pack.pack_id != recorded_pack_id:
        raise ReplayCompatibilityError(
            "pack.pack_id mismatch: "
            f"bundle={recorded_pack_id!r}, found={pack.pack_id!r}"
        )
    actual_content_hash = _pack_content_hash(pack)
    if actual_content_hash != recorded_content_hash:
        raise ReplayCompatibilityError(
            "pack.content_hash mismatch: "
            f"bundle={recorded_content_hash}, found={actual_content_hash}"
        )
    if pack.content_hash != actual_content_hash:
        raise ReplayCompatibilityError(
            "pack manifest content_hash is internally inconsistent: "
            f"declared={pack.content_hash}, recomputed={actual_content_hash}"
        )
    return pack


def _recorded_turns(
    bundle_dir: Path,
    events: Sequence[Mapping[str, object]],
) -> tuple[dict[int, _RecordedTurn], tuple[HarnessEvent, ...]]:
    replies: dict[int, dict[int, AgentReply]] = {}
    redacted_replies: set[tuple[int, int]] = set()
    harness_events: dict[int, list[HarnessEvent]] = {}
    final_harness_events: list[HarnessEvent] = []
    terminals: dict[int, _TerminalDecision] = {}
    for event in events:
        event_type = event.get("type")
        if event_type not in {
            "AgentResponded",
            "EgressBlocked",
            "ActionParsed",
            "ActionRejected",
        }:
            continue
        payload = _as_object(event.get("payload"), f"{event_type}.payload")
        if event_type == "EgressBlocked":
            if event.get("source") != "harness":
                raise ReplayError("EgressBlocked source must be harness")
            destination = _as_str(
                payload.get("destination"),
                "EgressBlocked.destination",
            )
            port_raw = payload.get("port")
            if port_raw is None:
                port = None
            else:
                port = _as_int(port_raw, "EgressBlocked.port")
                if port > 65_535:
                    raise ReplayError(
                        "EgressBlocked.port must be <= 65535"
                    )
            protocol = _as_str(
                payload.get("protocol"),
                "EgressBlocked.protocol",
            )
            if protocol not in {"https", "dns", "tcp", "udp", "other"}:
                raise ReplayError("EgressBlocked.protocol is unsupported")
            count = _as_int(
                payload.get("count"),
                "EgressBlocked.count",
                minimum=1,
            )
            recorded_event = HarnessEvent(
                type="EgressBlocked",
                payload={
                    "destination": destination,
                    "port": port,
                    "protocol": protocol,
                    "count": count,
                },
            )
            turn_raw = event.get("turn")
            if turn_raw is None:
                final_harness_events.append(recorded_event)
            else:
                turn = _as_int(turn_raw, "EgressBlocked.turn")
                harness_events.setdefault(turn, []).append(recorded_event)
            continue

        turn = _as_int(event.get("turn"), f"{event_type}.turn")
        if event_type == "AgentResponded":
            attempt = _as_int(
                payload.get("attempt"),
                f"AgentResponded[{turn}].attempt",
                minimum=1,
            )
            if attempt not in (1, 2):
                raise ReplayError(
                    f"AgentResponded turn {turn} has invalid attempt {attempt}"
                )
            per_turn = replies.setdefault(turn, {})
            if attempt in per_turn:
                raise ReplayError(
                    f"duplicate AgentResponded for turn {turn} attempt {attempt}"
                )
            raw_ref = _as_str(
                payload.get("raw_ref"),
                f"AgentResponded[{turn}].raw_ref",
            )
            expected_ref = f"raw/{turn:04d}-a{attempt}.txt"
            if raw_ref != expected_ref:
                raise ReplayError(
                    f"AgentResponded turn {turn} attempt {attempt} raw_ref "
                    f"must be {expected_ref!r}, found {raw_ref!r}"
                )
            expected_hash = _as_str(
                payload.get("raw_sha256"),
                f"AgentResponded[{turn}].raw_sha256",
            )
            expected_bytes = _as_int(
                payload.get("raw_bytes"),
                f"AgentResponded[{turn}].raw_bytes",
            )
            raw_path = bundle_dir / raw_ref
            if raw_path.exists():
                raw = _safe_bundle_file(bundle_dir, raw_ref).read_bytes()
                if sha256_prefixed(raw) != expected_hash:
                    raise ReplayError(
                        f"recorded response hash mismatch: {raw_ref}"
                    )
                if len(raw) != expected_bytes:
                    raise ReplayError(
                        f"recorded response byte count mismatch: {raw_ref}"
                    )
            elif (bundle_dir / "redaction.json").is_file():
                # COMPLETE verification has already proved that this exact
                # absence is disclosed by redaction.json and matches the
                # AgentResponded hash/length commitment. Raw model text is
                # evidence, not an economic replay input (IC-5).
                raw = b""
                redacted_replies.add((turn, attempt))
            else:
                _safe_bundle_file(bundle_dir, raw_ref)
                raise ReplayError(f"recorded response blob is missing: {raw_ref}")
            latency_ms = _as_int(
                payload.get("latency_ms"),
                f"AgentResponded[{turn}].latency_ms",
            )
            http_status_raw = payload.get("http_status")
            http_status = (
                None
                if http_status_raw is None
                else _as_int(
                    http_status_raw,
                    f"AgentResponded[{turn}].http_status",
                )
            )
            transport = _as_str(
                payload.get("transport"),
                f"AgentResponded[{turn}].transport",
            )
            per_turn[attempt] = AgentReply(
                body=raw,
                latency_ms=latency_ms,
                http_status=http_status,
                transport=transport,
            )
            continue

        if turn in terminals:
            raise ReplayError(f"turn {turn} has multiple terminal decisions")
        terminals[turn] = _TerminalDecision(
            kind=event_type,
            payload=payload,
        )

    if not terminals:
        raise ReplayError("bundle event stream has no recorded decisions")
    expected_turns = set(range(max(terminals) + 1))
    if set(terminals) != expected_turns:
        raise ReplayError("recorded decision turns are not contiguous from zero")
    orphan_replies = set(replies) - set(terminals)
    if orphan_replies:
        raise ReplayError(
            "AgentResponded events have no terminal decision at turns: "
            + ", ".join(str(turn) for turn in sorted(orphan_replies))
        )
    orphan_harness_events = set(harness_events) - set(terminals)
    if orphan_harness_events:
        raise ReplayError(
            "EgressBlocked events have no terminal decision at turns: "
            + ", ".join(
                str(turn) for turn in sorted(orphan_harness_events)
            )
        )
    result: dict[int, _RecordedTurn] = {}
    for turn in sorted(terminals):
        terminal = terminals[turn]
        turn_replies = dict(replies.get(turn, {}))
        for reply_attempt in sorted(turn_replies):
            if (turn, reply_attempt) not in redacted_replies:
                continue
            original = turn_replies[reply_attempt]
            synthetic = _synthetic_redacted_body(
                terminal,
                attempt=reply_attempt,
            )
            turn_replies[reply_attempt] = AgentReply(
                body=synthetic,
                latency_ms=original.latency_ms,
                http_status=original.http_status,
                transport=original.transport,
            )
        if terminal.kind == "ActionParsed":
            from_attempt = _as_int(
                terminal.payload.get("from_attempt"),
                f"ActionParsed[{turn}].from_attempt",
                minimum=1,
            )
            if from_attempt not in turn_replies:
                raise ReplayError(
                    f"ActionParsed turn {turn} has no raw response for "
                    f"attempt {from_attempt}"
                )
        else:
            attempts = _as_int(
                terminal.payload.get("attempts"),
                f"ActionRejected[{turn}].attempts",
                minimum=1,
            )
            if attempts not in (1, 2):
                raise ReplayError(
                    f"ActionRejected turn {turn} has invalid attempts={attempts}"
                )
            if any(attempt > attempts for attempt in turn_replies):
                raise ReplayError(
                    f"ActionRejected turn {turn} has response after final attempt"
                )
        result[turn] = _RecordedTurn(
            replies=turn_replies,
            harness_events=tuple(harness_events.get(turn, ())),
            terminal=terminal,
        )
    return result, tuple(final_harness_events)


def _wire_leverage(value: object, *, field: str) -> int | str:
    leverage = _as_int(value, field, minimum=-(2**53 - 1))
    if leverage % 10_000 == 0:
        return leverage // 10_000
    sign = "-" if leverage < 0 else ""
    whole, fraction = divmod(abs(leverage), 10_000)
    decimal = f"{fraction:04d}".rstrip("0")
    return f"{sign}{whole}.{decimal}"


def _synthetic_redacted_body(
    terminal: _TerminalDecision,
    *,
    attempt: int,
) -> bytes:
    """Reconstruct only the parser outcome needed for economic replay.

    A share bundle intentionally omits the original model prose. For a
    successful turn the chained ActionParsed payload contains the complete
    canonical intent, so it can be rendered back into a valid action/v1
    response. Earlier malformed attempts and terminal invalid turns use a
    deterministic invalid JSON byte: their exact text and rejection subtype
    do not affect positions, ledgers, or metrics.
    """

    if terminal.kind != "ActionParsed":
        return b"{"
    from_attempt = _as_int(
        terminal.payload.get("from_attempt"),
        "ActionParsed.from_attempt",
        minimum=1,
    )
    if attempt < from_attempt:
        return b"{"
    if attempt != from_attempt:
        raise ReplayError(
            f"redacted parsed turn has response attempt {attempt} after "
            f"from_attempt={from_attempt}"
        )
    targets = _as_object(
        terminal.payload.get("target_lev_1e4"),
        "ActionParsed.target_lev_1e4",
    )
    action: JsonObject = {
        "schema": "action/v1",
        "intent_kind": _as_str(
            terminal.payload.get("intent_kind"),
            "ActionParsed.intent_kind",
        ),
        "target": {
            alias: _wire_leverage(
                targets[alias],
                field=f"ActionParsed.target_lev_1e4.{alias}",
            )
            for alias in sorted(targets)
        },
    }
    max_slippage = terminal.payload.get("max_slippage_bps")
    if max_slippage is not None:
        action["max_slippage_bps"] = _as_int(
            max_slippage,
            "ActionParsed.max_slippage_bps",
        )
    return canonical_bytes(action)


class _RecordedTraceAgent:
    """IC-6 adapter backed only by committed bundle evidence."""

    def __init__(
        self,
        turns: Mapping[int, _RecordedTurn],
        final_harness_events: tuple[HarnessEvent, ...],
    ) -> None:
        self._turns = turns
        self._final_harness_events = final_harness_events
        self._pending_harness_events: tuple[HarnessEvent, ...] = ()
        self._attempt_drain_due = False
        self._staged_turns: set[int] = set()

    def decide(self, request: dict[str, object]) -> AgentReply:
        observation = _as_object(
            request.get("observation"),
            "runner_request.observation",
        )
        episode = _as_object(
            observation.get("episode"),
            "runner_request.observation.episode",
        )
        turn = _as_int(episode.get("turn"), "observation.episode.turn")
        attempt = _as_int(
            request.get("attempt"),
            "runner_request.attempt",
            minimum=1,
        )
        recorded = self._turns.get(turn)
        if recorded is None:
            raise ReplayError(f"engine requested unrecorded turn {turn}")
        self._attempt_drain_due = True
        if turn not in self._staged_turns:
            self._pending_harness_events = recorded.harness_events
            self._staged_turns.add(turn)
        reply = recorded.replies.get(attempt)
        if reply is not None:
            return reply

        terminal = recorded.terminal
        if terminal.kind == "ActionParsed":
            raise ReplayError(
                f"engine requested missing raw attempt {attempt} at parsed turn {turn}"
            )
        reason = _as_str(
            terminal.payload.get("reason"),
            f"ActionRejected[{turn}].reason",
        )
        attempts = _as_int(
            terminal.payload.get("attempts"),
            f"ActionRejected[{turn}].attempts",
            minimum=1,
        )
        if attempt <= attempts:
            if reason == "timeout":
                raise DecisionTimeout(f"recorded timeout at turn {turn}")
            if reason == "agent_error":
                raise _RecordedAgentFailure(
                    f"recorded agent error at turn {turn}"
                )
            raise ReplayError(
                f"turn {turn} is missing raw bytes for recorded rejection "
                f"attempt {attempt}"
            )

        # Current V1 bundles always record the retry policy through their
        # response/terminal event sequence.  This fallback keeps an older
        # single-attempt invalid trace economically replayable if the current
        # engine asks for its default second parse attempt.
        if attempts == 1 and recorded.replies:
            return recorded.replies[max(recorded.replies)]
        raise ReplayError(
            f"engine requested attempt {attempt} beyond recorded trace at turn {turn}"
        )

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        """Reinject the committed turn trace, then the final run-scoped trace."""

        if self._attempt_drain_due:
            self._attempt_drain_due = False
            drained = self._pending_harness_events
            self._pending_harness_events = ()
            return drained
        drained = self._final_harness_events
        self._final_harness_events = ()
        return drained


class _RecordedAgentFailure(RuntimeError):
    """Non-economic stand-in for a recorded agent transport failure."""


def _canonical_jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_bytes(row) + b"\n" for row in rows)


def _first_difference(expected: bytes, actual: bytes) -> int:
    for offset, (left, right) in enumerate(zip(expected, actual, strict=False)):
        if left != right:
            return offset
    return min(len(expected), len(actual))


def _compare_bytes(path: Path, actual: bytes) -> None:
    try:
        expected = path.read_bytes()
    except OSError as exc:
        raise ReplayError(f"cannot read replay oracle: {path}") from exc
    if expected != actual:
        offset = _first_difference(expected, actual)
        raise ReplayMismatchError(
            f"byte mismatch in {path.name} at offset {offset}: "
            f"stored_bytes={len(expected)}, replay_bytes={len(actual)}"
        )


def _compare_decisions(
    *,
    bundle_dir: Path,
    events: Sequence[Mapping[str, object]],
    ledger_rows: Sequence[Mapping[str, object]],
    run_id: str,
) -> int:
    try:
        from recorder.decisions import generate_decision_records
    except ImportError as exc:
        raise ReplayError("decision projection module is unavailable") from exc

    records = generate_decision_records(events, ledger_rows, run_id)
    decision_dir = bundle_dir / "decisions"
    stored_paths = sorted(decision_dir.glob("*.json"))
    if len(stored_paths) != len(records):
        raise ReplayMismatchError(
            "decision count mismatch: "
            f"stored={len(stored_paths)}, regenerated={len(records)}"
        )
    for turn, record in enumerate(records):
        expected_path = decision_dir / f"{turn:04d}.json"
        if stored_paths[turn] != expected_path:
            raise ReplayMismatchError(
                f"decision filename gap: expected {expected_path.name}, "
                f"found {stored_paths[turn].name}"
            )
        _compare_bytes(expected_path, canonical_bytes(record))
    return len(records)


def replay_bundle(
    bundle_dir: str | Path,
    *,
    pack_dir: str | Path,
) -> ReplayResult:
    """Replay a complete bundle and prove its derived bytes are identical.

    ``pack_dir`` is explicit because IC-5 references scenario data by hash; it
    never rehosts or embeds upstream market data inside the bundle.
    """

    bundle = Path(bundle_dir)
    pack_path = Path(pack_dir)
    bundle_root = _require_complete(bundle)
    manifest = _load_object(bundle / "manifest.json")
    run_id = _as_str(manifest.get("run_id"), "manifest.run_id")
    episode_id = _as_str(manifest.get("episode_id"), "manifest.episode_id")

    agent_manifest_raw = (bundle / "agent_manifest.json").read_bytes()
    expected_agent_hash = _as_str(
        manifest.get("agent_manifest_sha256"),
        "manifest.agent_manifest_sha256",
    )
    actual_agent_hash = sha256_prefixed(agent_manifest_raw)
    if actual_agent_hash != expected_agent_hash:
        raise ReplayCompatibilityError(
            "agent_manifest_sha256 mismatch: "
            f"bundle={expected_agent_hash}, found={actual_agent_hash}"
        )

    _check_compatibility(manifest, pack_dir=pack_path)
    run_config = _as_object(manifest.get("run_config"), "manifest.run_config")
    try:
        config = EpisodeConfig.from_mapping(run_config)
    except ValueError as exc:
        raise ReplayCompatibilityError(
            f"manifest.run_config is not replayable: {exc}"
        ) from exc

    events = _load_jsonl(bundle / "events.jsonl")
    recorded_turns, final_harness_events = _recorded_turns(bundle, events)
    result: EpisodeResult = run_episode(
        pack_dir=pack_path,
        agent=_RecordedTraceAgent(recorded_turns, final_harness_events),
        config=config,
        run_id=run_id,
        episode_id=episode_id,
    )

    _compare_bytes(
        bundle / "ledger.jsonl",
        _canonical_jsonl(result.ledger_primary),
    )
    _compare_bytes(
        bundle / "ledger_stress_2x.jsonl",
        _canonical_jsonl(result.ledger_stress_2x),
    )
    _compare_bytes(
        bundle / "metrics.json",
        canonical_bytes(result.metrics) + b"\n",
    )
    stored_primary = _load_jsonl(bundle / "ledger.jsonl")
    decisions_compared = _compare_decisions(
        bundle_dir=bundle,
        events=events,
        ledger_rows=stored_primary,
        run_id=run_id,
    )
    return ReplayResult(
        run_id=run_id,
        bundle_root=bundle_root,
        files_compared=(
            "ledger.jsonl",
            "ledger_stress_2x.jsonl",
            "metrics.json",
        ),
        decisions_compared=decisions_compared,
    )
