# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[2]


def test_metrics_document_names_every_frozen_profile_metric() -> None:
    schema = cast(
        dict[str, object],
        json.loads(
            (ROOT / "spec/schemas/metrics.v1.schema.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    defs = cast(dict[str, object], schema["$defs"])
    profile = cast(dict[str, object], defs["profile_metrics"])
    properties = cast(dict[str, object], profile["properties"])
    document = (ROOT / "spec/metrics.md").read_text(encoding="utf-8")
    for field in properties:
        assert f"`{field}`" in document


def test_metrics_document_names_every_profile_invariant_field() -> None:
    schema = cast(
        dict[str, object],
        json.loads(
            (ROOT / "spec/schemas/metrics.v1.schema.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    top_properties = cast(dict[str, object], schema["properties"])
    invariant = cast(dict[str, object], top_properties["profile_invariant"])
    invariant_properties = cast(dict[str, object], invariant["properties"])
    document = (ROOT / "spec/metrics.md").read_text(encoding="utf-8")
    for field in invariant_properties:
        assert f"`{field}`" in document


def test_fill_document_covers_frozen_execution_vocabulary() -> None:
    pack_schema = cast(
        dict[str, object],
        json.loads(
            (ROOT / "spec/schemas/pack_manifest.v1.schema.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    defs = cast(dict[str, object], pack_schema["$defs"])
    market = cast(dict[str, object], defs["market_descriptor"])
    market_properties = cast(dict[str, object], market["properties"])
    execution = cast(dict[str, object], market_properties["execution"])
    execution_properties = cast(dict[str, object], execution["properties"])
    impact_model = cast(dict[str, object], execution_properties["impact_model"])
    impact_names = cast(list[str], impact_model["enum"])

    event_schema = cast(
        dict[str, object],
        json.loads(
            (ROOT / "spec/schemas/event.v1.schema.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    event_defs = cast(dict[str, object], event_schema["$defs"])
    cancellation = cast(dict[str, object], event_defs["OrderCancelled"])
    cancellation_properties = cast(dict[str, object], cancellation["properties"])
    reason = cast(dict[str, object], cancellation_properties["reason"])
    cancellation_names = cast(list[str], reason["enum"])

    document = (ROOT / "spec/fill-model.md").read_text(encoding="utf-8")
    for name in [*impact_names, *cancellation_names, "primary", "stress_2x"]:
        assert f"`{name}`" in document
