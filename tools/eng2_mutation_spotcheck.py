# SPDX-License-Identifier: Apache-2.0
"""Kill three bounded money-path mutants in an isolated temporary copy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SpotCheckError(RuntimeError):
    """The mutation harness failed or a seeded mutant survived."""


@dataclass(frozen=True, slots=True)
class Mutant:
    name: str
    function: str
    old: str
    new: str
    tests: tuple[str, ...]


MUTANTS = (
    Mutant(
        name="funding-sign",
        function="funding_cash_flow_micro",
        old="    return -venue_amount\n",
        new="    return venue_amount\n",
        tests=(
            "core/tests/test_math.py::"
            "test_pnl_funding_margin_and_liquidation_match_golden_anchors",
            "core/tests/test_math.py::test_costs_round_up_and_credits_round_down",
        ),
    ),
    Mutant(
        name="maintenance-margin-rounding",
        function="maintenance_margin_micro",
        old="    return ceil_fraction(\n",
        new="    return floor_fraction(\n",
        tests=(
            "core/tests/test_math.py::"
            "test_pnl_funding_margin_and_liquidation_match_golden_anchors",
        ),
    ),
    Mutant(
        name="ledger-balance-bypass",
        function="ledger_delta",
        old="    result.require_balanced()\n",
        new="    # Seeded mutant: balance enforcement bypassed.\n",
        tests=(
            "core/tests/test_math.py::"
            "test_delta_helpers_reject_an_unbalanced_ledger_row",
        ),
    ),
)


def _copy_fixture(sandbox: Path) -> None:
    paths = (
        "core/__init__.py",
        "core/math.py",
        "core/models.py",
        "core/tests/__init__.py",
        "core/tests/test_math.py",
    )
    for relative in paths:
        source = ROOT / relative
        destination = sandbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copytree(
        ROOT / "fixtures" / "golden-mini" / "expected",
        sandbox / "fixtures" / "golden-mini" / "expected",
    )


def _apply_mutant(source: str, mutant: Mutant) -> str:
    start = source.find(f"def {mutant.function}(")
    if start < 0:
        raise SpotCheckError(
            f"{mutant.name}: function {mutant.function!r} was not found"
        )
    end = source.find("\ndef ", start + 1)
    if end < 0:
        end = len(source)
    function_source = source[start:end]
    if function_source.count(mutant.old) != 1:
        raise SpotCheckError(
            f"{mutant.name}: mutation target was not unique in "
            f"{mutant.function}()"
        )
    mutated = function_source.replace(mutant.old, mutant.new, 1)
    return source[:start] + mutated + source[end:]


def _pytest(
    sandbox: Path,
    tests: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *tests,
        ],
        cwd=sandbox,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_baseline(sandbox: Path) -> None:
    tests = tuple(
        dict.fromkeys(
            test
            for mutant in MUTANTS
            for test in mutant.tests
        )
    )
    completed = _pytest(sandbox, tests)
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr).strip()
        raise SpotCheckError(
            "unmutated targeted baseline did not pass\n" + detail
        )
    print(f"BASELINE passed {len(tests)} targeted tests")


def _run_mutant(sandbox: Path, mutant: Mutant, original: str) -> None:
    math_path = sandbox / "core" / "math.py"
    math_path.write_text(
        _apply_mutant(original, mutant),
        encoding="utf-8",
    )
    completed = _pytest(sandbox, mutant.tests)
    if completed.returncode != 1 or "failed" not in completed.stdout:
        detail = (completed.stdout + completed.stderr).strip()
        raise SpotCheckError(
            f"{mutant.name}: expected a targeted pytest failure, got exit "
            f"{completed.returncode}\n{detail}"
        )
    print(f"KILLED {mutant.name}")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(
            prefix="tradeevolve-eng2-mutants-"
        ) as temporary:
            sandbox = Path(temporary)
            _copy_fixture(sandbox)
            original = (sandbox / "core" / "math.py").read_text(
                encoding="utf-8"
            )
            _run_baseline(sandbox)
            for mutant in MUTANTS:
                _run_mutant(sandbox, mutant, original)
    except (OSError, SpotCheckError, subprocess.SubprocessError) as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 1
    print(f"PASS killed {len(MUTANTS)} of {len(MUTANTS)} seeded mutants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
