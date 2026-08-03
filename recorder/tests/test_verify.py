# SPDX-License-Identifier: Apache-2.0
"""Adversarial C3.1/C3.2 tests for stored-byte bundle verification."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import signal
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from core.config import EpisodeConfig
from core.engine import EpisodeResult, run_episode
from harness.scripted import ScriptedFixtureAgent
from recorder.verify import verify_bundle
from recorder.writer import (
    BundleWriter,
    build_bundle_manifest,
    record_episode_bundle,
)
from spec.canonical import (
    ChainBuilder,
    canonical_bytes,
    chain_genesis,
    run_config_sha256,
    seal_root,
    sha256_prefixed,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "fixtures" / "golden-mini"

AGENT_MANIFEST: dict[str, object] = {
    "schema": "agent_manifest/v1",
    "name": "golden-scripted-agent",
    "adapter": "in_process",
    "model_id": "none",
    "endpoint_domains": [],
    "inference_params": {},
    "prompt_sha256": None,
    "agent_version": "fixture-v1",
    "image_sha256": None,
}
HOST: dict[str, object] = {
    "os": "test",
    "arch": "test",
    "python": "3.12",
}


def _config() -> EpisodeConfig:
    value = json.loads(
        (GOLDEN / "episode_config.json").read_text(encoding="utf-8")
    )
    return EpisodeConfig.from_mapping(cast(dict[str, object], value))


def _run() -> EpisodeResult:
    return run_episode(
        pack_dir=GOLDEN / "pack",
        agent=ScriptedFixtureAgent.from_path(GOLDEN / "actions.jsonl"),
        config=_config(),
        run_id="run_00000000000000a1",
        episode_id="ep_0123456789abcdef",
    )


def _manifest(result: EpisodeResult) -> dict[str, object]:
    return build_bundle_manifest(
        result,
        AGENT_MANIFEST,
        created_at_ms=1_784_937_600_000,
        host=HOST,
    )


@pytest.fixture(scope="module")
def pristine_bundle(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    result = _run()
    path = tmp_path_factory.mktemp("recorder-verify") / "bundle"
    record_episode_bundle(
        path,
        result=result,
        manifest=_manifest(result),
        agent_manifest=AGENT_MANIFEST,
    )
    verification = verify_bundle(path)
    assert verification.verdict == "COMPLETE", verification.message
    yield path


@pytest.fixture
def bundle(tmp_path: Path, pristine_bundle: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(pristine_bundle, target)
    return target


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _jsonl(path: Path) -> list[dict[str, object]]:
    values = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(isinstance(value, dict) for value in values)
    return cast(list[dict[str, object]], values)


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_bytes(
        b"".join(canonical_bytes(value) + b"\n" for value in values)
    )


def _flip_first_timestamp_digit(path: Path) -> None:
    raw = bytearray(path.read_bytes())
    position = raw.index(b'"ts":') + len(b'"ts":')
    original = raw[position]
    assert chr(original).isdigit()
    raw[position] = ord("8") if original != ord("8") else ord("7")
    path.write_bytes(bytes(raw))


def test_one_byte_event_tamper_names_first_bad_record(bundle: Path) -> None:
    _flip_first_timestamp_digit(bundle / "events.jsonl")

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.stream == "events"
    assert result.seq == 0
    assert result.path == "events.jsonl"
    assert "record hash mismatch" in result.message


def test_one_byte_decision_tamper_names_decision_file(bundle: Path) -> None:
    _flip_first_timestamp_digit(bundle / "decisions" / "0000.json")

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.stream == "decisions"
    assert result.seq == 0
    assert result.path == "decisions/0000.json"


def test_raw_blob_tamper_names_blob(bundle: Path) -> None:
    raw_path = next((bundle / "raw").iterdir())
    raw = bytearray(raw_path.read_bytes())
    raw[0] = (raw[0] + 1) % 128
    raw_path.write_bytes(bytes(raw))

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.path == f"raw/{raw_path.name}"
    assert "raw hash mismatch" in result.message


def test_missing_seal_is_valid_but_truncated_prefix(bundle: Path) -> None:
    (bundle / "chain.json").unlink()

    result = verify_bundle(bundle)
    assert result.verdict == "TRUNCATED", result.message
    assert result.root is None
    assert result.last_good["events"] >= 0
    assert result.last_good["decisions"] >= 0
    assert len(result.turns) == result.inventory["decisions"]


def test_dangling_seal_symlink_is_corrupt_not_truncated(
    bundle: Path,
) -> None:
    (bundle / "chain.json").unlink()
    (bundle / "chain.json").symlink_to("missing-seal.json")

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.path == "chain.json"
    assert "symlink" in result.message


def test_sealed_rollback_of_record_and_link_is_corrupt(bundle: Path) -> None:
    events = _jsonl(bundle / "events.jsonl")
    assert events[-1]["type"] == "EpisodeEnd"
    _write_jsonl(bundle / "events.jsonl", events[:-1])

    links = _jsonl(bundle / "chain.jsonl")
    event_link_indexes = [
        index
        for index, link in enumerate(links)
        if link["stream"] == "events"
    ]
    del links[event_link_indexes[-1]]
    _write_jsonl(bundle / "chain.jsonl", links)

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.stream == "events"
    assert result.seq == len(events) - 1
    assert "sealed stream_head.count" in result.message


def test_sealed_unlinked_tail_is_corrupt(bundle: Path) -> None:
    links = _jsonl(bundle / "chain.jsonl")
    decision_link_indexes = [
        index
        for index, link in enumerate(links)
        if link["stream"] == "decisions"
    ]
    del links[decision_link_indexes[-1]]
    _write_jsonl(bundle / "chain.jsonl", links)

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.stream == "decisions"
    assert result.path is not None
    assert result.path.startswith("decisions/")


def test_share_redaction_is_complete_with_disclosure(bundle: Path) -> None:
    parent = verify_bundle(bundle)
    assert parent.is_complete
    events = _jsonl(bundle / "events.jsonl")
    response = next(
        cast(dict[str, object], event["payload"])
        for event in events
        if event["type"] == "AgentResponded"
    )
    relative = cast(str, response["raw_ref"])
    raw_path = bundle / relative
    raw_path.unlink()
    redaction: dict[str, object] = {
        "schema": "redaction/v1",
        "run_id": "run_00000000000000a1",
        "profile": "share",
        "parent_root": parent.root,
        "removals": [
            {
                "path": relative,
                "sha256": response["raw_sha256"],
                "bytes": response["raw_bytes"],
                "reason": "raw_model_text",
            }
        ],
    }
    (bundle / "redaction.json").write_bytes(
        canonical_bytes(redaction) + b"\n"
    )

    shared = verify_bundle(bundle)
    assert shared.verdict == "COMPLETE", shared.message
    assert shared.root == parent.root
    assert shared.inventory["raw_disclosed"] == 1
    assert "absent and disclosed" in shared.message


def test_undisclosed_missing_raw_is_corrupt(bundle: Path) -> None:
    raw_path = next((bundle / "raw").iterdir())
    relative = f"raw/{raw_path.name}"
    raw_path.unlink()

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.path == relative
    assert "missing and undisclosed" in result.message


def test_symlinked_blob_store_is_corrupt(bundle: Path) -> None:
    original = bundle / "raw"
    moved = bundle / "raw-real"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.path == "raw"
    assert "symlink" in result.message


def test_extra_top_level_material_cannot_ride_in_complete_bundle(
    bundle: Path,
) -> None:
    (bundle / "secret.env").write_text("credential=not-allowed\n")

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.path == "secret.env"
    assert "top-level layout mismatch" in result.message


def test_seal_files_map_is_exact(bundle: Path) -> None:
    seal = _load(bundle / "chain.json")
    files = cast(dict[str, object], seal["files"])
    files["events.jsonl"] = sha256_prefixed(
        (bundle / "events.jsonl").read_bytes()
    )
    seal["root"] = seal_root(seal)
    (bundle / "chain.json").write_bytes(canonical_bytes(seal))

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.path == "chain.json"
    assert "files coverage mismatch" in result.message


def test_seal_genesis_must_match_manifest_bound_identity(
    bundle: Path,
) -> None:
    seal = _load(bundle / "chain.json")
    streams = cast(dict[str, object], seal["streams"])
    event_head = cast(dict[str, object], streams["events"])
    event_head["genesis"] = "sha256:" + "0" * 64
    seal["root"] = seal_root(seal)
    (bundle / "chain.json").write_bytes(canonical_bytes(seal))

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.stream == "events"
    assert result.path == "chain.json"
    assert "genesis" in result.message


def test_hidden_symlink_inside_blob_store_is_corrupt(
    bundle: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.env"
    outside.write_text("credential=must-not-copy\n")
    (bundle / "observations" / ".env").symlink_to(outside)

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.path == "observations/.env"
    assert "store/reference mismatch" in result.message


def test_reforged_decision_chain_still_fails_event_regeneration(
    bundle: Path,
) -> None:
    decisions = [
        _load(path)
        for path in sorted((bundle / "decisions").glob("*.json"))
    ]
    account = cast(dict[str, object], decisions[0]["account_after"])
    account["nav_micro"] = cast(int, account["nav_micro"]) + 1
    for index, decision in enumerate(decisions):
        (bundle / "decisions" / f"{index:04d}.json").write_bytes(
            canonical_bytes(decision)
        )

    manifest = _load(bundle / "manifest.json")
    pack = cast(dict[str, object], manifest["pack"])
    agent_hash = cast(str, manifest["agent_manifest_sha256"])
    config = cast(dict[str, object], manifest["run_config"])
    genesis = chain_genesis(
        "decisions",
        run_id=cast(str, manifest["run_id"]),
        pack_content_hash=cast(str, pack["content_hash"]),
        agent_manifest_sha256=agent_hash,
        run_config_sha256=run_config_sha256(config),
    )
    builder = ChainBuilder("decisions", genesis)
    new_decision_links = [
        builder.append(canonical_bytes(decision))
        for decision in decisions
    ]
    old_links = _jsonl(bundle / "chain.jsonl")
    event_links = [
        link for link in old_links if link["stream"] == "events"
    ]
    new_chain = event_links + cast(
        list[dict[str, object]],
        new_decision_links,
    )
    _write_jsonl(bundle / "chain.jsonl", new_chain)

    seal = _load(bundle / "chain.json")
    streams = cast(dict[str, object], seal["streams"])
    streams["decisions"] = builder.stream_head()
    files = cast(dict[str, object], seal["files"])
    files["chain.jsonl"] = sha256_prefixed(
        (bundle / "chain.jsonl").read_bytes()
    )
    seal["root"] = seal_root(seal)
    (bundle / "chain.json").write_bytes(canonical_bytes(seal))

    result = verify_bundle(bundle)
    assert result.verdict == "CORRUPT"
    assert result.stream == "decisions"
    assert result.seq == 0
    assert result.path == "decisions/0000.json"
    assert "canonical event-derived view" in result.message


def _sigkill_worker(bundle: str) -> None:
    result = _run()
    writer = BundleWriter.create(
        bundle,
        manifest=_manifest(result),
        agent_manifest=AGENT_MANIFEST,
    )
    for turn, observation in enumerate(result.observations):
        writer.write_observation(turn, observation)
    for relative, raw in result.raw_blobs.items():
        writer.write_raw(relative, raw)
    for event in result.events:
        writer.append_event(event)
        time.sleep(0.02)


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="requires POSIX SIGKILL",
)
def test_actual_sigkill_leaves_verifiable_truncated_prefix(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "killed"
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_sigkill_worker, args=(str(bundle),))
    process.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        chain = bundle / "chain.jsonl"
        if chain.exists() and chain.read_bytes().count(b"\n") >= 3:
            break
        time.sleep(0.01)
    else:
        process.kill()
        process.join(timeout=5)
        pytest.fail("worker did not durably append three links")

    pid = process.pid
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    process.join(timeout=5)
    assert process.exitcode == -signal.SIGKILL

    result = verify_bundle(bundle)
    assert result.verdict == "TRUNCATED", result.message
    assert result.last_good["events"] >= 2
    assert result.root is None
