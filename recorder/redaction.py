# SPDX-License-Identifier: Apache-2.0
"""Verifiable IC-5 share-profile sub-bundles."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard, cast

from spec.canonical import canonical_bytes, sha256_prefixed

JsonObject = dict[str, object]


class RedactionError(RuntimeError):
    """A share sub-bundle cannot be created without weakening evidence."""


@dataclass(frozen=True, slots=True)
class ShareBundleResult:
    """Successful share export receipt."""

    path: Path
    run_id: str
    parent_root: str
    removals: int


def _reject_float(token: str) -> None:
    raise RedactionError(f"fractional JSON number is forbidden: {token}")


def _reject_constant(token: str) -> None:
    raise RedactionError(f"non-finite JSON number is forbidden: {token}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RedactionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_object(raw: bytes, *, source: Path) -> JsonObject:
    try:
        value = cast(
            object,
            json.loads(
                raw.decode("utf-8"),
                parse_float=_reject_float,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            ),
        )
    except UnicodeDecodeError as exc:
        raise RedactionError(f"{source}: invalid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise RedactionError(
            f"{source}: invalid JSON at byte {exc.pos}"
        ) from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise RedactionError(f"{source}: expected a JSON object")
    return cast(JsonObject, value)


def _load_object(path: Path) -> JsonObject:
    try:
        return _decode_object(path.read_bytes(), source=path)
    except OSError as exc:
        raise RedactionError(f"cannot read required bundle file: {path}") from exc


def _load_events(path: Path) -> list[JsonObject]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RedactionError(f"cannot read required bundle file: {path}") from exc
    events: list[JsonObject] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line:
            raise RedactionError(f"{path}:{line_number}: blank JSONL line")
        events.append(_decode_object(line, source=path))
    return events


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise RedactionError(f"{field} must be an object")
    return cast(JsonObject, value)


def _as_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RedactionError(f"{field} must be a string")
    return value


def _as_int(value: object, field: str) -> int:
    if not _is_int(value) or value < 0:
        raise RedactionError(f"{field} must be a non-negative integer")
    return value


def _require_complete(bundle_dir: Path) -> str:
    try:
        from recorder.verify import verify_bundle
    except ImportError as exc:
        raise RedactionError("bundle verifier is unavailable") from exc

    receipt = verify_bundle(bundle_dir)
    if not receipt.is_complete:
        raise RedactionError(
            f"share requires a COMPLETE parent bundle; "
            f"verifier returned {receipt.verdict}: {receipt.message}"
        )
    root = receipt.root
    if not isinstance(root, str):
        raise RedactionError("complete bundle verification returned no root")
    return root


def _raw_commitments(bundle_dir: Path) -> list[JsonObject]:
    commitments: dict[str, JsonObject] = {}
    for event in _load_events(bundle_dir / "events.jsonl"):
        if event.get("type") != "AgentResponded":
            continue
        turn = event.get("turn")
        if not _is_int(turn) or turn < 0:
            raise RedactionError("AgentResponded.turn must be non-negative")
        payload = _as_object(event.get("payload"), "AgentResponded.payload")
        attempt = _as_int(payload.get("attempt"), "AgentResponded.attempt")
        if attempt not in (1, 2):
            raise RedactionError(
                f"AgentResponded turn {turn} has invalid attempt {attempt}"
            )
        path = _as_str(payload.get("raw_ref"), "AgentResponded.raw_ref")
        expected = f"raw/{turn:04d}-a{attempt}.txt"
        if path != expected:
            raise RedactionError(
                f"AgentResponded raw_ref must be {expected!r}, found {path!r}"
            )
        if path in commitments:
            raise RedactionError(f"duplicate raw commitment: {path}")
        candidate = bundle_dir / path
        if candidate.is_symlink():
            raise RedactionError(f"raw blob must not be a symlink: {path}")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise RedactionError(f"committed raw blob is missing: {path}") from exc
        if not resolved.is_relative_to(bundle_dir.resolve()):
            raise RedactionError(f"raw blob escapes bundle: {path}")
        raw = resolved.read_bytes()
        expected_hash = _as_str(
            payload.get("raw_sha256"),
            "AgentResponded.raw_sha256",
        )
        expected_bytes = _as_int(
            payload.get("raw_bytes"),
            "AgentResponded.raw_bytes",
        )
        actual_hash = sha256_prefixed(raw)
        if actual_hash != expected_hash or len(raw) != expected_bytes:
            raise RedactionError(f"raw commitment mismatch: {path}")
        commitments[path] = {
            "path": path,
            "sha256": actual_hash,
            "bytes": len(raw),
            "reason": "raw_model_text",
        }

    raw_dir = bundle_dir / "raw"
    actual_paths = {
        path.relative_to(bundle_dir).as_posix()
        for path in raw_dir.rglob("*")
        if path.is_file()
    }
    unexpected = actual_paths - commitments.keys()
    missing = commitments.keys() - actual_paths
    if unexpected:
        raise RedactionError(
            "uncommitted raw files would leak from share export: "
            + ", ".join(sorted(unexpected))
        )
    if missing:
        raise RedactionError(
            "committed raw files are missing: " + ", ".join(sorted(missing))
        )
    if not commitments:
        raise RedactionError(
            "share profile requires at least one raw model response to redact"
        )
    return [commitments[path] for path in sorted(commitments)]


def _paths_overlap(parent: Path, destination: Path) -> bool:
    parent_resolved = parent.resolve()
    destination_resolved = destination.resolve()
    return destination_resolved.is_relative_to(
        parent_resolved
    ) or parent_resolved.is_relative_to(destination_resolved)


def create_share_bundle(
    parent_bundle: str | Path,
    destination: str | Path,
) -> ShareBundleResult:
    """Copy a complete bundle and redact only committed raw model text.

    The original ``chain.json`` is copied byte-for-byte and therefore retains
    the parent's root.  ``redaction.json`` discloses every permitted absence;
    it is deliberately outside the parent seal.
    """

    parent = Path(parent_bundle)
    target = Path(destination)
    if not parent.is_dir():
        raise RedactionError(f"parent bundle is not a directory: {parent}")
    if target.exists():
        raise RedactionError(f"share destination already exists: {target}")
    if _paths_overlap(parent, target):
        raise RedactionError(
            "share destination must not contain or be contained by parent bundle"
        )

    parent_root = _require_complete(parent)
    manifest = _load_object(parent / "manifest.json")
    run_id = _as_str(manifest.get("run_id"), "manifest.run_id")
    seal = _load_object(parent / "chain.json")
    seal_root = _as_str(seal.get("root"), "chain.root")
    if seal_root != parent_root:
        raise RedactionError(
            f"verifier root {parent_root} disagrees with chain.root {seal_root}"
        )
    removals = _raw_commitments(parent)

    # Preserve any entry that changes after parent verification as a symlink
    # instead of following it into material outside the verified bundle.  The
    # final verification below then rejects that symlink deterministically.
    shutil.copytree(parent, target, symlinks=True)
    for removal in removals:
        relative = cast(str, removal["path"])
        copied_blob = target / relative
        if copied_blob.is_symlink() or not copied_blob.is_file():
            raise RedactionError(
                f"copied raw blob is not a regular file: {relative}"
            )
        copied_blob.unlink()

    redaction: JsonObject = {
        "schema": "redaction/v1",
        "run_id": run_id,
        "profile": "share",
        "parent_root": parent_root,
        "removals": removals,
    }
    (target / "redaction.json").write_bytes(canonical_bytes(redaction) + b"\n")

    share_root = _require_complete(target)
    if share_root != parent_root:
        raise RedactionError(
            f"share verification changed root: parent={parent_root}, "
            f"share={share_root}"
        )
    return ShareBundleResult(
        path=target,
        run_id=run_id,
        parent_root=parent_root,
        removals=len(removals),
    )
