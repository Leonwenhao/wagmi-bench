# SPDX-License-Identifier: Apache-2.0
"""Sealed golden-bundle fixtures for report tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from core.config import EpisodeConfig
from core.engine import run_episode
from harness.scripted import (
    ConstantTargetAgent,
    MomentumAgent,
    ScriptedFixtureAgent,
)
from recorder.writer import build_bundle_manifest, record_episode_bundle

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


def _record(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    label: str,
    agent: object,
    agent_manifest: dict[str, object],
    run_id: str,
    episode_id: str,
) -> Path:
    result = run_episode(
        pack_dir=GOLDEN / "pack",
        agent=agent,  # type: ignore[arg-type]
        config=_config(),
        run_id=run_id,
        episode_id=episode_id,
    )
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=1_784_937_600_000,
        host=HOST,
    )
    bundle = tmp_path_factory.mktemp(label) / "bundle"
    record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )
    return bundle


def _baseline_manifest(kind: str, target_lev_1e4: int) -> dict[str, object]:
    return {
        **AGENT_MANIFEST,
        "name": f"{kind}-baseline",
        "inference_params": {
            "policy": "constant-target",
            "target_lev_1e4": target_lev_1e4,
        },
    }


@pytest.fixture(scope="session")
def momentum_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _record(
        tmp_path_factory,
        label="report-momentum",
        agent=MomentumAgent(),
        agent_manifest={**AGENT_MANIFEST, "name": "momentum-candidate"},
        run_id="run_7777777777777777",
        episode_id="ep_7777777777777777",
    )


@pytest.fixture(scope="session")
def buyhold_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _record(
        tmp_path_factory,
        label="report-buyhold",
        agent=ConstantTargetAgent(target_lev_1e4=10_000),
        agent_manifest=_baseline_manifest("buyhold", 10_000),
        run_id="run_8888888888888888",
        episode_id="ep_8888888888888888",
    )


@pytest.fixture(scope="session")
def flat_baseline_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _record(
        tmp_path_factory,
        label="report-flat-baseline",
        agent=ConstantTargetAgent(target_lev_1e4=0),
        agent_manifest=_baseline_manifest("flat", 0),
        run_id="run_9999999999999999",
        episode_id="ep_9999999999999999",
    )


@pytest.fixture(scope="session")
def flat_hold_bundle(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    result = run_episode(
        pack_dir=GOLDEN / "pack",
        agent=ConstantTargetAgent(target_lev_1e4=0),
        config=_config(),
        run_id="run_6666666666666666",
        episode_id="ep_6666666666666666",
    )
    manifest = build_bundle_manifest(
        result,
        AGENT_MANIFEST,
        created_at_ms=1_784_937_600_000,
        host=HOST,
    )
    bundle = tmp_path_factory.mktemp("report-flat") / "bundle"
    record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=AGENT_MANIFEST,
    )
    return bundle


@pytest.fixture(scope="session")
def golden_bundle(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    result = run_episode(
        pack_dir=GOLDEN / "pack",
        agent=ScriptedFixtureAgent.from_path(GOLDEN / "actions.jsonl"),
        config=_config(),
        run_id="run_5555555555555555",
        episode_id="ep_5555555555555555",
    )
    manifest = build_bundle_manifest(
        result,
        AGENT_MANIFEST,
        created_at_ms=1_784_937_600_000,
        host=HOST,
    )
    bundle = tmp_path_factory.mktemp("report-golden") / "bundle"
    record_episode_bundle(
        bundle,
        result=result,
        manifest=manifest,
        agent_manifest=AGENT_MANIFEST,
    )
    return bundle
