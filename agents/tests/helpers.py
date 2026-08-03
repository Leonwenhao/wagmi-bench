# SPDX-License-Identifier: Apache-2.0
"""Shared frozen-contract fixtures for reference-agent tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[2]
OBSERVATION_PATH = (
    ROOT / "spec" / "schemas" / "examples" / "observation.v1.json"
)


def runner_request(
    *,
    turn: int = 0,
    kill_switch_active: bool = False,
    attempt: int = 1,
) -> dict[str, object]:
    value = cast(
        dict[str, object],
        json.loads(OBSERVATION_PATH.read_text(encoding="utf-8")),
    )
    observation = copy.deepcopy(value)
    episode = cast(dict[str, object], observation["episode"])
    episode["turn"] = turn
    risk = cast(dict[str, object], observation["risk"])
    risk["kill_switch_active"] = kill_switch_active
    retry: dict[str, object] | None = None
    if attempt == 2:
        retry = {
            "reason": "invalid_json",
            "detail": "expected one action/v1 object",
            "prior_raw_sha256": "sha256:" + "0" * 64,
        }
    return {
        "schema": "runner_request/v1",
        "attempt": attempt,
        "observation": observation,
        "retry": retry,
    }
