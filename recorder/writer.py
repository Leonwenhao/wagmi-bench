# SPDX-License-Identifier: Apache-2.0
"""Crash-prefix-safe writer for frozen IC-5 evidence bundles.

The recorder is the only component that turns an in-memory engine result into
stored evidence.  Standalone JSON documents are JCS bytes, JSONL records are
JCS bytes followed by ``\n``, and hash links always cover the stored record
bytes excluding the JSONL newline.

``chain.jsonl`` is the commit boundary for incremental records.  A record is
fsynced before its link is appended and fsynced.  If the process is killed in
that small interval, verification reports the single unlinked tail as
TRUNCATED; a mismatch anywhere inside the linked prefix is CORRUPT.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from core.config import time_rebase_offset_ms
from core.engine import ENGINE_VERSION, EpisodeResult
from recorder.decisions import generate_decision_records
from spec.canonical import (
    ChainBuilder,
    canonical_bytes,
    chain_genesis,
    run_config_sha256,
    seal_root,
    sha256_prefixed,
)

JsonObject = dict[str, object]

ROOT: Final = Path(__file__).resolve().parents[1]
SCHEMA_DIR: Final = ROOT / "spec" / "schemas"
SCHEMA_NAMES: Final[tuple[str, ...]] = (
    "action",
    "agent_manifest",
    "bar_row",
    "bundle_manifest",
    "chain",
    "decision_record",
    "event",
    "funding_row",
    "ledger_row",
    "metrics",
    "observation",
    "pack_manifest",
    "redaction",
    "runner_request",
    "runner_response",
)
_RAW_REF: Final = re.compile(r"raw/[0-9]{4}-a[12]\.txt\Z")
_RUN_ID: Final = re.compile(r"run_[0-9a-f]{16}\Z")


class RecorderError(RuntimeError):
    """The requested bundle write would violate the frozen contract."""


class RecorderValidationError(RecorderError):
    """A value is not valid under its frozen schema."""


def _schema_object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RecorderError(f"{path}: schema root is not an object")
    return cast(JsonObject, value)


_SCHEMAS: Final[dict[str, JsonObject]] = {
    name: _schema_object(SCHEMA_DIR / f"{name}.v1.schema.json")
    for name in SCHEMA_NAMES
}
_REGISTRY: Final = Registry().with_resources(
    (
        cast(str, schema["$id"]),
        Resource.from_contents(schema),
    )
    for schema in _SCHEMAS.values()
)
_VALIDATORS: Final[dict[str, Draft202012Validator]] = {
    name: Draft202012Validator(schema, registry=_REGISTRY)
    for name, schema in _SCHEMAS.items()
}


def validate_instance(name: str, value: object, *, context: str) -> None:
    """Validate ``value`` against one frozen v1 schema.

    Raises a deterministic, compact error naming the first schema problem.
    Canonical encoding is also attempted so floats and non-I-JSON integers
    are rejected even where JSON Schema's number model is more permissive.
    """

    validator = _VALIDATORS.get(name)
    if validator is None:
        raise RecorderValidationError(f"unknown schema {name!r}")
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path)
        suffix = f" at {location}" if location else ""
        raise RecorderValidationError(
            f"{context}{suffix}: {error.message}"
        )
    try:
        canonical_bytes(value)
    except ValueError as exc:
        raise RecorderValidationError(f"{context}: {exc}") from exc


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("write returned no progress")
        offset += written


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_new_file(path: Path, data: bytes) -> None:
    """Durably install a new file without ever exposing partial target bytes."""

    if path.exists():
        raise RecorderError(f"refusing to overwrite immutable file: {path}")
    temporary = path.with_name(f".{path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
    except FileExistsError as exc:
        raise RecorderError(f"stale recorder temporary file: {temporary}") from exc
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        raise
    else:
        os.close(fd)
    if path.exists():
        raise RecorderError(f"refusing to overwrite immutable file: {path}")
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _append_fsync(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _canonical_jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_bytes(row) + b"\n" for row in rows)


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise RecorderError(f"{context} must be an object")
    return cast(Mapping[str, object], value)


def _integer(value: object, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RecorderError(f"{context} must be an integer >= {minimum}")
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise RecorderError(f"{context} must be a string")
    return value


def build_bundle_manifest(
    result: EpisodeResult,
    agent_manifest: Mapping[str, object],
    *,
    created_at_ms: int,
    host: Mapping[str, object],
) -> JsonObject:
    """Build the immutable manifest for an already chosen episode identity.

    ``created_at_ms`` is explicit: the harness owns the protocol's single
    permitted wall-clock read.  This helper never reads wall time.
    """

    validate_instance(
        "agent_manifest",
        agent_manifest,
        context="agent_manifest",
    )
    created = _integer(created_at_ms, "created_at_ms")
    pack_manifest_path = result.pack.root / "manifest.json"
    try:
        pack_manifest_bytes = pack_manifest_path.read_bytes()
    except OSError as exc:
        raise RecorderError(
            f"cannot read pack manifest: {pack_manifest_path}"
        ) from exc
    versions_value = json.loads(
        (SCHEMA_DIR / "VERSIONS.json").read_text(encoding="utf-8")
    )
    versions_container = _mapping(versions_value, "VERSIONS.json")
    versions = _mapping(
        versions_container.get("versions"),
        "VERSIONS.json.versions",
    )
    manifest: JsonObject = {
        "schema": "bundle_manifest/v1",
        "run_id": result.run_id,
        "episode_id": result.episode_id,
        "created_at_ms": created,
        "pack": {
            "pack_id": result.pack.pack_id,
            "content_hash": result.pack.content_hash,
            "manifest_sha256": sha256_prefixed(pack_manifest_bytes),
        },
        "agent_manifest_sha256": sha256_prefixed(
            canonical_bytes(agent_manifest)
        ),
        "engine_version": ENGINE_VERSION,
        "spec_versions": dict(versions),
        "time_rebase_offset_ms": time_rebase_offset_ms(
            result.pack.window_start_ts
        ),
        "run_config": result.config.to_run_config(),
        "host": dict(host),
    }
    validate_instance("bundle_manifest", manifest, context="manifest")
    return manifest


class BundleWriter:
    """Incrementally write one new evidence bundle.

    Instances cannot resume or overwrite a directory.  Recovery is a verifier
    concern: an interrupted directory remains immutable evidence of the
    verified prefix that reached ``chain.jsonl``.
    """

    def __init__(
        self,
        *,
        root: Path,
        manifest: JsonObject,
        agent_manifest: JsonObject,
        event_chain: ChainBuilder,
        decision_chain: ChainBuilder,
    ) -> None:
        self.root = root
        self.manifest = manifest
        self.agent_manifest = agent_manifest
        self._event_chain = event_chain
        self._decision_chain = decision_chain
        self._observations: set[int] = set()
        self._raw_refs: set[str] = set()
        self._last_event: JsonObject | None = None
        self._finalized = False

    @classmethod
    def create(
        cls,
        bundle_dir: str | Path,
        *,
        manifest: Mapping[str, object],
        agent_manifest: Mapping[str, object],
    ) -> BundleWriter:
        """Create a fresh bundle and durably write both immutable manifests."""

        root = Path(bundle_dir)
        if root.exists():
            raise RecorderError(f"bundle directory already exists: {root}")
        root.mkdir(parents=True)
        (root / "observations").mkdir()
        (root / "raw").mkdir()
        (root / "decisions").mkdir()
        _fsync_directory(root)
        _fsync_directory(root.parent)

        agent = dict(agent_manifest)
        bundle = dict(manifest)
        validate_instance("agent_manifest", agent, context="agent_manifest")
        validate_instance("bundle_manifest", bundle, context="manifest")
        agent_bytes = canonical_bytes(agent)
        agent_hash = sha256_prefixed(agent_bytes)
        if bundle.get("agent_manifest_sha256") != agent_hash:
            raise RecorderError(
                "manifest.agent_manifest_sha256 does not match "
                "agent_manifest canonical bytes"
            )
        run_id = _string(bundle.get("run_id"), "manifest.run_id")
        if _RUN_ID.fullmatch(run_id) is None:
            raise RecorderError("manifest.run_id is not a frozen run id")
        pack = _mapping(bundle.get("pack"), "manifest.pack")
        pack_hash = _string(
            pack.get("content_hash"),
            "manifest.pack.content_hash",
        )
        run_config = _mapping(
            bundle.get("run_config"),
            "manifest.run_config",
        )
        config_hash = run_config_sha256(run_config)

        _atomic_new_file(root / "agent_manifest.json", agent_bytes)
        _atomic_new_file(root / "manifest.json", canonical_bytes(bundle))
        _atomic_new_file(root / "events.jsonl", b"")
        _atomic_new_file(root / "chain.jsonl", b"")

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
        return cls(
            root=root,
            manifest=bundle,
            agent_manifest=agent,
            event_chain=ChainBuilder("events", event_genesis),
            decision_chain=ChainBuilder("decisions", decision_genesis),
        )

    @property
    def event_count(self) -> int:
        return self._event_chain.count

    @property
    def decision_count(self) -> int:
        return self._decision_chain.count

    def _require_open(self) -> None:
        if self._finalized:
            raise RecorderError("bundle is already finalized")

    def write_observation(
        self,
        turn: int,
        observation: Mapping[str, object],
    ) -> Path:
        """Write the exact observation bytes served for ``turn``."""

        self._require_open()
        turn_number = _integer(turn, "turn")
        if turn_number in self._observations:
            raise RecorderError(f"observation turn {turn_number} already written")
        document = dict(observation)
        validate_instance(
            "observation",
            document,
            context=f"observation turn {turn_number}",
        )
        path = self.root / "observations" / f"{turn_number:04d}.json"
        _atomic_new_file(path, canonical_bytes(document))
        self._observations.add(turn_number)
        return path

    def write_raw(self, relative_ref: str, data: bytes) -> Path:
        """Write one verbatim model-response blob at its frozen reference."""

        self._require_open()
        if _RAW_REF.fullmatch(relative_ref) is None:
            raise RecorderError(f"invalid raw blob reference: {relative_ref!r}")
        if relative_ref in self._raw_refs:
            raise RecorderError(f"raw blob already written: {relative_ref}")
        if not isinstance(data, bytes):
            raise RecorderError("raw blob must be bytes")
        path = self.root / relative_ref
        _atomic_new_file(path, data)
        self._raw_refs.add(relative_ref)
        return path

    def append_event(self, event: Mapping[str, object]) -> None:
        """Append, fsync, hash, link, and fsync one IC-4 event."""

        self._require_open()
        record = dict(event)
        validate_instance(
            "event",
            record,
            context=f"event seq {self._event_chain.count}",
        )
        seq = _integer(record.get("seq"), "event.seq")
        if seq != self._event_chain.count:
            raise RecorderError(
                f"event seq must be {self._event_chain.count}, got {seq}"
            )
        record_bytes = canonical_bytes(record)
        _append_fsync(self.root / "events.jsonl", record_bytes + b"\n")
        link = self._event_chain.append(record_bytes)
        _append_fsync(
            self.root / "chain.jsonl",
            canonical_bytes(link) + b"\n",
        )
        self._last_event = record

    def append_decision(self, decision: Mapping[str, object]) -> Path:
        """Durably install and chain one materialized decision record."""

        self._require_open()
        record = dict(decision)
        turn = _integer(record.get("turn"), "decision.turn")
        if turn != self._decision_chain.count:
            raise RecorderError(
                "decision turn must be "
                f"{self._decision_chain.count}, got {turn}"
            )
        if record.get("run_id") != self.manifest.get("run_id"):
            raise RecorderError("decision.run_id does not match manifest.run_id")
        validate_instance(
            "decision_record",
            record,
            context=f"decision turn {turn}",
        )
        record_bytes = canonical_bytes(record)
        path = self.root / "decisions" / f"{turn:04d}.json"
        _atomic_new_file(path, record_bytes)
        link = self._decision_chain.append(record_bytes)
        _append_fsync(
            self.root / "chain.jsonl",
            canonical_bytes(link) + b"\n",
        )
        return path

    def write_ledgers(
        self,
        primary: Sequence[Mapping[str, object]],
        stress_2x: Sequence[Mapping[str, object]],
    ) -> None:
        """Write both mandatory ledger projections after validating every row."""

        self._require_open()
        if len(primary) != len(stress_2x):
            raise RecorderError("primary and stress ledgers have different lengths")
        for profile, rows in (
            ("primary", primary),
            ("stress_2x", stress_2x),
        ):
            for index, row in enumerate(rows):
                validate_instance(
                    "ledger_row",
                    row,
                    context=f"{profile} ledger row {index}",
                )
                if row.get("profile") != profile:
                    raise RecorderError(
                        f"{profile} ledger row {index} has wrong profile"
                    )
                d_nav = _integer(
                    row.get("d_nav_micro"),
                    f"{profile} ledger row {index}.d_nav_micro",
                    minimum=-(2**53 - 1),
                )
                components = sum(
                    _integer(
                        row.get(field),
                        f"{profile} ledger row {index}.{field}",
                        minimum=-(2**53 - 1),
                    )
                    for field in (
                        "d_price_pnl_micro",
                        "d_funding_micro",
                        "d_fees_micro",
                        "d_liq_penalty_micro",
                    )
                )
                if d_nav != components:
                    raise RecorderError(
                        f"{profile} ledger row {index} violates MATH-2"
                    )
        _atomic_new_file(
            self.root / "ledger.jsonl",
            _canonical_jsonl(primary),
        )
        _atomic_new_file(
            self.root / "ledger_stress_2x.jsonl",
            _canonical_jsonl(stress_2x),
        )

    def write_metrics(self, metrics: Mapping[str, object]) -> None:
        """Write the metrics document using the engine's newline convention."""

        self._require_open()
        document = dict(metrics)
        validate_instance("metrics", document, context="metrics")
        if document.get("run_id") != self.manifest.get("run_id"):
            raise RecorderError("metrics.run_id does not match manifest.run_id")
        _atomic_new_file(
            self.root / "metrics.json",
            canonical_bytes(document) + b"\n",
        )

    def finalize(self) -> JsonObject:
        """Write the finalize-only seal and return it.

        No mutable status bit is used: a valid ``chain.json`` plus the final
        ``EpisodeEnd`` are the two independent completeness markers.
        """

        self._require_open()
        if self._last_event is None or self._last_event.get("type") != "EpisodeEnd":
            raise RecorderError("cannot finalize without final EpisodeEnd")
        if self._decision_chain.count != len(self._observations):
            raise RecorderError(
                "decision/observation count mismatch: "
                f"{self._decision_chain.count} vs {len(self._observations)}"
            )
        if self._observations != set(range(self._decision_chain.count)):
            raise RecorderError("observation turns are not contiguous from zero")
        required_files = (
            "manifest.json",
            "agent_manifest.json",
            "ledger.jsonl",
            "ledger_stress_2x.jsonl",
            "metrics.json",
            "chain.jsonl",
        )
        missing = [name for name in required_files if not (self.root / name).is_file()]
        if missing:
            raise RecorderError(
                "cannot finalize; missing required file(s): "
                + ", ".join(missing)
            )

        payload = _mapping(self._last_event.get("payload"), "EpisodeEnd.payload")
        metrics_hash = sha256_prefixed((self.root / "metrics.json").read_bytes())
        if payload.get("metrics_sha256") != metrics_hash:
            raise RecorderError(
                "EpisodeEnd.metrics_sha256 does not match metrics.json bytes"
            )

        files = {
            name: sha256_prefixed((self.root / name).read_bytes())
            for name in required_files
        }
        seal: JsonObject = {
            "schema": "chain/v1",
            "run_id": self.manifest["run_id"],
            "streams": {
                "events": self._event_chain.stream_head(),
                "decisions": self._decision_chain.stream_head(),
            },
            "files": files,
            "blobs": {
                "observations": {"count": len(self._observations)},
                "raw": {"count": len(self._raw_refs)},
            },
            "complete": True,
        }
        seal["root"] = seal_root(seal)
        validate_instance("chain", seal, context="chain seal")
        _atomic_new_file(self.root / "chain.json", canonical_bytes(seal))
        self._finalized = True
        return seal


def record_episode_bundle(
    bundle_dir: str | Path,
    *,
    result: EpisodeResult,
    manifest: Mapping[str, object],
    agent_manifest: Mapping[str, object],
) -> JsonObject:
    """Record and finalize all products of one deterministic engine episode."""

    if manifest.get("run_id") != result.run_id:
        raise RecorderError("manifest.run_id does not match EpisodeResult.run_id")
    if manifest.get("episode_id") != result.episode_id:
        raise RecorderError(
            "manifest.episode_id does not match EpisodeResult.episode_id"
        )
    writer = BundleWriter.create(
        bundle_dir,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )
    for turn, observation in enumerate(result.observations):
        writer.write_observation(turn, observation)
    for relative_ref in sorted(result.raw_blobs):
        writer.write_raw(relative_ref, result.raw_blobs[relative_ref])
    for event in result.events:
        writer.append_event(event)
    decisions = generate_decision_records(
        result.events,
        result.ledger_primary,
        result.run_id,
    )
    for decision in decisions:
        writer.append_decision(decision)
    writer.write_ledgers(
        result.ledger_primary,
        result.ledger_stress_2x,
    )
    writer.write_metrics(result.metrics)
    return writer.finalize()
