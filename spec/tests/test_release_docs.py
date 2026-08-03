# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from data.catalog import available_pack_ids

ROOT = Path(__file__).resolve().parents[2]
SPDX_HEADER = "# SPDX-License-Identifier: Apache-2.0"
PYTHON_SOURCE_ROOTS = (
    ROOT / "agents",
    ROOT / "cli",
    ROOT / "core",
    ROOT / "data",
    ROOT / "fixtures",
    ROOT / "harness",
    ROOT / "recorder",
    ROOT / "report",
    ROOT / "sandbox",
    ROOT / "spec",
    ROOT / "tools",
)
FROZEN_FIXTURE_HEADER_EXEMPTIONS = frozenset(
    {
        Path("fixtures/golden-mini/derivations/emit_expected_v1.py"),
        Path("fixtures/golden-mini/derivations/reconcile_check.py"),
        Path("fixtures/leakage-probe/tools/gen_fixture.py"),
        Path("fixtures/leakage-probe/tools/validate_fixture.py"),
    }
)

RELEASE_DOCS = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "FAQ.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "pack-catalog.md",
    ROOT / "docs" / "replay-and-verification.md",
    ROOT / "docs" / "adapter-guide.md",
    ROOT / "docs" / "compatible-reader.md",
)

OSS_HYGIENE_FILES = (
    ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml",
    ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
    ROOT / ".github" / "ISSUE_TEMPLATE" / "data_integrity.yml",
    ROOT / ".github" / "ISSUE_TEMPLATE" / "adapter_compatibility.yml",
    ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml",
    ROOT / ".github" / "pull_request_template.md",
)

RELEASE_AUDIT_FILES = (
    ROOT / "docs" / "review" / "M6-tos-check.md",
)

CAVEAT_DOCS = (
    ROOT / "README.md",
    ROOT / "FAQ.md",
    ROOT / "docs" / "pack-catalog.md",
)

ERA_FINGERPRINT_TERMS = (
    "price action",
    "venue-constant era fingerprinting",
    "funding cap/floor",
    "fee tier",
    "tick size",
    "qty step",
    "min notional",
)

_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

# ---------------------------------------------------------------------------
# LABEL-1 banned result framing.
#
# Two tiers.  Literal phrases are banned outright: there is no honest way to
# write "would have made" about a memorized historic window.  "alpha" and
# "skill" are different -- the project's own honesty caveats are *about* those
# words ("survival stress, not skill"), so a bare substring ban would forbid
# the caveats it exists to protect.  They are therefore banned at the *claim*
# level: an assertion that a result proves / demonstrates / measures / generates
# alpha or skill is a violation, while the same words under a negation cue in
# the same sentence are legal.
# ---------------------------------------------------------------------------
BANNED_LITERAL_PHRASES = (
    "would have made",
    "expected return",
    "expected-return",
)

_CLAIM_SUBJECT = r"(?:alpha|skills?)"
_CLAIM_GAP = r"[^.!?]{0,30}?"

BANNED_CLAIM_PATTERNS = (
    re.compile(rf"\bprov(?:e|es|ed|en|ing)\b{_CLAIM_GAP}\b{_CLAIM_SUBJECT}\b"),
    re.compile(rf"\bproof\b{_CLAIM_GAP}\b{_CLAIM_SUBJECT}\b"),
    re.compile(rf"\bdemonstrat\w*\b{_CLAIM_GAP}\b{_CLAIM_SUBJECT}\b"),
    re.compile(rf"\bevidence\s+of\b{_CLAIM_GAP}\b{_CLAIM_SUBJECT}\b"),
    re.compile(
        r"\b(?:measur|quantif|certif)\w*\b" rf"{_CLAIM_GAP}\b{_CLAIM_SUBJECT}\b"
    ),
    re.compile(rf"\bshow(?:s|ed|n|ing)?\b{_CLAIM_GAP}\b{_CLAIM_SUBJECT}\b"),
    re.compile(r"\balpha[-\s]generating\b"),
    re.compile(r"\bgenerat\w*\b" rf"{_CLAIM_GAP}\balpha\b"),
)

# A claim clause is neutralised when its own sentence negates or contrasts it.
_NEGATION_CUE = re.compile(
    r"\b(?:no|not|never|none|nothing|nor|neither|cannot|can't|won't|isn't|"
    r"aren't|doesn't|don't|didn't|without|rather\s+than|instead\s+of)\b"
)

# Markdown lines, list items and clause separators all end a claim's scope.
_SENTENCE_BOUNDARY = re.compile(r"[.!?;:\n|]+")


def banned_result_framing(text: str) -> tuple[str, ...]:
    """Return every LABEL-1 banned-framing hit in ``text`` (empty when clean)."""
    lowered = text.lower()
    hits: list[str] = []

    for phrase in BANNED_LITERAL_PHRASES:
        if phrase in lowered:
            hits.append(f"banned phrase: {phrase!r}")

    for sentence in _SENTENCE_BOUNDARY.split(lowered):
        for pattern in BANNED_CLAIM_PATTERNS:
            for match in pattern.finditer(sentence):
                clause = match.group(0)
                if _NEGATION_CUE.search(sentence[: match.start()]):
                    continue
                if _NEGATION_CUE.search(clause):
                    continue
                hits.append(f"banned claim: {clause.strip()!r}")

    return tuple(hits)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_release_document_set_exists() -> None:
    for path in (
        *RELEASE_DOCS,
        *OSS_HYGIENE_FILES,
        *RELEASE_AUDIT_FILES,
        ROOT / "LICENSE",
    ):
        assert path.is_file(), path.relative_to(ROOT)
        assert path.stat().st_size > 0


def test_upstream_data_posture_is_dated_and_non_relicensing() -> None:
    terms_check = " ".join(
        _text(ROOT / "docs" / "review" / "M6-tos-check.md").split()
    )

    assert "Checked: 2026-07-26" in terms_check
    assert "data.binance.vision" in terms_check
    assert ".CHECKSUM" in terms_check
    assert "no fetched archives or derived market series" in terms_check
    assert "does not purport to relicense Binance market data" in terms_check
    assert "not legal advice" in terms_check


def test_license_and_frozen_evolution_policy_are_exposed() -> None:
    license_text = _text(ROOT / "LICENSE")
    contributing = _text(ROOT / "CONTRIBUTING.md")

    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert "docs/contracts/VERSIONING.md" in contributing


def test_python_sources_carry_apache_spdx_headers() -> None:
    missing = [
        path.relative_to(ROOT)
        for source_root in PYTHON_SOURCE_ROOTS
        for path in source_root.rglob("*.py")
        if path.relative_to(ROOT) not in FROZEN_FIXTURE_HEADER_EXEMPTIONS
        if SPDX_HEADER not in path.read_text(encoding="utf-8").splitlines()[:2]
    ]
    assert not missing, missing


def test_claim_label_and_full_memorization_caveat_are_documented() -> None:
    for path in CAVEAT_DOCS:
        text = " ".join(_text(path).lower().split())
        assert "survival-stress" in text, path.relative_to(ROOT)
        for term in ERA_FINGERPRINT_TERMS:
            assert term in text, (path.relative_to(ROOT), term)


def test_pack_catalog_covers_every_committed_manifest() -> None:
    catalog = _text(ROOT / "docs" / "pack-catalog.md")
    pack_ids = available_pack_ids()

    assert len(pack_ids) == 13
    for pack_id in pack_ids:
        assert f"`{pack_id}`" in catalog
        manifest_path = ROOT / "packs" / pack_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["pack_id"] == pack_id
        assert manifest["claim_label"] == "survival-stress"


def test_release_docs_do_not_use_banned_result_framing() -> None:
    for path in RELEASE_DOCS:
        hits = banned_result_framing(_text(path))
        assert not hits, (path.relative_to(ROOT), hits)


def test_release_docs_and_caveat_docs_share_the_banned_framing_scan() -> None:
    """Every doc carrying the claim label is also scanned for framing."""
    assert set(CAVEAT_DOCS) <= set(RELEASE_DOCS)
    for path in (*RELEASE_DOCS, *RELEASE_AUDIT_FILES):
        assert not banned_result_framing(_text(path)), path.relative_to(ROOT)


@pytest.mark.parametrize(
    "claim",
    [
        "Our benchmark proves agent skill on the FTX window.",
        "The scorecard proves alpha.",
        "This run is proof of skill.",
        "The report demonstrates alpha across all thirteen packs.",
        "These packs demonstrate durable trading skill.",
        "TradeEvolve measures skill.",
        "The metric quantifies alpha.",
        "We certify skill with every bundle.",
        "The equity curve shows real skill.",
        "Results show alpha.",
        "An alpha-generating agent tops the board.",
        "The agent generates alpha in chop regimes.",
        "This is evidence of skill.",
        "It would have made 12% on the COVID window.",
        "Reported as an expected return.",
    ],
)
def test_banned_framing_scanner_flags_claim_assertions(claim: str) -> None:
    assert banned_result_framing(claim), claim


@pytest.mark.parametrize(
    "caveat",
    [
        "Scores are survival stress, not skill.",
        "A named historic scenario can never prove skill.",
        "These packs do not demonstrate alpha.",
        "Nothing in this bundle proves skill.",
        "We make no claim that the score measures skill.",
        "Results are labelled survival-stress rather than skill.",
        "This is not evidence of alpha.",
        "The harness cannot certify skill on memorized windows.",
        "Survival evidence, instead of an alpha claim.",
        "Only the forward season supports skill claims; sealed packs do not.",
        "The pack manifest carries a claim label.",
        "Deterministic replay shows byte-identical bundles.",
    ],
)
def test_banned_framing_scanner_allows_negated_and_neutral_usage(
    caveat: str,
) -> None:
    assert not banned_result_framing(caveat), caveat


def test_local_markdown_links_resolve() -> None:
    for document in RELEASE_DOCS:
        for target in _MARKDOWN_LINK.findall(_text(document)):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = target.split("#", 1)[0]
            if not relative:
                continue
            resolved = (document.parent / relative).resolve()
            assert resolved.is_relative_to(ROOT)
            assert resolved.exists(), (
                document.relative_to(ROOT),
                target,
            )
