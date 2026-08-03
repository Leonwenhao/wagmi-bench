# SPDX-License-Identifier: Apache-2.0
"""C3.3 exact economic replay over sealed IC-5 bundles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from core.config import EpisodeConfig
from core.engine import EpisodeResult, run_episode
from harness.protocol import AgentReply, HarnessEvent
from harness.scripted import ScriptedFixtureAgent
from recorder.replay import (
    ReplayCompatibilityError,
    ReplayError,
    ReplayResult,
    replay_bundle,
)
from recorder.writer import build_bundle_manifest, record_episode_bundle

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "fixtures" / "golden-mini"
RUN_ID = "run_2222222222222222"
EPISODE_ID = "ep_2222222222222222"


def _agent_manifest() -> dict[str, object]:
    return {
        "schema": "agent_manifest/v1",
        "name": "golden-scripted",
        "adapter": "in_process",
        "model_id": "none",
        "endpoint_domains": [],
        "inference_params": {},
        "prompt_sha256": None,
        "agent_version": "test",
        "image_sha256": None,
    }


def _result() -> EpisodeResult:
    raw = json.loads(
        (GOLDEN / "episode_config.json").read_text(encoding="utf-8")
    )
    config = EpisodeConfig.from_mapping(cast(dict[str, object], raw))
    return run_episode(
        pack_dir=GOLDEN / "pack",
        agent=ScriptedFixtureAgent.from_path(GOLDEN / "actions.jsonl"),
        config=config,
        run_id=RUN_ID,
        episode_id=EPISODE_ID,
    )


def _record(
    bundle: Path,
    *,
    manifest_update: dict[str, object] | None = None,
) -> EpisodeResult:
    result = _result()
    agent_manifest = _agent_manifest()
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=1_700_000_000_000,
        host={"os": "test", "arch": "test", "python": "3.12"},
    )
    if manifest_update:
        manifest.update(manifest_update)
    record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )
    return result


class _AlwaysErrorsAgent:
    def decide(self, request: dict[str, object]) -> AgentReply:
        del request
        raise RuntimeError("recorded transport failure")


class _EgressTraceAgent:
    def __init__(self) -> None:
        self._pending: tuple[HarnessEvent, ...] = ()
        self._attempt_drain_due = False
        self._final_sent = False

    def decide(self, request: dict[str, object]) -> AgentReply:
        observation = cast(dict[str, object], request["observation"])
        episode = cast(dict[str, object], observation["episode"])
        turn = cast(int, episode["turn"])
        self._attempt_drain_due = True
        self._pending = (
            HarnessEvent(
                type="EgressBlocked",
                payload={
                    "destination": f"turn-{turn:04d}-sha256:aaaa",
                    "port": 443,
                    "protocol": "https",
                    "count": 2,
                },
            ),
        )
        return AgentReply(
            b'{"schema":"action/v1","target":{"BTC":"0"}}'
        )

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        if self._attempt_drain_due:
            self._attempt_drain_due = False
            pending = self._pending
            self._pending = ()
            return pending
        if self._final_sent:
            return ()
        self._final_sent = True
        return (
            HarnessEvent(
                type="EgressBlocked",
                payload={
                    "destination": "final-sha256:bbbb",
                    "port": None,
                    "protocol": "dns",
                    "count": 5,
                },
            ),
        )


def test_replay_is_offline_and_byte_identical_for_all_derived_artifacts(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    result = _record(bundle)

    receipt: ReplayResult = replay_bundle(
        bundle,
        pack_dir=GOLDEN / "pack",
    )

    assert receipt.run_id == RUN_ID
    assert receipt.bundle_root.startswith("sha256:")
    assert receipt.files_compared == (
        "ledger.jsonl",
        "ledger_stress_2x.jsonl",
        "metrics.json",
    )
    assert receipt.decisions_compared == len(result.observations)


def test_replay_reinjects_turn_and_final_egress_counts_exactly(
    tmp_path: Path,
) -> None:
    result = run_episode(
        pack_dir=GOLDEN / "pack",
        agent=_EgressTraceAgent(),
        config=EpisodeConfig(),
        run_id=RUN_ID,
        episode_id=EPISODE_ID,
    )
    invariant = cast(
        dict[str, object],
        result.metrics["profile_invariant"],
    )
    assert invariant["egress_blocked_count"] == 31
    agent_manifest = _agent_manifest()
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=1_700_000_000_000,
        host={"os": "test", "arch": "test", "python": "3.12"},
    )
    bundle = tmp_path / "egress"
    record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )

    receipt = replay_bundle(bundle, pack_dir=GOLDEN / "pack")

    assert receipt.run_id == RUN_ID
    assert receipt.decisions_compared == len(result.observations)


def test_replay_reconstructs_recorded_agent_errors_without_agent_contact(
    tmp_path: Path,
) -> None:
    result = run_episode(
        pack_dir=GOLDEN / "pack",
        agent=_AlwaysErrorsAgent(),
        config=EpisodeConfig(),
        run_id=RUN_ID,
        episode_id=EPISODE_ID,
    )
    agent_manifest = _agent_manifest()
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=1_700_000_000_000,
        host={"os": "test", "arch": "test", "python": "3.12"},
    )
    bundle = tmp_path / "agent-errors"
    record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )

    receipt = replay_bundle(bundle, pack_dir=GOLDEN / "pack")

    assert receipt.decisions_compared == len(result.observations)


def test_replay_refuses_engine_version_mismatch_without_override(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "wrong-engine"
    _record(bundle, manifest_update={"engine_version": "9.9.9"})

    with pytest.raises(
        ReplayCompatibilityError,
        match=r"engine_version mismatch.*9\.9\.9.*0\.1\.0",
    ):
        replay_bundle(bundle, pack_dir=GOLDEN / "pack")


def test_replay_refuses_exact_spec_registry_mismatch(
    tmp_path: Path,
) -> None:
    result = _result()
    agent_manifest = _agent_manifest()
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=1_700_000_000_000,
        host={"os": "test", "arch": "test", "python": "3.12"},
    )
    versions = cast(dict[str, object], manifest["spec_versions"])
    versions["event/v1"] = "1.0.1"
    bundle = tmp_path / "wrong-spec"
    record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )

    with pytest.raises(
        ReplayCompatibilityError,
        match="spec_versions mismatch",
    ):
        replay_bundle(bundle, pack_dir=GOLDEN / "pack")


def test_replay_refuses_pack_manifest_hash_mismatch(
    tmp_path: Path,
) -> None:
    result = _result()
    agent_manifest = _agent_manifest()
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=1_700_000_000_000,
        host={"os": "test", "arch": "test", "python": "3.12"},
    )
    pack_ref = cast(dict[str, object], manifest["pack"])
    pack_ref["manifest_sha256"] = "sha256:" + "0" * 64
    bundle = tmp_path / "wrong-pack"
    record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )

    with pytest.raises(
        ReplayCompatibilityError,
        match="pack.manifest_sha256 mismatch",
    ):
        replay_bundle(bundle, pack_dir=GOLDEN / "pack")


def test_replay_refuses_tampered_bundle_before_economic_execution(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "tampered"
    _record(bundle)
    ledger = bundle / "ledger.jsonl"
    raw = bytearray(ledger.read_bytes())
    raw[0] = ord("[")
    ledger.write_bytes(bytes(raw))

    with pytest.raises(
        ReplayError,
        match=r"requires a COMPLETE bundle.*CORRUPT",
    ):
        replay_bundle(bundle, pack_dir=GOLDEN / "pack")
