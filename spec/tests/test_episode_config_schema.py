# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from core.config import (
    DEFAULT_COST_PROFILES,
    DEFAULT_DRAWDOWN_KILL_SWITCH_1E8,
    DEFAULT_GROSS_LEVERAGE_CAP_LEV_1E4,
    DEFAULT_RESPONSE_DEADLINE_MS,
    DEFAULT_STARTING_NAV_MICRO,
    ConfigError,
    EpisodeConfig,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    ROOT / "spec/runtime-schemas/episode_config.v1.schema.json"
)
SCHEMA = cast(dict[str, object], json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
VALIDATOR = Draft202012Validator(SCHEMA)


def test_runtime_episode_config_schema_is_valid_and_non_frozen() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA_PATH.parent.name == "runtime-schemas"
    assert SCHEMA_PATH.parent != ROOT / "spec/schemas"


def test_golden_legacy_config_validates_and_normalizes() -> None:
    raw = cast(
        dict[str, object],
        json.loads(
            (ROOT / "fixtures/golden-mini/episode_config.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    VALIDATOR.validate(raw)
    config = EpisodeConfig.from_mapping(raw)
    assert config.lookback_bars == 4
    assert config.funding_prints == 2
    assert config.response_deadline_ms == DEFAULT_RESPONSE_DEADLINE_MS
    assert config.cost_profiles == DEFAULT_COST_PROFILES


def test_normalized_runtime_form_round_trips_through_episode_config() -> None:
    expected = EpisodeConfig()
    raw = {
        "schema": "episode_config/v1",
        **expected.to_run_config(),
        "parse_failure_retries": expected.parse_failure_retries,
    }
    VALIDATOR.validate(raw)
    assert EpisodeConfig.from_mapping(raw) == expected


def test_schema_default_annotations_match_runtime_defaults() -> None:
    properties = cast(dict[str, dict[str, object]], SCHEMA["properties"])
    assert properties["starting_nav_micro"]["default"] == DEFAULT_STARTING_NAV_MICRO
    assert (
        properties["leverage_cap_gross_lev_1e4"]["default"]
        == DEFAULT_GROSS_LEVERAGE_CAP_LEV_1E4
    )
    assert (
        properties["drawdown_kill_switch_1e8"]["default"]
        == DEFAULT_DRAWDOWN_KILL_SWITCH_1E8
    )
    assert (
        properties["response_deadline_ms"]["default"]
        == DEFAULT_RESPONSE_DEADLINE_MS
    )
    assert properties["cost_profiles"]["default"] == list(DEFAULT_COST_PROFILES)


@pytest.mark.parametrize(
    "raw",
    [
        {"schema": "episode_config/v2"},
        {"starting_nav_micro": 0},
        {"starting_nav_micro": True},
        {"leverage_cap_gross_lev_1e4": 0},
        {"drawdown_kill_switch_1e8": 0},
        {"lookback": []},
        {"lookback": {"bars": 0}},
        {"funding_prints": -1},
        {"response_deadline_ms": 0},
        {"seed": -1},
        {"cost_profiles": ["primary"]},
        {"cost_profiles": ["primary", "stress_2x", "primary"]},
        {"cost_profiles": ["primary", "stress_2x", 1]},
        {"cost_profile": "cheap"},
        {"turnover_cap_1e8": -1},
        {"parse_failure_retries": -1},
    ],
)
def test_schema_and_runtime_reject_the_same_invalid_semantic_values(
    raw: dict[str, object],
) -> None:
    assert list(VALIDATOR.iter_errors(raw))
    with pytest.raises(ConfigError):
        EpisodeConfig.from_mapping(raw)


def test_schema_rejects_unknown_authored_fields() -> None:
    assert list(VALIDATOR.iter_errors({"unknown": 1}))
