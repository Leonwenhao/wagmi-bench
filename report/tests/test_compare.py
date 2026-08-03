# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path

import pytest

from report import (
    CLAIM_LABEL,
    MEMORIZATION_CAVEAT,
    ReportError,
    build_comparison_receipt,
    render_comparison_table,
    write_comparison_files,
)
from report.compare import assign_tier


def _tier(**overrides: object) -> str:
    arguments: dict[str, object] = {
        "survival_verdict": "survived",
        "fills": 5,
        "is_baseline": False,
        "net_return_primary_1e8": 10_000_000,
        "net_return_stress_2x_1e8": 9_000_000,
        "surviving_baseline_returns": ((0, 0), (-5_000_000, -6_000_000)),
        "has_baselines": True,
    }
    arguments.update(overrides)
    return assign_tier(**arguments)  # type: ignore[arg-type]


def test_tier_boundaries() -> None:
    assert _tier() == "GMI"
    assert _tier(survival_verdict="liquidated") == "NGMI"
    assert _tier(survival_verdict="killed_flat") == "NGMI"
    assert _tier(is_baseline=True) == "—"
    assert _tier(fills=0) == "—"
    # No baselines in the group at all: nothing to tier against.
    assert (
        _tier(surviving_baseline_returns=(), has_baselines=False) == "—"
    )
    # Must beat every surviving baseline under BOTH profiles; ties lose.
    assert _tier(net_return_primary_1e8=0) == "—"
    assert _tier(net_return_stress_2x_1e8=-6_000_000) == "—"
    with pytest.raises(ReportError):
        _tier(survival_verdict="unknown_state")


def test_dead_baselines_do_not_set_the_bar() -> None:
    """The COVID case: a killed short's +54% cannot out-rank a survivor.

    Surviving-baseline returns exclude killed rows, so a candidate that
    survived engaged at +31.5% earns GMI even though a killed baseline
    posted a higher paper return before the kill switch fired.
    """

    covid_like = _tier(
        net_return_primary_1e8=31_538_700,
        net_return_stress_2x_1e8=31_106_100,
        # Only flat survived (0%); shorthold's +54% died with it.
        surviving_baseline_returns=((0, 0),),
        has_baselines=True,
    )
    assert covid_like == "GMI"


def test_all_baselines_dead_makes_engaged_survival_the_bar() -> None:
    assert (
        _tier(surviving_baseline_returns=(), has_baselines=True) == "GMI"
    )
    # But an empty vacuous bar never rescues a flat-hold.
    assert (
        _tier(
            fills=0,
            surviving_baseline_returns=(),
            has_baselines=True,
        )
        == "—"
    )


def test_comparison_groups_by_pack_and_marks_flat_hold(
    golden_bundle: Path,
    flat_hold_bundle: Path,
) -> None:
    table = render_comparison_table((golden_bundle, flat_hold_bundle))
    assert table.startswith("WAGMI BENCH COMPARISON")
    assert f"claim_label: {CLAIM_LABEL}" in table
    assert MEMORIZATION_CAVEAT in table
    assert "PACK golden-mini" in table
    assert "SURVIVED — FLAT-HOLD" in table
    verdict_column = table.index("VERDICT")
    returns_column = table.index("NET RETURN")
    assert verdict_column < returns_column


def test_comparison_requires_two_bundles(golden_bundle: Path) -> None:
    with pytest.raises(ReportError):
        render_comparison_table((golden_bundle,))


def test_comparison_files_are_written_outside_bundles(
    golden_bundle: Path,
    flat_hold_bundle: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "compare"
    created = write_comparison_files((golden_bundle, flat_hold_bundle), output)
    assert tuple(path.name for path in created) == ("compare.json", "compare.txt")
    receipt = json.loads((output / "compare.json").read_text("utf-8"))
    assert receipt["schema"] == "compare_receipt/v1"
    assert receipt["claim_label"] == CLAIM_LABEL
    assert len(receipt["rows"]) == 2
    flat_rows = [row for row in receipt["rows"] if row["fills"] == 0]
    assert flat_rows and flat_rows[0]["verdict"] == "SURVIVED — FLAT-HOLD"

    with pytest.raises(ReportError):
        write_comparison_files(
            (golden_bundle, flat_hold_bundle),
            golden_bundle / "inside",
        )


def test_gmi_tier_end_to_end(
    momentum_bundle: Path,
    buyhold_bundle: Path,
    flat_baseline_bundle: Path,
) -> None:
    receipt = build_comparison_receipt(
        (momentum_bundle, buyhold_bundle, flat_baseline_bundle)
    )
    row_list = receipt["rows"]
    assert isinstance(row_list, list)
    rows = {row["agent"]: row for row in row_list}
    momentum = rows["momentum-candidate (none)"]
    assert momentum["is_baseline"] is False
    assert momentum["tier"] == "GMI"
    assert rows["buyhold-baseline (none)"]["tier"] == "—"
    assert rows["buyhold-baseline (none)"]["is_baseline"] is True
    assert rows["flat-baseline (none)"]["tier"] == "—"

    table = render_comparison_table(
        (momentum_bundle, buyhold_bundle, flat_baseline_bundle)
    )
    assert "TIER" in table
    assert "GMI" in table
    assert "Tiers are relative to the baselines" in table


def test_flat_hold_candidate_is_untier_ed_not_gmi(
    flat_hold_bundle: Path,
    flat_baseline_bundle: Path,
) -> None:
    receipt = build_comparison_receipt((flat_hold_bundle, flat_baseline_bundle))
    row_list = receipt["rows"]
    assert isinstance(row_list, list)
    rows = {row["agent"]: row for row in row_list}
    candidate = rows["golden-scripted-agent (none)"]
    assert candidate["fills"] == 0
    assert candidate["tier"] == "—"
    assert candidate["verdict"] == "SURVIVED — FLAT-HOLD"


def test_comparison_receipt_matches_table_rows(
    golden_bundle: Path,
    flat_hold_bundle: Path,
) -> None:
    receipt = build_comparison_receipt((golden_bundle, flat_hold_bundle))
    table = render_comparison_table((golden_bundle, flat_hold_bundle))
    rows = receipt["rows"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        assert str(row["bundle_root"]) in table
