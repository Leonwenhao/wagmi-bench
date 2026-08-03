#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Render docs/contracts/field-reference.md from the spec/schemas/*.schema.json bytes.

This is the SCH-2 "complete field reference" renderer (M0 audit round 3, finding
SCH-2-a). The normative field reference IS the set of schema-embedded
`description` strings; this tool renders them into one human-facing document with
one row per field definition. CI (spec/tests/test_field_reference.py) enforces:

1. description coverage - every field definition in every schema carries a
   non-empty description (independent walker, not this renderer);
2. completeness + currency - regenerating this document is byte-identical to the
   committed docs/contracts/field-reference.md, so a schema edit without a doc
   regen fails CI, and every schema property name appears in the render;
3. reverse-drift - every backticked field path in the IC-1..IC-6 curated docs'
   `Field`-headed tables names a real schema property.

Deterministic: output depends only on the schema file bytes (insertion order of
JSON members is preserved by json.load and is itself under version control).

Usage: gen_field_reference.py [output_file]
       (default: docs/contracts/field-reference.md)
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO / "spec" / "schemas"
DEFAULT_OUT = REPO / "docs" / "contracts" / "field-reference.md"

# Keywords whose subschemas are constraint MATCHERS (conditional dispatch /
# variant grammars), not field definitions. The walker never emits rows for
# them; the owning field's row carries the semantics.
_NON_FIELD_BRANCHES = ("allOf", "anyOf", "oneOf", "if", "then", "else", "not")


def _cell(text: str) -> str:
    """Escape a description for a one-line markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _type_of(schema: dict[str, Any]) -> str:
    """Compact human-readable type label for one field definition."""
    if "$ref" in schema:
        return f"ref: `{schema['$ref'].rsplit('/', 1)[-1]}`"
    if "const" in schema:
        return f"const `{json.dumps(schema['const'])}`"
    if "enum" in schema:
        return "enum: " + " \\| ".join(f"`{json.dumps(v)}`" for v in schema["enum"])
    t = schema.get("type")
    if isinstance(t, list):
        label = " \\| ".join(t)
    elif isinstance(t, str):
        label = t
    else:
        for kw in _NON_FIELD_BRANCHES:
            if kw in schema:
                return f"({kw} variants)"
        return "(any)"
    if t == "array" or (isinstance(t, list) and "array" in t):
        items = schema.get("items")
        if isinstance(items, dict):
            label += f" of {_type_of(items).replace('ref: ', '')}"
    if t == "object" and isinstance(schema.get("additionalProperties"), dict):
        label += " (map)"
    return label


def _conditional_required(schema: dict[str, Any]) -> set[str]:
    """Names required only inside conditional branches (if/then/else, allOf)."""
    found: set[str] = set()

    def scan(node: Any, top: bool) -> None:
        if isinstance(node, dict):
            if not top and isinstance(node.get("required"), list):
                found.update(x for x in node["required"] if isinstance(x, str))
            for kw in _NON_FIELD_BRANCHES:
                sub = node.get(kw)
                if isinstance(sub, dict):
                    scan(sub, top=False)
                elif isinstance(sub, list):
                    for s in sub:
                        scan(s, top=False)

    scan(schema, top=True)
    return found


def _req_flag(name: str, parent: dict[str, Any]) -> str:
    if name in parent.get("required", []):
        return "required"
    if name in _conditional_required(parent):
        return "conditional"
    return "optional"


def walk_fields(schema: dict[str, Any], base: str = "") -> list[tuple[str, dict[str, Any], str]]:
    """Yield (path, field_schema, required_flag) for every field definition.

    Field definitions are: entries of `properties` (recursively), `$defs`
    members, array `items` element fields, described map-value schemas
    (`additionalProperties` given as an object with its own description or
    properties), and properties introduced by `anyOf`/`oneOf` object variants
    (the nullable-object pattern) that are not already defined in the owning
    node's own `properties` block. Conditional matcher branches (`if`/`then`,
    and variant members that merely re-match canonical properties, e.g. payload
    dispatch) are never emitted.
    """
    out: list[tuple[str, dict[str, Any], str]] = []

    def recurse(node: dict[str, Any], path: str) -> None:
        props = node.get("properties")
        own_names = set(props.keys()) if isinstance(props, dict) else set()
        if isinstance(props, dict):
            for name, sub in props.items():
                if not isinstance(sub, dict):
                    continue
                p = f"{path}.{name}" if path else name
                out.append((p, sub, _req_flag(name, node)))
                recurse(sub, p)
        for kw in ("anyOf", "oneOf"):
            variants = node.get(kw)
            if not isinstance(variants, list):
                continue
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                vprops = variant.get("properties")
                if not isinstance(vprops, dict):
                    continue
                for name, sub in vprops.items():
                    if name in own_names or not isinstance(sub, dict):
                        continue
                    p = f"{path}.{name}" if path else name
                    out.append((p, sub, _req_flag(name, variant)))
                    recurse(sub, p)
        items = node.get("items")
        if isinstance(items, dict):
            recurse(items, f"{path}[]" if path else "[]")
        ap = node.get("additionalProperties")
        if isinstance(ap, dict) and ("description" in ap or "properties" in ap):
            p = f"{path}.*" if path else "*"
            out.append((p, ap, "map value"))
            recurse(ap, p)

    recurse(schema, base)
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        for name, sub in defs.items():
            if not isinstance(sub, dict):
                continue
            p = f"$defs/{name}"
            out.append((p, sub, "definition"))
            recurse(sub, p)
    return out


def render() -> str:
    lines: list[str] = []
    lines.append("# Complete Field Reference (generated — do not edit by hand)")
    lines.append("")
    lines.append(
        "**Generated by `spec/tools/gen_field_reference.py` from the "
        "`spec/schemas/*.schema.json` bytes.** This document is the SCH-2 "
        "\"complete field reference\": the schema-embedded `description` strings are "
        "the normative reference, and this file renders every one of them — one row "
        "per field definition, including nested fields, array element fields, map "
        "values, and `$defs`. CI (`spec/tests/test_field_reference.py`) asserts "
        "(1) 100% description coverage in the schemas, (2) that regenerating this "
        "file is byte-identical to the committed copy and covers every schema "
        "property (so schema/doc drift fails loudly), and (3) reverse-drift: every "
        "backticked field path in the curated IC-1..IC-6 field tables names a real "
        "schema property. The curated IC docs remain the architecture narrative; "
        "THIS file is the exhaustive reference. Regenerate after any schema change: "
        "`.venv/bin/python spec/tools/gen_field_reference.py`."
    )
    lines.append("")
    lines.append(
        "Required column: `required` (unconditional), `conditional` (required only "
        "inside an `if/then` branch — e.g. the IC-3 `intent_kind` seam), `optional`, "
        "`map value` (schema of every value of a map field), `definition` (a named "
        "`$defs` building block)."
    )
    for schema_file in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        schema = json.loads(schema_file.read_text())
        title = schema.get("title", schema_file.name)
        lines.append("")
        lines.append(f"## `{title}` — `spec/schemas/{schema_file.name}`")
        lines.append("")
        desc = schema.get("description", "")
        if desc:
            lines.append(_cell(desc))
            lines.append("")
        rows = walk_fields(schema)
        lines.append("| Field path | Type | Required | Description |")
        lines.append("|---|---|---|---|")
        for path, sub, flag in rows:
            lines.append(
                f"| `{path}` | {_type_of(sub)} | {flag} | {_cell(sub.get('description', ''))} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.write_text(render(), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
