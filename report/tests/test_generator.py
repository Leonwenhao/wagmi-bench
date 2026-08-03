# SPDX-License-Identifier: Apache-2.0
"""M5 C5 report snapshots, determinism, layering, and error gates."""

from __future__ import annotations

import hashlib
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from recorder.redaction import create_share_bundle
from recorder.verify import verify_bundle
from report import (
    CLAIM_LABEL,
    ReportError,
    generate_report,
    render_html_report,
    render_share_card_svg,
    render_terminal_report,
    write_report_files,
)

FORBIDDEN_RESULT_FRAMING = (
    "alpha",
    "skill",
    "expected return",
    "expected-return",
    "would have made",
)

# Golden-mini is a frozen economic oracle; these hashes pin the three M5
# render surfaces generated from its independently sealed IC-5 bundle.
EXPECTED_TERMINAL_SHA256 = (
    "64f251e0d92ee26843af47e0c6a95c2779c68dc91fc8074c8b8d6b5c156807a1"
)
EXPECTED_HTML_SHA256 = (
    "7c635a04803ae5e4bf0b7e3dca53c2d69bd90e230b112f3b4213b86fdcef81ed"
)
EXPECTED_CARD_SHA256 = (
    "4e0456f0680b87bd5e8be7b95f299c65eb9c617defd61ac1ab65e84c07079961"
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_complete_bundle_renders_all_surfaces_deterministically(
    golden_bundle: Path,
) -> None:
    first = generate_report(golden_bundle)
    second = generate_report(golden_bundle)
    receipt = verify_bundle(golden_bundle)

    assert first == second
    assert receipt.is_complete
    assert receipt.root is not None
    for artifact in (
        first.terminal_text,
        first.html,
        first.share_card_svg,
    ):
        assert f"claim_label: {CLAIM_LABEL}" in artifact
        assert "venue-constant era fingerprinting" in artifact
        assert receipt.root in artifact
        lower = artifact.lower()
        assert all(phrase not in lower for phrase in FORBIDDEN_RESULT_FRAMING)

    assert first.terminal_text == render_terminal_report(golden_bundle)
    assert first.html == render_html_report(golden_bundle)
    assert first.share_card_svg == render_share_card_svg(golden_bundle)


def test_flat_hold_survival_is_marked_on_every_surface(
    flat_hold_bundle: Path,
    golden_bundle: Path,
) -> None:
    flat = generate_report(flat_hold_bundle)
    assert "SURVIVED — FLAT-HOLD" in flat.terminal_text
    assert "Zero executed fills" in flat.terminal_text
    assert "SURVIVED — FLAT-HOLD" in flat.html
    assert "Zero executed fills" in flat.html
    assert "SURVIVED — FLAT-HOLD" in flat.share_card_svg
    assert "0 fills — never took a position" in flat.share_card_svg

    engaged = generate_report(golden_bundle)
    assert "FLAT-HOLD" not in engaged.terminal_text
    assert "FLAT-HOLD" not in engaged.html
    assert "FLAT-HOLD" not in engaged.share_card_svg


def test_golden_bundle_report_snapshots(golden_bundle: Path) -> None:
    report = generate_report(golden_bundle)

    assert _sha256(report.terminal_text) == EXPECTED_TERMINAL_SHA256
    assert _sha256(report.html) == EXPECTED_HTML_SHA256
    assert _sha256(report.share_card_svg) == EXPECTED_CARD_SHA256


def test_every_render_surface_carries_the_wagmi_bench_name(
    golden_bundle: Path,
) -> None:
    """User-visible branding is WAGMI Bench on all three render surfaces."""
    report = generate_report(golden_bundle)

    assert "WAGMI BENCH SURVIVAL REPORT" in report.terminal_text
    assert "<title>WAGMI Bench survival report" in report.html
    assert 'aria-label="WAGMI Bench ' in report.share_card_svg
    assert ">WAGMI BENCH</text>" in report.share_card_svg
    for artifact in (report.terminal_text, report.html, report.share_card_svg):
        lowered = artifact.lower()
        assert "tradeevolve" not in lowered
        assert "tradevolve" not in lowered


def test_timeline_explains_nav_drop_from_stored_turn_evidence(
    golden_bundle: Path,
) -> None:
    report = generate_report(golden_bundle)

    assert "TURN 0004 | DECISION BAR 4 -> NAV BAR 5" in (
        report.terminal_text
    )
    assert (
        "delta NAV=-1060.863500 quote = "
        "price -1058.000000 quote + "
        "funding -2.863500 quote + "
        "fees 0.000000 quote + "
        "liquidation penalty 0.000000 quote"
    ) in report.terminal_text
    for label in (
        "SAW:",
        "SAID:",
        "MEANT:",
        "RULE:",
        "HAPPENED:",
        "COST TO HOLD:",
        "NAV ATTRIBUTION:",
    ):
        assert label in report.terminal_text
    assert "NEAR LIQUIDATION" in report.terminal_text
    assert "wick distance=3.3072%" in report.terminal_text
    assert "EQUITY VERSUS PRICE" in report.terminal_text
    assert "All series normalized to 100.00" in report.terminal_text

    assert "Turn 0004 · Decision bar 4 → NAV bar 5" in report.html
    assert "Exact stored observation" in report.html
    assert "NAV attribution" in report.html
    assert "Evidence events" in report.html


def test_html_is_standalone_and_embeds_equity_price_chart(
    golden_bundle: Path,
) -> None:
    html = render_html_report(golden_bundle)

    assert html.startswith("<!doctype html>\n")
    assert '<meta charset="utf-8">' in html
    assert '<svg class="equity-chart"' in html
    assert "Primary NAV" in html
    assert "Stress NAV" in html
    assert "BTC mark" in html
    assert "<script" not in html.lower()
    assert "<link" not in html.lower()
    assert " src=" not in html.lower()
    assert " href=" not in html.lower()


def test_share_card_is_static_labeled_and_survival_first(
    golden_bundle: Path,
) -> None:
    card = render_share_card_svg(golden_bundle)
    root = ET.fromstring(card)

    assert root.tag.endswith("svg")
    assert "SURVIVAL-NATIVE READOUT" in card
    assert "Primary costs" in card
    assert "Stress 2x costs" in card
    assert "Maximum drawdown" in card
    assert "Minimum liquidation distance" in card
    assert "Funding flow" in card
    assert "Turnover" in card
    assert "End NAV change" not in card
    assert "net_return" not in card


def test_report_refuses_truncated_corrupt_and_missing_bundles(
    golden_bundle: Path,
    tmp_path: Path,
) -> None:
    truncated = tmp_path / "truncated"
    shutil.copytree(golden_bundle, truncated)
    (truncated / "chain.json").unlink()
    with pytest.raises(
        ReportError,
        match=r"requires a COMPLETE bundle.*TRUNCATED",
    ):
        generate_report(truncated)

    corrupt = tmp_path / "corrupt"
    shutil.copytree(golden_bundle, corrupt)
    metrics = bytearray((corrupt / "metrics.json").read_bytes())
    location = metrics.index(b'"bars":') + len(b'"bars":')
    metrics[location] = ord("9")
    (corrupt / "metrics.json").write_bytes(bytes(metrics))
    with pytest.raises(
        ReportError,
        match=r"requires a COMPLETE bundle.*CORRUPT",
    ):
        generate_report(corrupt)

    with pytest.raises(
        ReportError,
        match=r"requires a COMPLETE bundle.*CORRUPT",
    ):
        generate_report(tmp_path / "missing")


def test_report_is_copy_location_independent(
    golden_bundle: Path,
    tmp_path: Path,
) -> None:
    copied = tmp_path / "renamed-evidence"
    shutil.copytree(golden_bundle, copied)

    assert generate_report(copied) == generate_report(golden_bundle)


def test_write_report_files_preserves_exact_rendered_bytes(
    golden_bundle: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "rendered"
    expected = generate_report(golden_bundle)

    files = write_report_files(golden_bundle, output)

    assert files.terminal_text.read_text(encoding="utf-8") == (
        expected.terminal_text
    )
    assert files.html.read_text(encoding="utf-8") == expected.html
    assert files.share_card_svg.read_text(encoding="utf-8") == (
        expected.share_card_svg
    )
    assert files.terminal_text.name == "report.txt"
    assert files.html.name == "report.html"
    assert files.share_card_svg.name == "share-card.svg"

    with pytest.raises(ReportError, match="outside the sealed bundle"):
        write_report_files(golden_bundle, golden_bundle / "derived")


def test_complete_share_bundle_renders_disclosed_raw_absence(
    golden_bundle: Path,
    tmp_path: Path,
) -> None:
    share = tmp_path / "share"
    parent = verify_bundle(golden_bundle)
    create_share_bundle(golden_bundle, share)

    report = generate_report(share)
    shared = verify_bundle(share)

    assert shared.is_complete
    assert shared.root == parent.root
    assert "[raw model text absent under disclosed share redaction]" in (
        report.terminal_text
    )
    assert "raw model text absent under disclosed share redaction" in (
        report.html
    )


def test_report_layer_has_no_engine_or_live_state_dependency() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "generator.py"
    ).read_text(encoding="utf-8")

    assert "core.engine" not in source
    assert "run_episode" not in source
    assert "time.time" not in source
    assert "datetime.now" not in source
    assert "random." not in source
    assert "MEMORIZATION_CAVEAT =" in source
    assert "venue-constant era " in source
