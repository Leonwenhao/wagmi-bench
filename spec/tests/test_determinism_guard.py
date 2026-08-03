# SPDX-License-Identifier: Apache-2.0
"""AST determinism guard for the ``spec`` package (DET-4).

``core/tests/test_determinism_guard.py`` lints ``core/*.py`` for ambient
wall-clock, RNG, locale and binary-float constructs.  ``spec`` sits on exactly
the same determinism contract -- it canonicalises the bytes that every content
hash, manifest digest and bundle signature is taken over -- so it is linted the
same way here.  ``spec`` is clean today; this guard exists to keep it clean.

The lint is deliberately mechanical (no import of the modules under test) so it
also covers code paths that are never executed by the rest of the suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "spec"

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

#: Fully dotted call targets that read a wall clock or a nondeterministic
#: entropy source.  Matched against the literal dotted source text so an
#: unrelated ``foo.now()`` method on a project object is not flagged.
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
    """Every non-test ``spec`` module, including the generator tools."""
    modules = [
        path
        for path in sorted(SPEC.rglob("*.py"))
        if "tests" not in path.relative_to(SPEC).parts
    ]
    assert modules, "spec runtime module discovery found nothing"
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


def test_spec_modules_have_no_ambient_time_random_or_locale_imports() -> None:
    offenders: list[str] = []
    for path in _runtime_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.partition(".")[0] for alias in node.names}
                for root in sorted(roots & PROHIBITED_IMPORT_ROOTS):
                    offenders.append(f"{relative}:{node.lineno}: import {root}")
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                root = node.module.partition(".")[0]
                if root in PROHIBITED_IMPORT_ROOTS:
                    offenders.append(
                        f"{relative}:{node.lineno}: from {root} import ..."
                    )
    assert not offenders, offenders


def test_spec_modules_never_call_a_wall_clock_or_entropy_source() -> None:
    offenders: list[str] = []
    for path in _runtime_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(ROOT)
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


def test_spec_modules_have_no_binary_float_literals_or_casts() -> None:
    offenders: list[str] = []
    for path in _runtime_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(ROOT)
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


def test_guard_discovers_the_canonicaliser_and_skips_its_own_tests() -> None:
    discovered = {path.relative_to(ROOT).as_posix() for path in _runtime_modules()}

    assert "spec/canonical.py" in discovered
    assert "spec/tools/gen_field_reference.py" in discovered
    assert not any(part.startswith("spec/tests/") for part in discovered)


def test_guard_detects_planted_violations() -> None:
    """The lint predicates must actually fire on the constructs they ban."""
    imports = ast.parse("import time\nfrom zoneinfo import ZoneInfo\n")
    import_roots = {
        alias.name.partition(".")[0]
        for node in ast.walk(imports)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.partition(".")[0]
        for node in ast.walk(imports)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert import_roots <= PROHIBITED_IMPORT_ROOTS

    calls = ast.parse("time.time()\nrandom.choice(x)\ndatetime.datetime.now()\n")
    dotted = [
        _dotted_name(node.func)
        for node in ast.walk(calls)
        if isinstance(node, ast.Call)
    ]
    assert dotted == ["time.time", "random.choice", "datetime.datetime.now"]
    for name in dotted:
        assert name is not None
        assert (
            name in PROHIBITED_CALL_TARGETS
            or name.partition(".")[0] in PROHIBITED_CALL_ROOTS
        )

    # A project object that merely spells a banned attribute is not a hit.
    benign = ast.parse("pack.now()\nself.random_seed_bytes()\n")
    benign_dotted = [
        _dotted_name(node.func)
        for node in ast.walk(benign)
        if isinstance(node, ast.Call)
    ]
    for name in benign_dotted:
        assert name is not None
        assert name not in PROHIBITED_CALL_TARGETS
        assert name.partition(".")[0] not in PROHIBITED_CALL_ROOTS
