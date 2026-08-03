# SPDX-License-Identifier: Apache-2.0
"""AST determinism guard for the ``data`` package (DET-4).

Deny-list breadth is aligned with ``core/tests/test_determinism_guard.py``:
ambient wall-clock / RNG / locale imports, wall-clock and entropy call targets,
and binary-float literals or casts.

One documented deviation from ``core``: ``core`` bans the ``datetime`` import
root outright, but ``data/catalog.py`` legitimately uses ``datetime.date`` and
``datetime.timedelta`` as *pure calendar value types* to spell the frozen pack
windows (``date(2020, 3, 5)``) and to enumerate the months an archive fetch
covers.  Those are constructed from literals and never read a clock.  The
deviation is therefore pinned rather than waived:

* only ``data/catalog.py`` may import ``datetime`` at all,
* only the names ``date`` and ``timedelta`` may be imported from it, so the
  ``datetime.datetime`` class (whose ``now``/``utcnow`` are the actual hazard)
  is unreachable by name, and
* the wall-clock call ban below still applies to the whole package, so
  ``date.today()`` is rejected everywhere including ``data/catalog.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

#: Import roots that hand a module ambient, machine-dependent state.
PROHIBITED_IMPORT_ROOTS = frozenset(
    {
        "datetime",
        "locale",
        "random",
        "secrets",
        "time",
        "uuid",
        "zoneinfo",
    }
)

#: The single pinned deviation from ``core``'s deny-list; see module docstring.
CALENDAR_VALUE_EXEMPTION = "data/catalog.py"
CALENDAR_VALUE_NAMES = frozenset({"date", "timedelta"})

#: Fully dotted call targets that read a wall clock or an entropy source.
PROHIBITED_CALL_TARGETS = frozenset(
    {
        "date.today",
        "datetime.date.today",
        "datetime.datetime.now",
        "datetime.datetime.today",
        "datetime.datetime.utcfromtimestamp",
        "datetime.datetime.utcnow",
        "datetime.now",
        "datetime.today",
        "datetime.utcfromtimestamp",
        "datetime.utcnow",
        "os.urandom",
        "secrets.token_bytes",
        "secrets.token_hex",
        "secrets.token_urlsafe",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "time.process_time",
        "time.time",
        "time.time_ns",
        "uuid.uuid1",
        "uuid.uuid4",
    }
)

#: Roots whose *every* attribute is a nondeterminism source.
PROHIBITED_CALL_ROOTS = frozenset({"random"})


def _runtime_modules() -> list[Path]:
    """Every non-test ``data`` module."""
    modules = [
        path
        for path in sorted(DATA.rglob("*.py"))
        if "tests" not in path.relative_to(DATA).parts
    ]
    assert modules, "data runtime module discovery found nothing"
    return modules


def _dotted_name(node: ast.expr) -> str | None:
    """Render ``a.b.c`` attribute chains as ``"a.b.c"``; ``None`` otherwise."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def test_data_modules_have_no_ambient_time_random_or_locale_imports() -> None:
    offenders: list[str] = []
    for path in _runtime_modules():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.partition(".")[0]
                    if root in PROHIBITED_IMPORT_ROOTS:
                        offenders.append(f"{relative}:{node.lineno}: import {root}")
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                root = node.module.partition(".")[0]
                if root not in PROHIBITED_IMPORT_ROOTS:
                    continue
                imported = {alias.name for alias in node.names}
                permitted = (
                    root == "datetime"
                    and relative == CALENDAR_VALUE_EXEMPTION
                    and imported <= CALENDAR_VALUE_NAMES
                )
                if not permitted:
                    offenders.append(
                        f"{relative}:{node.lineno}: from {root} import "
                        f"{', '.join(sorted(imported))}"
                    )
    assert not offenders, offenders


def test_calendar_value_exemption_stays_exactly_one_pinned_import() -> None:
    """The lone deviation from ``core``'s deny-list must not silently grow."""
    exempt_uses: list[str] = []
    for path in _runtime_modules():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.partition(".")[0] == "datetime"
            ):
                exempt_uses.append(relative)

    assert exempt_uses == [CALENDAR_VALUE_EXEMPTION], exempt_uses


def test_data_modules_never_call_a_wall_clock_or_entropy_source() -> None:
    offenders: list[str] = []
    for path in _runtime_modules():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted_name(node.func)
            if dotted is None:
                continue
            if (
                dotted in PROHIBITED_CALL_TARGETS
                or dotted.partition(".")[0] in PROHIBITED_CALL_ROOTS
            ):
                offenders.append(f"{relative}:{node.lineno}: {dotted}()")
    assert not offenders, offenders


def test_data_modules_have_no_binary_float_literals_or_casts() -> None:
    offenders: list[str] = []
    for path in _runtime_modules():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"{relative}:{node.lineno}: float literal")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                offenders.append(f"{relative}:{node.lineno}: float() cast")
    assert not offenders, offenders


def test_guard_discovers_every_data_runtime_module() -> None:
    discovered = {path.relative_to(ROOT).as_posix() for path in _runtime_modules()}

    assert {
        "data/__init__.py",
        "data/binance.py",
        "data/builder.py",
        "data/catalog.py",
        "data/distribution_guard.py",
        "data/nightly.py",
        "data/validator.py",
    } <= discovered
    assert not any(path.startswith("data/tests/") for path in discovered)


def test_guard_detects_planted_violations() -> None:
    """The widened predicates must actually fire on the constructs they ban."""
    planted = ast.parse(
        "import time\n"
        "import locale\n"
        "from zoneinfo import ZoneInfo\n"
        "from datetime import datetime\n"
    )
    banned: list[str] = []
    for node in ast.walk(planted):
        if isinstance(node, ast.Import):
            banned.extend(
                alias.name
                for alias in node.names
                if alias.name.partition(".")[0] in PROHIBITED_IMPORT_ROOTS
            )
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.partition(".")[0] in PROHIBITED_IMPORT_ROOTS:
                banned.append(node.module)
    assert banned == ["time", "locale", "zoneinfo", "datetime"]

    # ``from datetime import datetime`` is *not* covered by the pinned
    # calendar-value exemption even inside ``data/catalog.py``.
    assert not {"datetime"} <= CALENDAR_VALUE_NAMES

    calls = ast.parse("date.today()\nrandom.shuffle(x)\ntime.monotonic()\n")
    dotted = [
        _dotted_name(node.func)
        for node in ast.walk(calls)
        if isinstance(node, ast.Call)
    ]
    assert dotted == ["date.today", "random.shuffle", "time.monotonic"]
    for name in dotted:
        assert name is not None
        assert (
            name in PROHIBITED_CALL_TARGETS
            or name.partition(".")[0] in PROHIBITED_CALL_ROOTS
        )

    floats = ast.parse("x = 1.5\ny = float(z)\n")
    hits = [
        node
        for node in ast.walk(floats)
        if (isinstance(node, ast.Constant) and isinstance(node.value, float))
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        )
    ]
    assert len(hits) == 2
