# SPDX-License-Identifier: Apache-2.0
"""Deterministic, bundle-only survival reporting.

The public API accepts an IC-5 evidence-bundle path and never an engine
instance.  Every renderer refuses anything the recorder verifier does not
classify as ``COMPLETE``.
"""

from report.compare import (
    build_comparison_receipt,
    render_comparison_table,
    write_comparison_files,
)
from report.generator import (
    CLAIM_LABEL,
    MEMORIZATION_CAVEAT,
    ReportArtifacts,
    ReportError,
    ReportFiles,
    generate_report,
    render_html_report,
    render_share_card_svg,
    render_terminal_report,
    write_report_files,
)
from report.score import (
    build_score_receipt,
    render_score_table,
    write_score_files,
)

__all__ = [
    "CLAIM_LABEL",
    "MEMORIZATION_CAVEAT",
    "ReportArtifacts",
    "ReportError",
    "ReportFiles",
    "generate_report",
    "render_html_report",
    "render_share_card_svg",
    "render_terminal_report",
    "build_comparison_receipt",
    "build_score_receipt",
    "render_comparison_table",
    "render_score_table",
    "write_comparison_files",
    "write_score_files",
    "write_report_files",
]
