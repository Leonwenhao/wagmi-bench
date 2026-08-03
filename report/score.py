# SPDX-License-Identifier: Apache-2.0
"""WAGMI Score v0: one survival-dominant number per agent across packs.

The score is a render-time aggregate over sealed bundles, never sealed
itself. Design constraints, in order: survival dominates; engagement is
required (flat-holding can never climb the board); relative performance is
measured only against surviving baselines; both cost profiles must agree;
every rung is explainable in one sentence and recomputable by anyone from
public bundles.

Per-pack ladder (0/25/50/75/100):

- ``0``   — the episode ended liquidated or killed flat.
- ``25``  — survived without a single executed fill (flat-hold).
- ``50``  — survived with real engagement.
- ``75``  — engaged survival with a net return above zero under BOTH
            cost profiles.
- ``100`` — all of the above and the GMI condition: net return above
            every surviving baseline in the pack group under both
            profiles (dead baselines never set the bar).

An agent's WAGMI Score is the mean of its pack scores over the full
catalog: packs not attempted contribute zero, so partial coverage cannot
be hidden. A perpetual flat-holder converges to 25; any engaged survivor
set outranks it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from report.compare import ComparisonRow, load_comparison_rows
from report.generator import (
    CLAIM_LABEL,
    EVIDENCE_LIMIT,
    MEMORIZATION_CAVEAT,
    ReportError,
)

CATALOG_PACK_COUNT = 13

SCORE_LEGEND = (
    "WAGMI Score v0: per pack — 0 liquidated/killed, 25 flat-hold "
    "survival, 50 engaged survival, 75 engaged survival with positive net "
    "return under both cost profiles, 100 GMI (above every surviving "
    "baseline under both profiles). Agent score = mean over the full "
    f"{CATALOG_PACK_COUNT}-pack catalog; unattempted packs count zero."
)


@dataclass(frozen=True)
class PackScore:
    """One agent's rung on one pack."""

    pack: str
    score: int
    verdict: str
    tier: str
    fills: int
    bundle_root: str


@dataclass(frozen=True)
class AgentScore:
    """One agent's aggregate over the catalog."""

    agent: str
    wagmi_score: float
    packs_attempted: int
    pack_scores: tuple[PackScore, ...]


def pack_score(row: ComparisonRow) -> int:
    """Score one candidate row on the v0 ladder."""

    if row.survival_verdict in ("liquidated", "killed_flat"):
        return 0
    if row.survival_verdict != "survived":
        raise ReportError(
            f"unknown survival verdict {row.survival_verdict!r} while scoring"
        )
    if row.fills == 0:
        return 25
    positive_both = (
        row.net_return_primary_1e8 > 0 and row.net_return_stress_2x_1e8 > 0
    )
    if not positive_both:
        return 50
    if row.tier == "GMI":
        return 100
    return 75


def build_scoreboard(
    bundle_dirs: Sequence[str | Path],
) -> tuple[AgentScore, ...]:
    """Score every non-baseline agent found across the given bundles."""

    rows = load_comparison_rows(bundle_dirs)
    by_agent: dict[str, list[ComparisonRow]] = {}
    for row in rows:
        if row.is_baseline:
            continue
        by_agent.setdefault(row.agent, []).append(row)
    if not by_agent:
        raise ReportError(
            "no candidate rows to score: every bundle is a baseline"
        )
    scores: list[AgentScore] = []
    for agent, agent_rows in sorted(by_agent.items()):
        seen_packs: set[str] = set()
        pack_scores: list[PackScore] = []
        for row in agent_rows:
            if row.pack in seen_packs:
                raise ReportError(
                    f"agent {agent!r} has more than one bundle for pack "
                    f"{row.pack!r}; score one run per pack"
                )
            seen_packs.add(row.pack)
            pack_scores.append(
                PackScore(
                    pack=row.pack,
                    score=pack_score(row),
                    verdict=row.verdict,
                    tier=row.tier,
                    fills=row.fills,
                    bundle_root=row.bundle_root,
                )
            )
        total = sum(entry.score for entry in pack_scores)
        scores.append(
            AgentScore(
                agent=agent,
                wagmi_score=round(total / CATALOG_PACK_COUNT, 2),
                packs_attempted=len(pack_scores),
                pack_scores=tuple(pack_scores),
            )
        )
    scores.sort(key=lambda entry: (-entry.wagmi_score, entry.agent))
    return tuple(scores)


def build_score_receipt(
    bundle_dirs: Sequence[str | Path],
) -> dict[str, object]:
    scoreboard = build_scoreboard(bundle_dirs)
    return {
        "schema": "wagmi_score_receipt/v0",
        "claim_label": CLAIM_LABEL,
        "score_legend": SCORE_LEGEND,
        "memorization_caveat": MEMORIZATION_CAVEAT,
        "evidence_limit": EVIDENCE_LIMIT,
        "catalog_pack_count": CATALOG_PACK_COUNT,
        "agents": [
            {
                "agent": entry.agent,
                "wagmi_score": entry.wagmi_score,
                "packs_attempted": entry.packs_attempted,
                "pack_scores": [
                    {
                        "pack": item.pack,
                        "score": item.score,
                        "verdict": item.verdict,
                        "tier": item.tier,
                        "fills": item.fills,
                        "bundle_root": item.bundle_root,
                    }
                    for item in entry.pack_scores
                ],
            }
            for entry in scoreboard
        ],
    }


def render_score_table(bundle_dirs: Sequence[str | Path]) -> str:
    scoreboard = build_scoreboard(bundle_dirs)
    lines = [
        "WAGMI SCORE v0",
        f"claim_label: {CLAIM_LABEL}",
        "",
    ]
    width = max(len(entry.agent) for entry in scoreboard)
    width = max(width, len("AGENT"))
    lines.append(
        f"{'AGENT'.ljust(width)}  SCORE   PACKS"
    )
    for entry in scoreboard:
        lines.append(
            f"{entry.agent.ljust(width)}  "
            f"{entry.wagmi_score:6.2f}  "
            f"{entry.packs_attempted}/{CATALOG_PACK_COUNT}"
        )
    lines.extend(
        [
            "",
            SCORE_LEGEND,
            "",
            f"claim_label: {CLAIM_LABEL}",
            MEMORIZATION_CAVEAT,
            EVIDENCE_LIMIT,
        ]
    )
    text = "\n".join(lines) + "\n"
    for required in (CLAIM_LABEL, MEMORIZATION_CAVEAT, EVIDENCE_LIMIT):
        if required not in text:
            raise ReportError("score surface lost a required label")
    return text


def write_score_files(
    bundle_dirs: Sequence[str | Path],
    output_dir: str | Path,
) -> tuple[Path, ...]:
    output = Path(output_dir)
    for bundle_dir in bundle_dirs:
        bundle = Path(bundle_dir).resolve()
        if bundle == output.resolve() or bundle in output.resolve().parents:
            raise ReportError(
                "score output directory must live outside every bundle"
            )
    table = render_score_table(bundle_dirs)
    receipt = build_score_receipt(bundle_dirs)
    output.mkdir(parents=True, exist_ok=False)
    table_path = output / "score.txt"
    receipt_path = output / "score.json"
    table_path.write_text(table, encoding="utf-8")
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return (receipt_path, table_path)
