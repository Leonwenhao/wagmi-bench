# SPDX-License-Identifier: Apache-2.0
"""Focused C3.1 tests for immutable, incremental evidence recording."""

from __future__ import annotations

import json
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
    RecorderError,
    build_bundle_manifest,
    record_episode_bundle,
)
from spec.canonical import canonical_bytes, sha256_prefixed

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


@pytest.fixture(scope="module")
def result() -> Iterator[EpisodeResult]:
    yield run_episode(
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


def test_record_episode_bundle_writes_exact_complete_bundle(
    tmp_path: Path,
    result: EpisodeResult,
) -> None:
    bundle = tmp_path / "bundle-run"
    seal = record_episode_bundle(
        bundle,
        result=result,
        manifest=_manifest(result),
        agent_manifest=AGENT_MANIFEST,
    )

    verification = verify_bundle(bundle)
    assert verification.verdict == "COMPLETE", verification.message
    assert verification.is_complete
    assert verification.root == seal["root"]
    assert verification.inventory["events"] == len(result.events)
    assert verification.inventory["decisions"] == len(result.observations)
    assert verification.inventory["observations"] == len(
        result.observations
    )
    assert verification.inventory["raw"] == len(result.raw_blobs)

    assert not (bundle / "manifest.json").read_bytes().endswith(b"\n")
    assert not (bundle / "agent_manifest.json").read_bytes().endswith(b"\n")
    assert not (bundle / "observations" / "0000.json").read_bytes().endswith(
        b"\n"
    )
    assert not (bundle / "decisions" / "0000.json").read_bytes().endswith(
        b"\n"
    )
    assert (bundle / "metrics.json").read_bytes().endswith(b"\n")
    assert (bundle / "events.jsonl").read_bytes().endswith(b"\n")
    assert (bundle / "chain.jsonl").read_bytes().endswith(b"\n")

    final_payload = cast(
        dict[str, object],
        result.events[-1]["payload"],
    )
    assert final_payload["metrics_sha256"] == sha256_prefixed(
        (bundle / "metrics.json").read_bytes()
    )
    stored_manifest = (bundle / "manifest.json").read_bytes()
    assert stored_manifest == canonical_bytes(_manifest(result))


def test_writer_refuses_to_mutate_existing_bundle(
    tmp_path: Path,
    result: EpisodeResult,
) -> None:
    bundle = tmp_path / "immutable"
    writer = BundleWriter.create(
        bundle,
        manifest=_manifest(result),
        agent_manifest=AGENT_MANIFEST,
    )
    manifest_before = (bundle / "manifest.json").read_bytes()

    with pytest.raises(RecorderError, match="already exists"):
        BundleWriter.create(
            bundle,
            manifest=_manifest(result),
            agent_manifest=AGENT_MANIFEST,
        )
    assert (bundle / "manifest.json").read_bytes() == manifest_before

    writer.write_observation(0, result.observations[0])
    with pytest.raises(RecorderError, match="already written"):
        writer.write_observation(0, result.observations[0])
    assert (bundle / "manifest.json").read_bytes() == manifest_before


def test_unsealed_incremental_prefix_is_truncated_not_complete(
    tmp_path: Path,
    result: EpisodeResult,
) -> None:
    bundle = tmp_path / "prefix"
    writer = BundleWriter.create(
        bundle,
        manifest=_manifest(result),
        agent_manifest=AGENT_MANIFEST,
    )
    # Blobs may be durably installed ahead of the event prefix.  They remain
    # inventory, but only chain.jsonl-committed events are claimed verified.
    for turn, observation in enumerate(result.observations):
        writer.write_observation(turn, observation)
    for relative, raw in result.raw_blobs.items():
        writer.write_raw(relative, raw)
    for event in result.events[:7]:
        writer.append_event(event)

    verification = verify_bundle(bundle)
    assert verification.verdict == "TRUNCATED", verification.message
    assert not verification.is_complete
    assert verification.root is None
    assert verification.last_good["events"] == 6
    assert verification.inventory["events"] == 7


def test_finalize_requires_episode_end_and_all_products(
    tmp_path: Path,
    result: EpisodeResult,
) -> None:
    writer = BundleWriter.create(
        tmp_path / "unfinished",
        manifest=_manifest(result),
        agent_manifest=AGENT_MANIFEST,
    )
    writer.write_observation(0, result.observations[0])
    writer.append_event(result.events[0])
    with pytest.raises(RecorderError, match="EpisodeEnd"):
        writer.finalize()
