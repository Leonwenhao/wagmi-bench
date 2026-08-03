# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.action import parse_action
from core.config import ConfigError, EpisodeConfig, VirtualClock
from core.observation import AccountState, build_observation
from core.pack import load_pack

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core"
LEAKAGE = ROOT / "fixtures/leakage-probe"
PROHIBITED_IMPORT_ROOTS = {
    "datetime",
    "locale",
    "random",
    "time",
    "zoneinfo",
}


def test_core_paths_have_no_ambient_time_random_or_float_literals() -> None:
    for path in sorted(CORE.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
                assert not roots & PROHIBITED_IMPORT_ROOTS, (path, roots)
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert (
                    node.module.split(".", 1)[0] not in PROHIBITED_IMPORT_ROOTS
                ), (path, node.module)
            assert not (
                isinstance(node, ast.Constant) and isinstance(node.value, float)
            ), (path, getattr(node, "lineno", None))


def test_action_and_observation_repeated_runs_are_identical() -> None:
    body = b'{"schema":"action/v1","target":{"BTC":"-1.25"}}'
    first_action = parse_action(body, {"BTC"})
    second_action = parse_action(body, {"BTC"})
    assert first_action == second_action

    pack = load_pack(LEAKAGE)
    first = build_observation(
        pack,
        EpisodeConfig(),
        episode_id="ep_0123456789abcdef",
        turn=1,
        account=AccountState(
            cash_micro=10_000_000_000,
            realized_pnl_micro=0,
        ),
    )
    second = build_observation(
        pack,
        EpisodeConfig(),
        episode_id="ep_0123456789abcdef",
        turn=1,
        account=AccountState(
            cash_micro=10_000_000_000,
            realized_pnl_micro=0,
        ),
    )
    assert first.document == second.document
    assert first.provenance == second.provenance


def test_virtual_clock_cannot_move_backwards() -> None:
    clock = VirtualClock.for_window(window_start_ts=604_800_000, real_ts=700_000_000)
    with pytest.raises(ConfigError, match="cannot move backwards"):
        clock.advance_to(699_999_999)
