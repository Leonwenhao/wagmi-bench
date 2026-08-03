# SPDX-License-Identifier: Apache-2.0
"""Cross-bundle comparison: one pack-grouped table over verified bundles.

The comparison exists so a headline survival verdict is always read against
do-nothing and hold-one-way baselines on the same pack: a zero-fill
"survived" must be visibly a flat-hold, never a leaderboard win. Layering
matches the single-bundle report (SCH-4): every cell derives from verified
COMPLETE bundles, and the surface is survival-first — verdict and engagement
columns precede any return column, and returns never appear without both
cost profiles.

Tiers are relative, render-time descriptions, never sealed into bundles:
GMI means the candidate survived with engagement and finished with a net
return above every baseline in its pack group under BOTH cost profiles;
NGMI means the episode ended liquidated or killed flat. Baselines and
flat-holds are untiered. A tier is a comparison against fixed reference
policies on identical frozen data — it is not a forward-looking claim.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from report.generator import (
    CLAIM_LABEL,
    EVIDENCE_LIMIT,
    MEMORIZATION_CAVEAT,
    ReportError,
    _agent_label,
    _as_int,
    _as_str,
    _display_verdict,
    _Evidence,
    _execution_counts,
    _load_evidence,
    _pack_id,
    _percent,
    _ratio,
)

TIER_LEGEND = (
    "Tiers are relative to the baselines in each pack group. GMI: survived "
    "with executed fills and a net return above every surviving baseline "
    "under both cost profiles; a killed baseline's paper return does not "
    "set the bar. NGMI: liquidated or killed flat. Baselines, flat-holds, "
    "and packs without baselines are untiered (—)."
)

_BASELINE_POLICY = "constant-target"

_COLUMNS = (
    "agent",
    "verdict",
    "tier",
    "fills",
    "max_dd_primary",
    "max_dd_stress_2x",
    "turnover_primary",
    "net_return_primary",
    "net_return_stress_2x",
)
_HEADERS = {
    "agent": "AGENT",
    "verdict": "VERDICT",
    "tier": "TIER",
    "fills": "FILLS",
    "max_dd_primary": "MAX DD",
    "max_dd_stress_2x": "MAX DD 2X",
    "turnover_primary": "TURNOVER",
    "net_return_primary": "NET RETURN",
    "net_return_stress_2x": "NET RETURN 2X",
}


@dataclass(frozen=True)
class ComparisonRow:
    """One bundle rendered as one table row."""

    pack: str
    agent: str
    verdict: str
    tier: str
    fills: int
    max_dd_primary: str
    max_dd_stress_2x: str
    turnover_primary: str
    net_return_primary: str
    net_return_stress_2x: str
    net_return_primary_1e8: int
    net_return_stress_2x_1e8: int
    survival_verdict: str
    is_baseline: bool
    bundle_root: str


def _metric(evidence: _Evidence, profile: str, field: str) -> int:
    return _as_int(
        evidence.profiles[profile].get(field),
        f"metrics.profiles.{profile}.{field}",
    )


def _is_baseline(evidence: _Evidence) -> bool:
    params = evidence.agent_manifest.get("inference_params")
    if not isinstance(params, dict):
        return False
    return params.get("policy") == _BASELINE_POLICY


def assign_tier(
    *,
    survival_verdict: str,
    fills: int,
    is_baseline: bool,
    net_return_primary_1e8: int,
    net_return_stress_2x_1e8: int,
    surviving_baseline_returns: Sequence[tuple[int, int]],
    has_baselines: bool,
) -> str:
    """Tier one candidate against its pack group's surviving baselines.

    ``surviving_baseline_returns`` holds (primary, stress_2x) net returns
    for baseline rows in the same pack group whose episodes survived. A
    killed or liquidated baseline keeps its table row but never sets the
    return bar: the bench's thesis is that a dead strategy's paper gains
    are not success, so they cannot out-rank a survivor. When baselines
    exist but none survived, an engaged survivor clears a vacuous bar and
    earns GMI outright. Pure so the boundary cases are directly testable
    without building bundles.
    """

    if is_baseline:
        return "—"
    if survival_verdict in ("liquidated", "killed_flat"):
        return "NGMI"
    if survival_verdict != "survived":
        raise ReportError(
            f"unknown survival verdict {survival_verdict!r} while tiering"
        )
    if fills == 0 or not has_baselines:
        return "—"
    beats_primary = all(
        net_return_primary_1e8 > primary
        for primary, _stress in surviving_baseline_returns
    )
    beats_stress = all(
        net_return_stress_2x_1e8 > stress
        for _primary, stress in surviving_baseline_returns
    )
    if beats_primary and beats_stress:
        return "GMI"
    return "—"


def _row(evidence: _Evidence) -> ComparisonRow:
    fills, _cancels = _execution_counts(evidence)
    survival_verdict = _as_str(
        evidence.invariant.get("survival_verdict"),
        "metrics.profile_invariant.survival_verdict",
    )
    return ComparisonRow(
        pack=_pack_id(evidence),
        agent=_agent_label(evidence),
        verdict=_display_verdict(evidence),
        tier="—",
        fills=fills,
        max_dd_primary=_percent(
            _metric(evidence, "primary", "max_drawdown_1e8")
        ),
        max_dd_stress_2x=_percent(
            _metric(evidence, "stress_2x", "max_drawdown_1e8")
        ),
        turnover_primary=(
            f"{_ratio(_metric(evidence, 'primary', 'turnover_1e8'))}x"
        ),
        net_return_primary=_percent(
            _metric(evidence, "primary", "net_return_1e8"),
            signed=True,
        ),
        net_return_stress_2x=_percent(
            _metric(evidence, "stress_2x", "net_return_1e8"),
            signed=True,
        ),
        net_return_primary_1e8=_metric(evidence, "primary", "net_return_1e8"),
        net_return_stress_2x_1e8=_metric(
            evidence, "stress_2x", "net_return_1e8"
        ),
        survival_verdict=survival_verdict,
        is_baseline=_is_baseline(evidence),
        bundle_root=evidence.bundle_root,
    )


def load_comparison_rows(
    bundle_dirs: Sequence[str | Path],
) -> tuple[ComparisonRow, ...]:
    if len(bundle_dirs) < 2:
        raise ReportError(
            "a comparison needs at least two bundles (a candidate and a "
            "baseline on the same pack)"
        )
    rows = [_row(_load_evidence(bundle_dir)) for bundle_dir in bundle_dirs]

    packs_with_baselines: set[str] = set()
    surviving_returns: dict[str, list[tuple[int, int]]] = {}
    for row in rows:
        if not row.is_baseline:
            continue
        packs_with_baselines.add(row.pack)
        if row.survival_verdict == "survived":
            surviving_returns.setdefault(row.pack, []).append(
                (row.net_return_primary_1e8, row.net_return_stress_2x_1e8)
            )
    tiered = [
        ComparisonRow(
            **{
                **asdict(row),
                "tier": assign_tier(
                    survival_verdict=row.survival_verdict,
                    fills=row.fills,
                    is_baseline=row.is_baseline,
                    net_return_primary_1e8=row.net_return_primary_1e8,
                    net_return_stress_2x_1e8=row.net_return_stress_2x_1e8,
                    surviving_baseline_returns=surviving_returns.get(
                        row.pack, ()
                    ),
                    has_baselines=row.pack in packs_with_baselines,
                ),
            }
        )
        for row in rows
    ]
    return tuple(tiered)


def _pack_groups(
    rows: Sequence[ComparisonRow],
) -> tuple[tuple[str, tuple[ComparisonRow, ...]], ...]:
    order: list[str] = []
    grouped: dict[str, list[ComparisonRow]] = {}
    for row in rows:
        if row.pack not in grouped:
            order.append(row.pack)
            grouped[row.pack] = []
        grouped[row.pack].append(row)
    return tuple((pack, tuple(grouped[pack])) for pack in order)


def render_comparison_table(bundle_dirs: Sequence[str | Path]) -> str:
    rows = load_comparison_rows(bundle_dirs)
    cells = {
        column: [_HEADERS[column]]
        + [str(getattr(row, column)) for row in rows]
        for column in _COLUMNS
    }
    widths = {
        column: max(len(value) for value in values)
        for column, values in cells.items()
    }

    def format_line(values: Sequence[str]) -> str:
        return "  ".join(
            value.ljust(widths[column])
            for column, value in zip(_COLUMNS, values, strict=True)
        ).rstrip()

    header = format_line([_HEADERS[column] for column in _COLUMNS])
    lines = [
        "WAGMI BENCH COMPARISON",
        f"claim_label: {CLAIM_LABEL}",
        "",
    ]
    for pack, pack_rows in _pack_groups(rows):
        lines.append(f"PACK {pack}")
        lines.append(header)
        for row in pack_rows:
            lines.append(
                format_line([str(getattr(row, column)) for column in _COLUMNS])
            )
        lines.append("")
    lines.extend(
        [
            TIER_LEGEND,
            "",
            f"claim_label: {CLAIM_LABEL}",
            MEMORIZATION_CAVEAT,
            EVIDENCE_LIMIT,
            "",
            "EVIDENCE ROOTS",
        ]
    )
    for row in rows:
        lines.append(f"{row.pack} / {row.agent}: {row.bundle_root}")
    text = "\n".join(lines) + "\n"
    _assert_comparison_labels(text)
    return text


def build_comparison_receipt(
    bundle_dirs: Sequence[str | Path],
) -> dict[str, object]:
    rows = load_comparison_rows(bundle_dirs)
    return {
        "schema": "compare_receipt/v1",
        "claim_label": CLAIM_LABEL,
        "tier_legend": TIER_LEGEND,
        "memorization_caveat": MEMORIZATION_CAVEAT,
        "evidence_limit": EVIDENCE_LIMIT,
        "rows": [asdict(row) for row in rows],
    }


def _assert_comparison_labels(text: str) -> None:
    for required in (
        CLAIM_LABEL,
        MEMORIZATION_CAVEAT,
        EVIDENCE_LIMIT,
        TIER_LEGEND,
    ):
        if required not in text:
            raise ReportError(
                "comparison surface lost a required label or caveat"
            )


def write_comparison_files(
    bundle_dirs: Sequence[str | Path],
    output_dir: str | Path,
) -> tuple[Path, ...]:
    output = Path(output_dir)
    for bundle_dir in bundle_dirs:
        bundle = Path(bundle_dir).resolve()
        if bundle == output.resolve() or bundle in output.resolve().parents:
            raise ReportError(
                "comparison output directory must live outside every bundle"
            )
    table = render_comparison_table(bundle_dirs)
    receipt = build_comparison_receipt(bundle_dirs)
    output.mkdir(parents=True, exist_ok=False)
    table_path = output / "compare.txt"
    receipt_path = output / "compare.json"
    table_path.write_text(table, encoding="utf-8")
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return (receipt_path, table_path)
