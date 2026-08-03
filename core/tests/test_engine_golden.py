# SPDX-License-Identifier: Apache-2.0
"""M1 exit oracle: the engine is written to the frozen golden bytes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from core.config import EpisodeConfig
from core.engine import EpisodeResult, run_episode
from harness.scripted import ScriptedFixtureAgent
from spec.canonical import canonical_bytes

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "fixtures" / "golden-mini"


def _config() -> EpisodeConfig:
    raw = json.loads((GOLDEN / "episode_config.json").read_text(encoding="utf-8"))
    return EpisodeConfig.from_mapping(cast(dict[str, object], raw))


def _canonical_jsonl(rows: list[dict[str, object]]) -> bytes:
    return b"".join(canonical_bytes(row) + b"\n" for row in rows)


@pytest.mark.parametrize(
    ("episode", "pack_dir", "actions_path", "run_id"),
    [
        (
            "main",
            GOLDEN / "pack",
            GOLDEN / "actions.jsonl",
            "run_00000000000000a1",
        ),
        (
            "variant-liquidation",
            GOLDEN / "variant-liquidation" / "pack",
            GOLDEN / "variant-liquidation" / "actions.jsonl",
            "run_00000000000000b1",
        ),
    ],
)
def test_engine_reproduces_frozen_golden_oracle(
    episode: str,
    pack_dir: Path,
    actions_path: Path,
    run_id: str,
) -> None:
    result: EpisodeResult = run_episode(
        pack_dir=pack_dir,
        agent=ScriptedFixtureAgent.from_path(actions_path),
        config=_config(),
        run_id=run_id,
        episode_id="ep_0123456789abcdef",
    )
    expected = GOLDEN / "expected" / episode

    assert _canonical_jsonl(result.golden_event_projection()) == (
        expected / "events.jsonl"
    ).read_bytes()
    assert _canonical_jsonl(result.ledger_primary) == (
        expected / "ledger.jsonl"
    ).read_bytes()
    assert _canonical_jsonl(result.ledger_stress_2x) == (
        expected / "ledger_stress_2x.jsonl"
    ).read_bytes()
    assert canonical_bytes(result.metrics) + b"\n" == (
        expected / "metrics.json"
    ).read_bytes()


def test_engine_full_event_stream_is_deterministic_and_complete() -> None:
    first = run_episode(
        pack_dir=GOLDEN / "pack",
        agent=ScriptedFixtureAgent.from_path(GOLDEN / "actions.jsonl"),
        config=_config(),
        run_id="run_00000000000000a1",
        episode_id="ep_0123456789abcdef",
    )
    second = run_episode(
        pack_dir=GOLDEN / "pack",
        agent=ScriptedFixtureAgent.from_path(GOLDEN / "actions.jsonl"),
        config=_config(),
        run_id="run_00000000000000a1",
        episode_id="ep_0123456789abcdef",
    )
    assert _canonical_jsonl(first.events) == _canonical_jsonl(second.events)
    assert first.events[-1]["type"] == "EpisodeEnd"
    assert [event["seq"] for event in first.events] == list(range(len(first.events)))
