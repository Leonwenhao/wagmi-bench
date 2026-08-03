# SPDX-License-Identifier: Apache-2.0
"""SCH-2 CI completeness gate (M0 audit round 3, finding SCH-2-a).

SCH-2: "Every schema field carries a human-readable description; docs render a
complete field reference | [CI] completeness check". The field reference is
DEFINED as the schema-embedded descriptions, rendered to
docs/contracts/field-reference.md by spec/tools/gen_field_reference.py. This
module is the mechanical gate:

1. 100% description coverage: every field definition in every schema (property
   subschemas, $defs, described map values) carries a non-empty description.
   Conditional matcher branches (allOf/if/then/oneOf dispatch) are constraint
   grammar, not field definitions, and are exempt.
2. Rendered reference is complete and current: regenerating field-reference.md
   is byte-identical to the committed file, and every property name that occurs
   anywhere in any schema appears in the render. A schema edit without a doc
   regen fails here.
3. Reverse-drift: every backticked field path in a `Field`-headed table of the
   curated IC-1..IC-6 docs resolves to real schema vocabulary (no phantom or
   renamed fields in the narrative docs).
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO / "spec" / "schemas"
DOCS_DIR = REPO / "docs" / "contracts"
REFERENCE = DOCS_DIR / "field-reference.md"

sys.path.insert(0, str(REPO))
from spec.tools.gen_field_reference import render, walk_fields  # noqa: E402

SCHEMA_FILES = sorted(SCHEMAS_DIR.glob("*.schema.json"))


def _load(p: pathlib.Path) -> dict[str, Any]:
    data = json.loads(p.read_text())
    assert isinstance(data, dict)
    return data


def test_schema_files_exist() -> None:
    assert len(SCHEMA_FILES) >= 15


def test_every_field_definition_has_nonempty_description() -> None:
    """SCH-2 clause 1, mechanically: walk every schema's field definitions."""
    missing: list[str] = []
    for schema_file in SCHEMA_FILES:
        schema = _load(schema_file)
        assert schema.get("description", "").strip(), f"{schema_file.name}: schema root lacks description"
        for path, sub, _flag in walk_fields(schema):
            if not str(sub.get("description", "")).strip():
                missing.append(f"{schema_file.name}: {path}")
    assert not missing, (
        "Field definitions without a description (SCH-2): " + ", ".join(missing)
    )


def test_field_reference_render_is_byte_identical_to_committed() -> None:
    """SCH-2 clause 2 (the [CI] completeness check): docs render a complete
    field reference, and the committed render is current."""
    assert REFERENCE.exists(), (
        "docs/contracts/field-reference.md missing - run "
        ".venv/bin/python spec/tools/gen_field_reference.py"
    )
    committed = REFERENCE.read_text(encoding="utf-8")
    assert committed == render(), (
        "docs/contracts/field-reference.md is stale relative to the schemas. "
        "Regenerate: .venv/bin/python spec/tools/gen_field_reference.py"
    )


def _all_property_names(node: Any, names: set[str]) -> None:
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            names.update(k for k in props if isinstance(k, str))
        for v in node.values():
            _all_property_names(v, names)
    elif isinstance(node, list):
        for v in node:
            _all_property_names(v, names)


def _schema_vocabulary() -> set[str]:
    """Every property name, $defs name, and enum/const string value across schemas."""
    vocab: set[str] = set()
    for schema_file in SCHEMA_FILES:
        schema = _load(schema_file)
        _all_property_names(schema, vocab)
        defs = schema.get("$defs")
        if isinstance(defs, dict):
            vocab.update(defs.keys())

        def enums(node: Any) -> None:
            if isinstance(node, dict):
                for v in node.get("enum", []) if isinstance(node.get("enum"), list) else []:
                    if isinstance(v, str):
                        vocab.add(v)
                if isinstance(node.get("const"), str):
                    vocab.add(node["const"])
                for v in node.values():
                    enums(v)
            elif isinstance(node, list):
                for v in node:
                    enums(v)

        enums(schema)
    return vocab


def test_field_reference_mentions_every_schema_property() -> None:
    """Independent completeness cross-check: no schema property (even ones that
    only occur inside conditional branches) is absent from the render."""
    text = REFERENCE.read_text(encoding="utf-8")
    tokens = set(re.findall(r"[A-Za-z0-9_$]+", text))
    names: set[str] = set()
    for schema_file in SCHEMA_FILES:
        _all_property_names(_load(schema_file), names)
    missing = sorted(n for n in names if n not in tokens)
    assert not missing, f"Schema properties absent from field-reference.md: {missing}"


_FIELD_PATH_RE = re.compile(r"^[a-z_][a-z0-9_]*(\[\])?([.*/][a-z0-9_*]+(\[\])?)*$")


def test_ic_doc_field_tables_have_no_phantom_fields() -> None:
    """Reverse-drift gate: backticked field paths in `Field`-headed tables of
    the curated IC docs must resolve to real schema vocabulary."""
    vocab = _schema_vocabulary()
    phantoms: list[str] = []
    for doc in sorted(DOCS_DIR.glob("IC-*.md")):
        in_field_table = False
        for line in doc.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                in_field_table = False
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not cells:
                continue
            first = cells[0]
            if first in ("Field", "**Field**"):
                in_field_table = True
                continue
            if set(first) <= set("-: ") or not in_field_table:
                continue
            for tick in re.findall(r"`([^`]+)`", first):
                if not _FIELD_PATH_RE.match(tick):
                    continue  # not a plain field path (schema names, codes, etc.)
                parts = [p for p in re.split(r"[.\[\]/*]+", tick) if p]
                for part in parts:
                    if part not in vocab:
                        phantoms.append(f"{doc.name}: `{tick}` (unknown segment '{part}')")
    assert not phantoms, (
        "IC doc field tables reference non-schema fields (drift): " + "; ".join(phantoms)
    )
