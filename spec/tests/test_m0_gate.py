# SPDX-License-Identifier: Apache-2.0
"""M0 exit gate — the repeatable schema + example + fixture validation pass.

This module IS the programmatic form of the M0 milestone exit proof
(Development Plan §3: "Schemas validate; fixture derivation reviewed").
Running `.venv/bin/python -m pytest spec/tests/test_m0_gate.py` at any time
re-establishes, from committed bytes alone, that:

1. Every published schema file parses, meta-validates as JSON Schema
   draft 2020-12, carries an `$id`, and self-names correctly (SCH-1).
2. No schema anywhere admits `"type": "number"` — the schema-level float
   ban (DET-4 / §0 global decisions).
3. `spec/schemas/VERSIONS.json` covers exactly the published schema majors,
   and every schema has exactly one committed example (SCH-1).
4. Every example validates against its schema (SCH-1).
5. Fixture packs (golden-mini main, golden-mini variant-liquidation,
   leakage-probe) are internally consistent: per-file sha256 / byte-count /
   record-count in `manifest.json.files[]` match the committed bytes, every
   row validates against its declared `row_schema`, and the manifest
   `content_hash` recomputes exactly per its documented derivation through
   `spec.canonical` (DET-2, DET-5-style at fixture scale, MATH-1 inputs).
6. Golden expected outputs (the MATH-1 oracle) validate against the final
   published schemas, every JSONL line is byte-identical to its JCS
   re-canonicalization (DET-2), and the full files hash to the reconciled
   values pinned in `fixtures/golden-mini/derivations/reconciliation.md`
   §8.3 — any drift of the twice-computed oracle fails here by name.
7. Instance-level float ban over every committed contract instance in
   `spec/schemas/` and `fixtures/` (VERSIONING.md instance scan; the
   historical `derivations/` record is exempt — it is evidence, not
   contract instances).

Deeper semantic checks (MATH-2 identity, funding coverage, leakage-probe
generator faithfulness, SCH-2 field reference) live in their dedicated
modules; this gate plus those modules is the full M0 CI surface.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from spec.canonical import canonical_bytes, content_hash

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "spec" / "schemas"
EXAMPLE_DIR = SCHEMA_DIR / "examples"
GOLDEN = ROOT / "fixtures" / "golden-mini"
LEAKAGE = ROOT / "fixtures" / "leakage-probe"

SCHEMA_FILES = sorted(SCHEMA_DIR.glob("*.v1.schema.json"))
SCHEMA_NAMES = sorted(p.name.removesuffix(".v1.schema.json") for p in SCHEMA_FILES)


def _reject_float(_: str) -> None:
    raise ValueError("fractional/exponent JSON number in a committed instance (DET-2)")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(), parse_float=_reject_float)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line, parse_float=_reject_float)
        for line in path.read_text().splitlines()
        if line
    ]


_schemas: dict[str, dict[str, Any]] = {
    name: json.loads((SCHEMA_DIR / f"{name}.v1.schema.json").read_text())
    for name in SCHEMA_NAMES
}
_registry = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema)) for schema in _schemas.values()
)


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(_schemas[name], registry=_registry)


def assert_valid(name: str, instance: object, ctx: str) -> None:
    errors = list(validator(name).iter_errors(instance))
    assert not errors, f"{ctx}: {[e.message for e in errors[:5]]}"


# ---------------------------------------------------------------------------
# 1 + 2. schema files: draft 2020-12 meta-validation + schema-level float ban
# ---------------------------------------------------------------------------

def test_expected_schema_set_published() -> None:
    assert SCHEMA_NAMES == [
        "action", "agent_manifest", "bar_row", "bundle_manifest", "chain",
        "decision_record", "event", "funding_row", "ledger_row", "metrics",
        "observation", "pack_manifest", "redaction", "runner_request",
        "runner_response",
    ]


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_meta_validates(name: str) -> None:
    schema = _schemas[name]
    Draft202012Validator.check_schema(schema)  # raises on an invalid schema
    assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema.get("$id"), f"{name}: missing $id"
    assert f"{name}.v1" in str(schema["$id"]), f"{name}: $id does not self-name"


def _walk(node: object) -> list[object]:
    out: list[object] = [node]
    if isinstance(node, dict):
        for v in node.values():
            out.extend(_walk(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk(v))
    return out


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_no_number_type_in_schema(name: str) -> None:
    for node in _walk(_schemas[name]):
        if isinstance(node, dict) and "type" in node:
            declared = node["type"]
            types = declared if isinstance(declared, list) else [declared]
            assert "number" not in types, f"{name}: 'number' type declared"


# ---------------------------------------------------------------------------
# 3 + 4. registry coverage + one validating example per schema
# ---------------------------------------------------------------------------

def test_versions_registry_exactly_covers_published_schemas() -> None:
    registry = load_json(SCHEMA_DIR / "VERSIONS.json")
    assert set(registry["versions"]) == {f"{n}/v1" for n in SCHEMA_NAMES}


def test_example_set_is_a_bijection_with_schemas() -> None:
    examples = sorted(p.name.removesuffix(".v1.json")
                      for p in EXAMPLE_DIR.glob("*.v1.json"))
    assert examples == SCHEMA_NAMES


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_example_validates_against_schema(name: str) -> None:
    assert_valid(name, load_json(EXAMPLE_DIR / f"{name}.v1.json"), f"examples/{name}")


# ---------------------------------------------------------------------------
# 5. fixture packs: manifest-vs-bytes integrity + row validation + content hash
# ---------------------------------------------------------------------------

PACKS = {
    "golden-main": GOLDEN / "pack",
    "golden-variant": GOLDEN / "variant-liquidation" / "pack",
    "leakage-probe": LEAKAGE,
}

ROW_SCHEMA_BY_NAME = {"bar_row/v1": "bar_row", "funding_row/v1": "funding_row"}


@pytest.mark.parametrize("pack_name", sorted(PACKS))
def test_pack_manifest_validates(pack_name: str) -> None:
    pack = PACKS[pack_name]
    assert_valid("pack_manifest", load_json(pack / "manifest.json"),
                 f"{pack_name}/manifest.json")


@pytest.mark.parametrize("pack_name", sorted(PACKS))
def test_pack_files_match_manifest_and_rows_validate(pack_name: str) -> None:
    pack = PACKS[pack_name]
    manifest = load_json(pack / "manifest.json")
    for entry in manifest["files"]:
        path = pack / entry["path"]
        raw = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        assert digest == entry["sha256"], f"{pack_name}/{entry['path']}: sha256 drift"
        assert len(raw) == entry["bytes"], f"{pack_name}/{entry['path']}: byte count drift"
        rows = load_jsonl(path)
        assert len(rows) == entry["records"], f"{pack_name}/{entry['path']}: record count drift"
        schema_name = ROW_SCHEMA_BY_NAME[entry["row_schema"]]
        for i, row in enumerate(rows):
            assert_valid(schema_name, row, f"{pack_name}/{entry['path']}:{i}")


@pytest.mark.parametrize("pack_name", sorted(PACKS))
def test_pack_content_hash_recomputes(pack_name: str) -> None:
    """content_hash = SHA-256 over the JCS bytes of
    {"schema":"pack_content/v1","files":[{path,sha256,bytes,records}...]}
    sorted by path (pack_manifest/v1 content_hash description)."""
    manifest = load_json(PACKS[pack_name] / "manifest.json")
    doc = {
        "schema": "pack_content/v1",
        "files": sorted(
            (
                {k: f[k] for k in ("path", "sha256", "bytes", "records")}
                for f in manifest["files"]
            ),
            key=lambda f: str(f["path"]),
        ),
    }
    assert content_hash(doc) == manifest["content_hash"], (
        f"{pack_name}: content_hash does not recompute from committed bytes"
    )


# ---------------------------------------------------------------------------
# 6. golden expected outputs: schema-valid, JCS-canonical, byte-pinned
# ---------------------------------------------------------------------------

EPISODES = ["main", "variant-liquidation"]

# Reconciled oracle hashes — reconciliation.md §8.3 (C0.3d). Any change to the
# twice-computed expected bytes must be a deliberate re-derivation that updates
# both the reconciliation record and this table.
ORACLE_SHA256 = {
    "main/events.jsonl":
        "66ed61031c07132aad867a4216caab0984f799334550d738cd0fc380bb8d72b6",
    "main/ledger.jsonl":
        "66204092bba019210b8f9d919b293e290b6924f0b100b87a9f66ec63a78e60e3",
    "main/ledger_stress_2x.jsonl":
        "4e41ee64b7230e693ee200ccd74956522ca6c44de95ec4b6d0368581335a73d7",
    "main/metrics.json":
        "ae7c53439fd1338553be64daa51a64a03f20b67ef60fe81b65ee887f42e10bbf",
    "variant-liquidation/events.jsonl":
        "3807e7ded28d4117c02045df3bbe02534f28439e71b0cde1c49b0f31cced2d0a",
    "variant-liquidation/ledger.jsonl":
        "88a53add1c2b7680728631f6a991750cfd97c780cf835358ac27fe9896d99001",
    "variant-liquidation/ledger_stress_2x.jsonl":
        "4402ead4f9643b54d86a3f22807e7f629703e2dd9bd8a92d8f76f82ac1e31016",
    "variant-liquidation/metrics.json":
        "d419498804501eaac46775ff5907c1390ca346c93eb2615e27020d2bea231cd0",
}

SCHEMA_BY_EXPECTED_FILE = {
    "events.jsonl": "event",
    "ledger.jsonl": "ledger_row",
    "ledger_stress_2x.jsonl": "ledger_row",
}


@pytest.mark.parametrize("rel", sorted(ORACLE_SHA256))
def test_oracle_bytes_pinned_to_reconciliation(rel: str) -> None:
    raw = (GOLDEN / "expected" / rel).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ORACLE_SHA256[rel], (
        f"expected/{rel}: oracle bytes differ from the reconciled hashes "
        "(reconciliation.md §8.3) — golden fixture drift"
    )


@pytest.mark.parametrize("episode", EPISODES)
@pytest.mark.parametrize("fname", sorted(SCHEMA_BY_EXPECTED_FILE))
def test_expected_jsonl_schema_valid_and_canonical(episode: str, fname: str) -> None:
    path = GOLDEN / "expected" / episode / fname
    schema_name = SCHEMA_BY_EXPECTED_FILE[fname]
    for i, line in enumerate(path.read_text().splitlines()):
        if not line:
            continue
        obj = json.loads(line, parse_float=_reject_float)
        assert_valid(schema_name, obj, f"{episode}/{fname}:{i}")
        assert line.encode() == canonical_bytes(obj), (
            f"{episode}/{fname}:{i}: line is not JCS-canonical (DET-2)"
        )


@pytest.mark.parametrize("episode", EPISODES)
def test_expected_metrics_schema_valid(episode: str) -> None:
    metrics = load_json(GOLDEN / "expected" / episode / "metrics.json")
    assert_valid("metrics", metrics, f"{episode}/metrics.json")
    assert metrics["claim_label"] == "survival-stress"  # LABEL-1 at the oracle
    assert set(metrics["profiles"]) == {"primary", "stress_2x"}  # MATH-5 shape


# ---------------------------------------------------------------------------
# 7. instance-level float ban over spec/ and fixtures/ committed instances
# ---------------------------------------------------------------------------

def test_float_ban_over_all_committed_instances() -> None:
    files = [
        p
        for root in (SCHEMA_DIR, GOLDEN, LEAKAGE)
        for pattern in ("*.json", "*.jsonl")
        for p in root.rglob(pattern)
        if "derivations" not in p.parts  # historical record, not instances
    ]
    assert len(files) > 40, "fixture/schema instance sweep looks incomplete"
    for path in files:
        if path.suffix == ".jsonl":
            load_jsonl(path)  # raises on any fractional/exponent number
        else:
            load_json(path)
