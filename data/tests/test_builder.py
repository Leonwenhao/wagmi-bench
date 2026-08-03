# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from data.binance import ArchiveSource, ChecksumError
from data.builder import (
    PackBuildConfig,
    PackBuildError,
    RawSeriesArchive,
    build_pack,
)
from spec.canonical import canonical_bytes, content_hash

START_TS = 1_600_012_800_000
HOUR_MS = 3_600_000


def test_builder_emits_schema_valid_canonical_deterministic_pack(
    tmp_path: Path,
) -> None:
    sources = _raw_sources(tmp_path)
    config = _config()

    first = build_pack(config, sources, tmp_path / "pack-a")
    second = build_pack(config, sources, tmp_path / "pack-b")

    first_files = sorted(path.name for path in first.directory.iterdir())
    second_files = sorted(path.name for path in second.directory.iterdir())
    assert first_files == second_files
    for name in first_files:
        assert (first.directory / name).read_bytes() == (
            second.directory / name
        ).read_bytes()

    manifest_object = cast(
        dict[str, object],
        json.loads(first.manifest_path.read_text(encoding="utf-8")),
    )
    schema = json.loads(
        Path("spec/schemas/pack_manifest.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(manifest_object)
    assert first.manifest_path.read_bytes() == canonical_bytes(manifest_object)
    assert manifest_object["claim_label"] == "survival-stress"
    assert manifest_object["bar_intervals_ms"] == [HOUR_MS]

    files = cast(list[dict[str, object]], manifest_object["files"])
    projection = {
        "schema": "pack_content/v1",
        "files": [
            {
                key: metadata[key]
                for key in ("path", "sha256", "bytes", "records")
            }
            for metadata in files
        ],
    }
    assert manifest_object["content_hash"] == content_hash(projection)
    assert first.content_hash == manifest_object["content_hash"]

    trade_rows = _jsonl(first.directory / "bars_1h.jsonl")
    assert trade_rows[0] == {
        "ts": START_TS,
        "available_at": START_TS + HOUR_MS,
        "o": 1000,
        "h": 1020,
        "l": 990,
        "c": 1010,
        "v_base_1e8": 123_456_789,
    }
    assert trade_rows[1]["ts"] == START_TS + HOUR_MS
    mark_rows = _jsonl(first.directory / "mark_1h.jsonl")
    index_rows = _jsonl(first.directory / "index_1h.jsonl")
    assert mark_rows[0] == {
        "ts": START_TS,
        "available_at": START_TS + HOUR_MS,
        "o": 1000,
        "h": 1011,
        "l": 980,
        "c": 1001,
        "v_base_1e8": 0,
    }
    assert mark_rows[0]["v_base_1e8"] == 0
    assert index_rows[0]["v_base_1e8"] == 0
    funding_rows = _jsonl(first.directory / "funding.jsonl")
    assert funding_rows == [
        {
            "ts": START_TS,
            "available_at": START_TS,
            "rate_1e8": 10_000,
        }
    ]

    for series_path in first.series_paths:
        for line in series_path.read_bytes().splitlines():
            parsed = cast(object, json.loads(line))
            assert line == canonical_bytes(parsed)


def test_builder_verifies_all_archives_before_creating_output(
    tmp_path: Path,
) -> None:
    sources = _raw_sources(tmp_path)
    corrupt = sources[1].source
    corrupt.checksum_path.write_text(
        f"{'0' * 64}  {corrupt.archive_path.name}\n",
        encoding="utf-8",
    )
    output = tmp_path / "must-not-exist"

    with pytest.raises(ChecksumError):
        build_pack(_config(), sources, output)

    assert not output.exists()


def test_builder_normalizes_subsecond_funding_calc_time_jitter(
    tmp_path: Path,
) -> None:
    sources = _raw_sources(tmp_path)
    jittered = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        f"{START_TS + 6},8,-0.00300000\n"
    )
    sources[-1] = RawSeriesArchive(
        "funding",
        0,
        _source(tmp_path, "funding-jitter.zip", jittered),
    )

    built = build_pack(_config(), sources, tmp_path / "pack")

    assert _jsonl(built.directory / "funding.jsonl") == [
        {
            "ts": START_TS,
            "available_at": START_TS,
            "rate_1e8": -300_000,
        }
    ]


def test_builder_rejects_funding_interval_mismatch_from_source(
    tmp_path: Path,
) -> None:
    sources = _raw_sources(tmp_path)
    wrong_interval = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        f"{START_TS},4,0.00010000\n"
    )
    sources[-1] = RawSeriesArchive(
        "funding",
        0,
        _source(tmp_path, "funding-wrong-interval.zip", wrong_interval),
    )

    with pytest.raises(PackBuildError, match="funding interval"):
        build_pack(_config(), sources, tmp_path / "pack")


def _config() -> PackBuildConfig:
    market_descriptor: dict[str, object] = {
        "instrument": "binance-um:BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "tick_size_micro": 100_000,
        "qty_step_base_1e8": 100_000,
        "min_notional_micro": 10_000_000,
        "leverage_cap_lev_1e4": 30_000,
        "margin": {
            "tiers": [
                {
                    "notional_cap_micro": 9_007_199_254_740_991,
                    "initial_rate_1e8": 33_333_334,
                    "maintenance_rate_1e8": 16_666_667,
                }
            ],
            "liquidation_penalty_1e8": 500_000,
        },
        "funding": {
            "interval_ms": 28_800_000,
            "settlement_offsets_ms": [0, 28_800_000, 57_600_000],
            "cap_1e8": 300_000,
            "floor_1e8": -300_000,
        },
        "fees": {
            "maker_rate_1e8": 20_000,
            "taker_rate_1e8": 40_000,
        },
        "execution": {
            "half_spread_1e8": 50_000,
            "impact_model": "sqrt",
            "impact_coeff_1e8": 30_000,
            "participation_cap_1e8": 1_000_000,
            "cost_profile_multipliers_1e4": {
                "primary": 10_000,
                "stress_2x": 20_000,
            },
        },
    }
    return PackBuildConfig(
        pack_id="offline-test",
        market_alias="BTC",
        market_descriptor=market_descriptor,
        window_start_ts=START_TS,
        window_end_ts=START_TS + (2 * HOUR_MS),
        decision_bar_ms=HOUR_MS,
        warmup_bars=0,
        default_lookback_bars=2,
        default_funding_prints=1,
        regime_description="Synthetic offline pack for deterministic tests.",
        created_by_version="0.1.0",
    )


def _raw_sources(directory: Path) -> list[RawSeriesArchive]:
    trade = (
        f"{START_TS + HOUR_MS},101.0,103.0,100.0,102.0,2.00000000\n"
        f"{START_TS},100.0,102.0,99.0,101.0,1.23456789\n"
    )
    mark = (
        "open_time,open,high,low,close,volume\n"
        f"{START_TS},100.04499999,101.00000001,98.09999999,100.05000000,999.0\n"
        f"{START_TS + HOUR_MS},100.0,102.0,99.0,101.0,999.0\n"
    )
    index = (
        "open_time,open,high,low,close,volume\n"
        f"{START_TS},100.0,101.0,99.0,100.0,999.0\n"
        f"{START_TS + HOUR_MS},100.0,102.0,99.0,101.0,999.0\n"
    )
    funding = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        f"{START_TS},8,0.00010000\n"
    )
    return [
        RawSeriesArchive(
            "trade",
            HOUR_MS,
            _source(directory, "trade.zip", trade),
        ),
        RawSeriesArchive(
            "mark",
            HOUR_MS,
            _source(directory, "mark.zip", mark),
        ),
        RawSeriesArchive(
            "index",
            HOUR_MS,
            _source(directory, "index.zip", index),
        ),
        RawSeriesArchive(
            "funding",
            0,
            _source(directory, "funding.zip", funding),
        ),
    ]


def _source(directory: Path, name: str, csv_text: str) -> ArchiveSource:
    archive_path = directory / name
    info = zipfile.ZipInfo(name.removesuffix(".zip") + ".csv")
    info.date_time = (1980, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(archive_path, mode="w") as zipped:
        zipped.writestr(info, csv_text.encode("utf-8"))
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = directory / f"{name}.CHECKSUM"
    checksum_path.write_text(f"{digest}  {name}\n", encoding="utf-8")
    return ArchiveSource(
        url=f"https://data.binance.vision/data/futures/um/monthly/{name}",
        archive_path=archive_path,
        checksum_path=checksum_path,
    )


def _jsonl(path: Path) -> list[dict[str, int]]:
    return [
        cast(dict[str, int], json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
