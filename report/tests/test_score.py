# SPDX-License-Identifier: Apache-2.0
"""WAGMI Score ladder and aggregation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from report import (
    CLAIM_LABEL,
    ReportError,
    build_score_receipt,
    render_score_table,
    write_score_files,
)
from report.compare import ComparisonRow
from report.score import CATALOG_PACK_COUNT, pack_score


def _row(**overrides: object) -> ComparisonRow:
    values: dict[str, object] = {
        "pack": "covid-black-thursday",
        "agent": "candidate (model)",
        "verdict": "SURVIVED",
        "tier": "—",
        "fills": 19,
        "max_dd_primary": "3.2932%",
        "max_dd_stress_2x": "3.2942%",
        "turnover_primary": "4.2703x",
        "net_return_primary": "+31.5387%",
        "net_return_stress_2x": "+31.1061%",
        "net_return_primary_1e8": 31_538_700,
        "net_return_stress_2x_1e8": 31_106_100,
        "survival_verdict": "survived",
        "is_baseline": False,
        "bundle_root": "sha256:" + "ab" * 32,
    }
    values.update(overrides)
    return ComparisonRow(**values)  # type: ignore[arg-type]


def test_pack_score_ladder() -> None:
    assert pack_score(_row(survival_verdict="liquidated")) == 0
    assert pack_score(_row(survival_verdict="killed_flat")) == 0
    assert pack_score(_row(fills=0)) == 25
    assert (
        pack_score(
            _row(net_return_primary_1e8=-1, net_return_stress_2x_1e8=-1)
        )
        == 50
    )
    # Positive under one profile only is not positive-both.
    assert (
        pack_score(
            _row(net_return_primary_1e8=5, net_return_stress_2x_1e8=0)
        )
        == 50
    )
    assert pack_score(_row(tier="—")) == 75
    assert pack_score(_row(tier="GMI")) == 100
    with pytest.raises(ReportError):
        pack_score(_row(survival_verdict="unknown"))


def test_scoreboard_over_real_bundles(
    momentum_bundle: Path,
    buyhold_bundle: Path,
    flat_baseline_bundle: Path,
    tmp_path: Path,
) -> None:
    bundles = (momentum_bundle, buyhold_bundle, flat_baseline_bundle)
    receipt = build_score_receipt(bundles)
    agents = receipt["agents"]
    assert isinstance(agents, list)
    assert len(agents) == 1  # baselines are never scored
    entry = agents[0]
    assert entry["agent"] == "momentum-candidate (none)"
    assert entry["packs_attempted"] == 1
    # Momentum earns GMI on golden-mini → 100 on 1 of 13 packs.
    assert entry["wagmi_score"] == round(100 / CATALOG_PACK_COUNT, 2)
    assert entry["pack_scores"][0]["score"] == 100

    table = render_score_table(bundles)
    assert "WAGMI SCORE v0" in table
    assert f"claim_label: {CLAIM_LABEL}" in table
    assert "1/13" in table

    output = tmp_path / "scoreboard"
    created = write_score_files(bundles, output)
    assert tuple(path.name for path in created) == ("score.json", "score.txt")
    stored = json.loads((output / "score.json").read_text("utf-8"))
    assert stored["schema"] == "wagmi_score_receipt/v0"


def test_duplicate_pack_runs_are_refused(
    momentum_bundle: Path,
    flat_baseline_bundle: Path,
) -> None:
    with pytest.raises(ReportError, match="one run per pack"):
        build_score_receipt(
            (momentum_bundle, momentum_bundle, flat_baseline_bundle)
        )


def test_baseline_only_input_is_refused(
    buyhold_bundle: Path,
    flat_baseline_bundle: Path,
) -> None:
    with pytest.raises(ReportError, match="every bundle is a baseline"):
        build_score_receipt((buyhold_bundle, flat_baseline_bundle))
