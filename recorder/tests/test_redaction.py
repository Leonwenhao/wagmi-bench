# SPDX-License-Identifier: Apache-2.0
"""C3.4 share-profile redaction preserves the parent's evidence root."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from core.config import EpisodeConfig
from core.engine import run_episode
from harness.scripted import ScriptedFixtureAgent
from recorder.redaction import RedactionError, create_share_bundle
from recorder.replay import replay_bundle
from recorder.verify import verify_bundle
from recorder.writer import build_bundle_manifest, record_episode_bundle

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "fixtures" / "golden-mini"
REDACTION_SCHEMA = json.loads(
    (ROOT / "spec" / "schemas" / "redaction.v1.schema.json").read_text(
        encoding="utf-8"
    )
)


def _record_parent(path: Path) -> None:
    raw = json.loads(
        (GOLDEN / "episode_config.json").read_text(encoding="utf-8")
    )
    result = run_episode(
        pack_dir=GOLDEN / "pack",
        agent=ScriptedFixtureAgent.from_path(GOLDEN / "actions.jsonl"),
        config=EpisodeConfig.from_mapping(cast(dict[str, object], raw)),
        run_id="run_3333333333333333",
        episode_id="ep_3333333333333333",
    )
    agent_manifest: dict[str, object] = {
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
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=1_700_000_000_000,
        host={"os": "test", "arch": "test", "python": "3.12"},
    )
    record_episode_bundle(
        path,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )


def test_share_deletes_only_raw_blobs_and_retains_complete_parent_root(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    share = tmp_path / "share"
    _record_parent(parent)
    parent_receipt = verify_bundle(parent)
    parent_raw = sorted((parent / "raw").glob("*.txt"))
    assert parent_receipt.is_complete
    assert parent_raw

    result = create_share_bundle(parent, share)

    assert result.parent_root == parent_receipt.root
    assert result.removals == len(parent_raw)
    assert all(path.is_file() for path in parent_raw)
    assert list((share / "raw").glob("*.txt")) == []

    redaction = json.loads(
        (share / "redaction.json").read_text(encoding="utf-8")
    )
    errors = list(Draft202012Validator(REDACTION_SCHEMA).iter_errors(redaction))
    assert not errors, [error.message for error in errors]
    assert redaction["parent_root"] == parent_receipt.root
    assert [row["path"] for row in redaction["removals"]] == [
        path.relative_to(parent).as_posix() for path in parent_raw
    ]

    for source in sorted(path for path in parent.rglob("*") if path.is_file()):
        relative = source.relative_to(parent)
        if relative.parts[0] == "raw":
            continue
        assert (share / relative).read_bytes() == source.read_bytes()

    share_receipt = verify_bundle(share)
    assert share_receipt.is_complete
    assert share_receipt.root == parent_receipt.root
    replay = replay_bundle(share, pack_dir=GOLDEN / "pack")
    assert replay.bundle_root == parent_receipt.root


def test_share_refuses_corrupt_or_truncated_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    _record_parent(parent)
    (parent / "chain.json").unlink()

    with pytest.raises(
        RedactionError,
        match=r"requires a COMPLETE parent bundle.*TRUNCATED",
    ):
        create_share_bundle(parent, tmp_path / "share")


def test_share_refuses_destination_inside_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    _record_parent(parent)

    with pytest.raises(RedactionError, match="must not contain"):
        create_share_bundle(parent, parent / "share")
