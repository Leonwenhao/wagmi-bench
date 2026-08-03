# SPDX-License-Identifier: Apache-2.0
"""CI instance checks for the M0 contract package (audit round 1).

Locks the published schemas, their examples, and the fixture bytes together:

1. Every `spec/schemas/examples/*.json` validates against its schema.
2. Every golden-mini expected file validates against the FINAL published
   schemas (event/v1, ledger_row/v1, metrics/v1) — the SCH-1/MATH-1 fix's
   regression guard: the MATH-1 oracle must stay mechanically diffable.
3. The MATH-2 delta-NAV identity holds on the committed golden ledger bytes.
4. Instance-level float ban (VERSIONING.md / DET-2): every committed JSON
   contract instance parses under a float-rejecting hook — closes the `1.0`
   lexical gap the JSON-Schema `integer` type cannot see.
5. Schema-tightening regressions from the audit: fractional numbers in `ext`
   are schema-invalid (SCH-1/DET-2), credential-bearing endpoint_domains
   entries are schema-invalid (SEC-1), and a decision_record whose `meant`
   status contradicts its action/rejected nullability is schema-invalid.

Run with the project venv: `.venv/bin/python -m pytest spec/tests`.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "spec" / "schemas"
EXAMPLE_DIR = SCHEMA_DIR / "examples"
GOLDEN = ROOT / "fixtures" / "golden-mini"
LEAKAGE = ROOT / "fixtures" / "leakage-probe"

SCHEMA_NAMES = [
    "action", "agent_manifest", "bar_row", "bundle_manifest", "chain",
    "decision_record", "event", "funding_row", "ledger_row", "metrics",
    "observation", "pack_manifest", "redaction", "runner_request",
    "runner_response",
]


def _reject_float(_: str) -> None:
    raise ValueError("fractional/exponent JSON number in a hashed document (DET-2)")


def load_json(path: Path) -> Any:
    """Float-rejecting parse: any number with a fractional part or exponent
    lexeme raises, including integral-valued `1.0` (VERSIONING.md instance scan)."""
    return json.loads(path.read_text(), parse_float=_reject_float)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line, parse_float=_reject_float)
        for line in path.read_text().splitlines()
        if line
    ]


_schemas = {name: json.loads((SCHEMA_DIR / f"{name}.v1.schema.json").read_text())
            for name in SCHEMA_NAMES}
_registry = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema)) for schema in _schemas.values()
)


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(_schemas[name], registry=_registry)


def assert_valid(name: str, instance: object, ctx: str) -> None:
    errors = list(validator(name).iter_errors(instance))
    assert not errors, f"{ctx}: {[e.message for e in errors[:5]]}"


def assert_invalid(name: str, instance: object, ctx: str) -> None:
    assert not validator(name).is_valid(instance), f"{ctx}: unexpectedly valid"


# ---------------------------------------------------------------------------
# 1. examples validate against their schemas
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_example_validates(name: str) -> None:
    assert_valid(name, load_json(EXAMPLE_DIR / f"{name}.v1.json"), f"examples/{name}")


def test_versions_registry_covers_every_schema() -> None:
    versions = load_json(SCHEMA_DIR / "VERSIONS.json")["versions"]
    assert set(versions) == {f"{n}/v1" for n in SCHEMA_NAMES}
    example = load_json(EXAMPLE_DIR / "bundle_manifest.v1.json")
    assert example["spec_versions"] == versions


# ---------------------------------------------------------------------------
# 2 + 3. golden-mini expected files: final schemas + MATH-2 identity
# ---------------------------------------------------------------------------

EPISODES = ["main", "variant-liquidation"]
LEDGER_FILES = ["ledger.jsonl", "ledger_stress_2x.jsonl"]


# The golden events.jsonl is a defined ECONOMIC PROJECTION of the IC-4 stream,
# not a full-lifecycle stream (scenario.md "Event-stream scope"; IC-4 design
# decision 10): the fixture stores no observation/raw artifacts, so
# ObservationEmitted / AgentResponded / MarginUpdate are out of scope and seq
# is renumbered contiguously over the asserted subset. This test locks the
# projection in as DEFINED scope: only asserted types may appear.
GOLDEN_EVENT_SCOPE = {
    "ActionParsed", "ActionRejected", "RiskCheck", "OrderFilled",
    "OrderCancelled", "FundingApplied", "NearLiquidation",
    "LiquidationTriggered", "EpisodeEnd",
}
GOLDEN_EVENT_OMITTED = {"ObservationEmitted", "AgentResponded", "MarginUpdate"}


@pytest.mark.parametrize("episode", EPISODES)
def test_golden_events_validate(episode: str) -> None:
    events = load_jsonl(GOLDEN / "expected" / episode / "events.jsonl")
    for ev in events:
        assert_valid("event", ev, f"{episode}/events.jsonl seq={ev.get('seq')}")
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert events[-1]["type"] == "EpisodeEnd"
    types = {e["type"] for e in events}
    assert types <= GOLDEN_EVENT_SCOPE, types - GOLDEN_EVENT_SCOPE
    assert not (types & GOLDEN_EVENT_OMITTED)
    # Every ActionParsed carries the canonical intent discriminator (IC-3 seam).
    for e in events:
        if e["type"] == "ActionParsed":
            assert e["payload"]["intent_kind"] == "leverage_target"


def test_golden_funding_coverage() -> None:
    """MATH-1 audit round 2: the oracle pins a NONZERO, UNCAPPED funding
    magnitude on a held position (not only the zero-position edge and the
    at-cap print), plus a negative-rate settlement paid by a short."""
    events = load_jsonl(GOLDEN / "expected" / "main" / "events.jsonl")
    manifest = load_json(GOLDEN / "pack" / "manifest.json")
    cap = manifest["markets"]["BTC"]["funding"]["cap_1e8"]
    fund = [e["payload"] for e in events if e["type"] == "FundingApplied"]
    assert any(
        0 < abs(f["rate_1e8"]) < cap
        and f["position_qty_base_1e8"] != 0
        and f["amount_micro"] != 0
        for f in fund
    ), "no normal-rate funding on a live position in the golden oracle"
    assert any(abs(f["rate_1e8"]) == cap and f["amount_micro"] != 0 for f in fund)
    assert any(
        f["rate_1e8"] < 0 and f["position_qty_base_1e8"] < 0 and f["amount_micro"] < 0
        for f in fund
    ), "no negative-rate settlement paid by a held short"


@pytest.mark.parametrize("episode", EPISODES)
@pytest.mark.parametrize("fname", LEDGER_FILES)
def test_golden_ledger_validates_and_math2(episode: str, fname: str) -> None:
    rows = load_jsonl(GOLDEN / "expected" / episode / fname)
    prev = None
    for row in rows:
        assert_valid("ledger_row", row, f"{episode}/{fname} bar={row.get('bar_index')}")
        # MATH-2: the stored attribution terms sum to the stored delta-NAV.
        assert row["d_nav_micro"] == (
            row["d_price_pnl_micro"] + row["d_funding_micro"]
            + row["d_fees_micro"] + row["d_liq_penalty_micro"]
        ), f"{episode}/{fname} bar={row['bar_index']}: MATH-2 identity broken"
        if prev is not None:
            assert row["nav_micro"] - prev["nav_micro"] == row["d_nav_micro"]
        prev = row


@pytest.mark.parametrize("episode", EPISODES)
def test_golden_metrics_validate(episode: str) -> None:
    assert_valid("metrics", load_json(GOLDEN / "expected" / episode / "metrics.json"),
                 f"{episode}/metrics.json")


@pytest.mark.parametrize("pack", [GOLDEN / "pack", GOLDEN / "variant-liquidation" / "pack",
                                  LEAKAGE])
def test_fixture_packs_validate(pack: Path) -> None:
    assert_valid("pack_manifest", load_json(pack / "manifest.json"), str(pack))
    for series in pack.glob("bars_*.jsonl"):
        for row in load_jsonl(series):
            assert_valid("bar_row", row, f"{series}")
    for series in list(pack.glob("mark_*.jsonl")) + list(pack.glob("index_*.jsonl")):
        for row in load_jsonl(series):
            assert_valid("bar_row", row, f"{series}")
    for row in load_jsonl(pack / "funding.jsonl"):
        assert_valid("funding_row", row, f"{pack}/funding.jsonl")


# ---------------------------------------------------------------------------
# 4. instance-level float ban over every committed contract instance
# ---------------------------------------------------------------------------

def test_no_fractional_numbers_in_any_committed_instance() -> None:
    files = (
        list(EXAMPLE_DIR.glob("*.json"))
        + [SCHEMA_DIR / "VERSIONS.json"]
        + list((GOLDEN / "expected").rglob("*.json*"))
        + list((GOLDEN / "pack").glob("*.json*"))
        + list((GOLDEN / "variant-liquidation" / "pack").glob("*.json*"))
        + [GOLDEN / "episode_config.json", GOLDEN / "actions.jsonl"]
        + list(LEAKAGE.glob("*.json*"))
    )
    assert len(files) > 30
    for path in files:
        if path.suffix == ".jsonl":
            load_jsonl(path)  # raises on any fractional/exponent number
        else:
            load_json(path)


# ---------------------------------------------------------------------------
# 5. audit-fix schema regressions
# ---------------------------------------------------------------------------

EXT_SCHEMAS = ["action", "agent_manifest", "bundle_manifest", "decision_record",
               "event", "pack_manifest"]


@pytest.mark.parametrize("name", EXT_SCHEMAS)
def test_fractional_ext_value_is_schema_invalid(name: str) -> None:
    base = copy.deepcopy(load_json(EXAMPLE_DIR / f"{name}.v1.json"))
    assert isinstance(base, dict)
    base["ext"] = {"x_weight": 1.5}
    # jsonschema sees the float; the schema's ext_value union must reject it.
    assert_invalid(name, base, f"{name}.ext fractional number")
    base["ext"] = {"x_nested": {"deep": [{"x": 0.25}]}}
    assert_invalid(name, base, f"{name}.ext nested fractional number")
    base["ext"] = {"x_ok": ["0.25", 3, None, True, {"k": -7}]}
    assert_valid(name, base, f"{name}.ext JCS-safe values")


def test_endpoint_domains_reject_credentials_and_urls() -> None:
    base = copy.deepcopy(load_json(EXAMPLE_DIR / "agent_manifest.v1.json"))
    for bad in ["user:pass@evil.com/steal?token=abc", "https://api.example.com",
                "api.example.com/v1", "api.example.com:443", "a@b.com",
                "API.Example.com", "localhost", "-bad.example.com"]:
        base["endpoint_domains"] = [bad]
        assert_invalid("agent_manifest", base, f"endpoint_domains {bad!r}")
    base["endpoint_domains"] = ["api.anthropic.com", "generativelanguage.googleapis.com"]
    assert_valid("agent_manifest", base, "endpoint_domains bare hostnames")


def test_action_intent_kind_seam() -> None:
    """IC-3 seam (M0 audit round 2): `target` is conditionally required behind
    the optional `intent_kind` discriminator, and in v1 that condition is
    always in force (absence == 'leverage_target'), so an action without a
    target is still schema-invalid; unknown intent kinds are schema-invalid."""
    base = copy.deepcopy(load_json(EXAMPLE_DIR / "action.v1.json"))
    assert isinstance(base, dict)
    assert_valid("action", base, "example with explicit intent_kind")

    implicit = copy.deepcopy(base)
    del implicit["intent_kind"]
    assert_valid("action", implicit, "intent_kind absent == leverage_target")

    no_target = copy.deepcopy(base)
    del no_target["target"]
    assert_invalid("action", no_target, "leverage_target without target")

    no_target_implicit = copy.deepcopy(implicit)
    del no_target_implicit["target"]
    assert_invalid("action", no_target_implicit, "implicit intent without target")

    unknown = copy.deepcopy(base)
    unknown["intent_kind"] = "purchase_intent"  # future minor, invalid in v1
    assert_invalid("action", unknown, "unknown intent_kind")

    # Canonical mirrors require the constant discriminator.
    record = copy.deepcopy(load_json(EXAMPLE_DIR / "decision_record.v1.json"))
    assert record["meant"]["action"]["intent_kind"] == "leverage_target"
    broken = copy.deepcopy(record)
    del broken["meant"]["action"]["intent_kind"]
    assert_invalid("decision_record", broken, "meant.action without intent_kind")


def test_canonical_action_seam_completes_on_hashed_forms() -> None:
    """IC-3 seam completion (M0 audit round 3): the conditional now lives on the
    two canonical, hashed, chained forms too (event/v1 ActionParsed and
    decision_record/v1 meant.action) — target_lev_1e4/max_slippage_bps are
    required exactly when intent_kind == 'leverage_target', which in v1 is
    always (intent_kind is unconditionally required and has one member). So
    dropping the leverage vector is STILL schema-invalid in v1, while a future
    purchase_intent branch needs only a minor bump."""
    # event/v1 -> ActionParsed: take a real golden ActionParsed line.
    events = load_jsonl(GOLDEN / "expected" / "main" / "events.jsonl")
    parsed = [e for e in events if e["type"] == "ActionParsed"]
    assert parsed, "golden events must contain ActionParsed"
    ev = copy.deepcopy(parsed[0])
    assert_valid("event", ev, "golden ActionParsed event")

    no_vector = copy.deepcopy(ev)
    del no_vector["payload"]["target_lev_1e4"]
    assert_invalid("event", no_vector, "ActionParsed without target_lev_1e4")

    no_slip = copy.deepcopy(ev)
    del no_slip["payload"]["max_slippage_bps"]
    assert_invalid("event", no_slip, "ActionParsed without max_slippage_bps")

    no_kind = copy.deepcopy(ev)
    del no_kind["payload"]["intent_kind"]
    assert_invalid("event", no_kind, "ActionParsed without intent_kind")

    # decision_record/v1 -> meant.action: same conditional, same outcome.
    record = copy.deepcopy(load_json(EXAMPLE_DIR / "decision_record.v1.json"))
    assert_valid("decision_record", record, "example decision_record")

    broken = copy.deepcopy(record)
    del broken["meant"]["action"]["target_lev_1e4"]
    assert_invalid("decision_record", broken, "meant.action without target_lev_1e4")

    broken = copy.deepcopy(record)
    del broken["meant"]["action"]["max_slippage_bps"]
    assert_invalid("decision_record", broken, "meant.action without max_slippage_bps")


def test_decision_record_meant_status_coupling() -> None:
    record = copy.deepcopy(load_json(EXAMPLE_DIR / "decision_record.v1.json"))
    assert record["meant"]["status"] == "parsed" and record["meant"]["action"]
    assert_valid("decision_record", record, "example decision_record")

    broken = copy.deepcopy(record)
    broken["meant"]["action"] = None  # parsed but no action: self-contradictory
    assert_invalid("decision_record", broken, "status=parsed with action=null")

    broken = copy.deepcopy(record)
    broken["meant"]["status"] = "rejected"  # rejected but action non-null
    assert_invalid("decision_record", broken, "status=rejected with action set")
