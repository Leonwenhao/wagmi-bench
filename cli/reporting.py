# SPDX-License-Identifier: Apache-2.0
"""Narrow integration seam between the CLI and the bundle-only report API."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import cast


class ReportingUnavailableError(RuntimeError):
    """The report component is missing or does not expose the CLI seam."""


def _callable(name: str) -> Callable[..., object]:
    try:
        module = importlib.import_module("report")
    except ImportError as exc:
        raise ReportingUnavailableError(
            "the report component is not installed"
        ) from exc
    candidate = getattr(module, name, None)
    if not callable(candidate):
        raise ReportingUnavailableError(
            f"the report component does not expose {name}()"
        )
    return cast(Callable[..., object], candidate)


def _created_files(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
        )
    )


def generate_report(bundle_dir: Path, output_dir: Path) -> tuple[Path, ...]:
    """Generate report artifacts strictly from a sealed bundle."""

    function = _callable("write_report_files")
    function(bundle_dir, output_dir)
    created = _created_files(output_dir)
    after = set(created)
    if not after:
        raise ReportingUnavailableError(
            "report generation returned without creating an artifact"
        )
    return created


def generate_comparison(
    bundle_dirs: tuple[Path, ...],
    output_dir: Path | None,
) -> tuple[str, tuple[Path, ...]]:
    """Render the cross-bundle comparison strictly from sealed bundles."""

    render = _callable("render_comparison_table")
    table = render(bundle_dirs)
    if not isinstance(table, str):
        raise ReportingUnavailableError(
            "comparison rendering did not return table text"
        )
    if output_dir is None:
        return table, ()
    write = _callable("write_comparison_files")
    write(bundle_dirs, output_dir)
    created = _created_files(output_dir)
    if not created:
        raise ReportingUnavailableError(
            "comparison generation returned without creating an artifact"
        )
    return table, created


def generate_scoreboard(
    bundle_dirs: tuple[Path, ...],
    output_dir: Path | None,
) -> tuple[str, tuple[Path, ...]]:
    """Render the WAGMI Score table strictly from sealed bundles."""

    render = _callable("render_score_table")
    table = render(bundle_dirs)
    if not isinstance(table, str):
        raise ReportingUnavailableError(
            "score rendering did not return table text"
        )
    if output_dir is None:
        return table, ()
    write = _callable("write_score_files")
    write(bundle_dirs, output_dir)
    created = _created_files(output_dir)
    if not created:
        raise ReportingUnavailableError(
            "score generation returned without creating an artifact"
        )
    return table, created


def generate_share_card(bundle_dir: Path, output_path: Path) -> Path:
    """Generate one labeled share card strictly from a sealed bundle."""

    function = _callable("render_share_card_svg")
    value = function(bundle_dir)
    if not isinstance(value, str):
        raise ReportingUnavailableError(
            "share-card generation did not return SVG text"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(value, encoding="utf-8")
    return output_path
