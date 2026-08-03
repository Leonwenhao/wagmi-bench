# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from data.validator import (
    MARK_TRADE_MAX_DIVERGENCE_1E8,
    PackValidationError,
    validate_pack,
)
from spec.canonical import canonical_bytes, content_hash, sha256_prefixed

JsonObject = dict[str, object]

HOUR_MS = 3_600_000
FUNDING_INTERVAL_MS = 8 * HOUR_MS
START_TS = 1_600_012_800_000  # 2020-09-13T16:00:00Z
END_TS = START_TS + (16 * HOUR_MS)


@pytest.fixture
def valid_pack(tmp_path: Path) -> Path:
    return _make_valid_pack(tmp_path / "pack")


def test_valid_pack_returns_compact_receipt(valid_pack: Path) -> None:
    result = validate_pack(valid_pack)

    assert result.pack_id == "validator-fixture"
    assert result.files == 4
    assert result.bar_rows == 48
    assert result.funding_rows == 2
    assert result.actionable_bars == 13


def test_deleted_aligned_bar_reaches_exact_gap_check(
    valid_pack: Path,
) -> None:
    for path in ("bars_1h.jsonl", "index_1h.jsonl", "mark_1h.jsonl"):
        rows = _read_rows(valid_pack / path)
        del rows[5]
        _rewrite_rows(valid_pack, path, rows)

    with pytest.raises(PackValidationError, match=r"^BAR_GAP:"):
        validate_pack(valid_pack)


def test_deleted_funding_print_reaches_exact_gap_check(
    valid_pack: Path,
) -> None:
    rows = _read_rows(valid_pack / "funding.jsonl")
    del rows[1]
    _rewrite_rows(valid_pack, "funding.jsonl", rows)

    with pytest.raises(PackValidationError, match=r"^FUNDING_GAP:"):
        validate_pack(valid_pack)


def test_duplicated_funding_row_reaches_duplicate_check(
    valid_pack: Path,
) -> None:
    rows = _read_rows(valid_pack / "funding.jsonl")
    rows.insert(1, dict(rows[0]))
    _rewrite_rows(valid_pack, "funding.jsonl", rows)

    with pytest.raises(PackValidationError, match=r"^DUPLICATE_TIMESTAMP:"):
        validate_pack(valid_pack)


def test_shuffled_bar_reaches_strict_order_check(valid_pack: Path) -> None:
    rows = _read_rows(valid_pack / "bars_1h.jsonl")
    rows[3], rows[4] = rows[4], rows[3]
    _rewrite_rows(valid_pack, "bars_1h.jsonl", rows)

    with pytest.raises(PackValidationError, match=r"^TIMESTAMP_ORDER:"):
        validate_pack(valid_pack)


def test_wrong_btc_funding_interval_reaches_interval_check(
    valid_pack: Path,
) -> None:
    manifest = _read_manifest(valid_pack)
    funding = _funding_descriptor(manifest)
    funding["interval_ms"] = 4 * HOUR_MS
    funding["settlement_offsets_ms"] = [
        0,
        4 * HOUR_MS,
        8 * HOUR_MS,
        12 * HOUR_MS,
        16 * HOUR_MS,
        20 * HOUR_MS,
    ]
    _write_manifest(valid_pack, manifest)

    with pytest.raises(PackValidationError, match=r"^FUNDING_INTERVAL:"):
        validate_pack(valid_pack)


def test_bar_available_at_reaches_semantic_check(valid_pack: Path) -> None:
    rows = _read_rows(valid_pack / "bars_1h.jsonl")
    rows[2]["available_at"] = cast(int, rows[2]["available_at"]) + 1
    _rewrite_rows(valid_pack, "bars_1h.jsonl", rows)

    with pytest.raises(PackValidationError, match=r"^AVAILABLE_AT:"):
        validate_pack(valid_pack)


def test_funding_available_at_reaches_semantic_check(
    valid_pack: Path,
) -> None:
    rows = _read_rows(valid_pack / "funding.jsonl")
    rows[0]["available_at"] = cast(int, rows[0]["available_at"]) + 1
    _rewrite_rows(valid_pack, "funding.jsonl", rows)

    with pytest.raises(PackValidationError, match=r"^AVAILABLE_AT:"):
        validate_pack(valid_pack)


def test_off_clock_funding_reaches_settlement_check(
    valid_pack: Path,
) -> None:
    rows = _read_rows(valid_pack / "funding.jsonl")
    rows[1]["ts"] = cast(int, rows[1]["ts"]) + HOUR_MS
    rows[1]["available_at"] = rows[1]["ts"]
    _rewrite_rows(valid_pack, "funding.jsonl", rows)

    with pytest.raises(PackValidationError, match=r"^FUNDING_SETTLEMENT:"):
        validate_pack(valid_pack)


def test_out_of_cap_funding_reaches_cap_check(valid_pack: Path) -> None:
    rows = _read_rows(valid_pack / "funding.jsonl")
    rows[0]["rate_1e8"] = 300_001
    _rewrite_rows(valid_pack, "funding.jsonl", rows)

    with pytest.raises(PackValidationError, match=r"^FUNDING_CAP:"):
        validate_pack(valid_pack)


def test_one_missing_price_role_reaches_series_matrix_check(
    valid_pack: Path,
) -> None:
    manifest = _read_manifest(valid_pack)
    files = _manifest_files(manifest)
    manifest["files"] = [
        entry for entry in files if entry.get("role") != "mark"
    ]
    _refresh_content_hash(manifest)
    _write_manifest(valid_pack, manifest)

    with pytest.raises(PackValidationError, match=r"^MISSING_SERIES:"):
        validate_pack(valid_pack)


def test_misaligned_price_role_reaches_alignment_check(
    valid_pack: Path,
) -> None:
    rows = _read_rows(valid_pack / "index_1h.jsonl")
    rows[6]["ts"] = cast(int, rows[6]["ts"]) + 1
    rows[6]["available_at"] = cast(int, rows[6]["available_at"]) + 1
    _rewrite_rows(valid_pack, "index_1h.jsonl", rows)

    with pytest.raises(PackValidationError, match=r"^BAR_ALIGNMENT:"):
        validate_pack(valid_pack)


def test_gross_mark_trade_divergence_reaches_policy_check(
    valid_pack: Path,
) -> None:
    rows = _read_rows(valid_pack / "mark_1h.jsonl")
    trade_rows = _read_rows(valid_pack / "bars_1h.jsonl")
    grossly_wrong = 2 * cast(int, trade_rows[7]["c"])
    for field in ("o", "h", "l", "c"):
        rows[7][field] = grossly_wrong
    _rewrite_rows(valid_pack, "mark_1h.jsonl", rows)

    with pytest.raises(
        PackValidationError,
        match=r"^MARK_TRADE_DIVERGENCE:",
    ):
        validate_pack(valid_pack)


def test_mark_trade_divergence_policy_includes_exact_boundary(
    valid_pack: Path,
) -> None:
    rows = _read_rows(valid_pack / "mark_1h.jsonl")
    trade_rows = _read_rows(valid_pack / "bars_1h.jsonl")
    trade_close = cast(int, trade_rows[0]["c"])
    allowed_difference = (
        trade_close * MARK_TRADE_MAX_DIVERGENCE_1E8 // 100_000_000
    )
    boundary_close = trade_close + allowed_difference
    for field in ("o", "h", "l", "c"):
        rows[0][field] = boundary_close
    _rewrite_rows(valid_pack, "mark_1h.jsonl", rows)

    assert validate_pack(valid_pack).pack_id == "validator-fixture"


def test_warmup_must_leave_actionable_and_holding_bars(
    valid_pack: Path,
) -> None:
    manifest = _read_manifest(valid_pack)
    manifest["warmup_bars"] = 15
    _write_manifest(valid_pack, manifest)

    with pytest.raises(PackValidationError, match=r"^WARMUP_LENGTH:"):
        validate_pack(valid_pack)


def test_max_leverage_margin_tier_requires_half_maintenance(
    valid_pack: Path,
) -> None:
    manifest = _read_manifest(valid_pack)
    markets = _object(manifest["markets"])
    btc = _object(markets["BTC"])
    margin = _object(btc["margin"])
    tiers = _array(margin["tiers"])
    first = _object(tiers[0])
    first["maintenance_rate_1e8"] = 16_666_666
    _write_manifest(valid_pack, manifest)

    with pytest.raises(PackValidationError, match=r"^MARGIN_INVARIANT:"):
        validate_pack(valid_pack)


def test_seed_helpers_recompute_integrity_before_semantic_validation(
    valid_pack: Path,
) -> None:
    rows = _read_rows(valid_pack / "bars_1h.jsonl")
    rows[1]["available_at"] = cast(int, rows[1]["available_at"]) + 1
    _rewrite_rows(valid_pack, "bars_1h.jsonl", rows)

    manifest = _read_manifest(valid_pack)
    entry = next(
        item
        for item in _manifest_files(manifest)
        if item["path"] == "bars_1h.jsonl"
    )
    raw = (valid_pack / "bars_1h.jsonl").read_bytes()
    assert entry["bytes"] == len(raw)
    assert entry["records"] == len(raw.splitlines())
    assert entry["sha256"] == sha256_prefixed(raw)
    assert manifest["content_hash"] == _projected_content_hash(manifest)
    with pytest.raises(PackValidationError, match=r"^AVAILABLE_AT:"):
        validate_pack(valid_pack)


def _make_valid_pack(root: Path) -> Path:
    root.mkdir()
    series_rows: dict[str, tuple[str, int, list[JsonObject]]] = {}
    trade: list[JsonObject] = []
    mark: list[JsonObject] = []
    index: list[JsonObject] = []
    for offset in range(16):
        ts = START_TS + offset * HOUR_MS
        base = 100_000 + offset * 100
        trade.append(
            {
                "ts": ts,
                "available_at": ts + HOUR_MS,
                "o": base,
                "h": base + 40,
                "l": base - 40,
                "c": base + 20,
                "v_base_1e8": 100_000_000,
            }
        )
        mark.append(
            {
                "ts": ts,
                "available_at": ts + HOUR_MS,
                "o": base + 10,
                "h": base + 45,
                "l": base - 35,
                "c": base + 25,
                "v_base_1e8": 0,
            }
        )
        index.append(
            {
                "ts": ts,
                "available_at": ts + HOUR_MS,
                "o": base + 5,
                "h": base + 42,
                "l": base - 38,
                "c": base + 22,
                "v_base_1e8": 0,
            }
        )
    funding: list[JsonObject] = [
        {
            "ts": START_TS,
            "available_at": START_TS,
            "rate_1e8": 10_000,
        },
        {
            "ts": START_TS + FUNDING_INTERVAL_MS,
            "available_at": START_TS + FUNDING_INTERVAL_MS,
            "rate_1e8": -10_000,
        },
    ]
    series_rows["bars_1h.jsonl"] = ("trade", HOUR_MS, trade)
    series_rows["funding.jsonl"] = ("funding", 0, funding)
    series_rows["index_1h.jsonl"] = ("index", HOUR_MS, index)
    series_rows["mark_1h.jsonl"] = ("mark", HOUR_MS, mark)

    files: list[JsonObject] = []
    for path, (role, interval_ms, rows) in sorted(series_rows.items()):
        payload = b"".join(canonical_bytes(row) + b"\n" for row in rows)
        (root / path).write_bytes(payload)
        files.append(
            {
                "path": path,
                "role": role,
                "market": "BTC",
                "interval_ms": interval_ms,
                "row_schema": (
                    "funding_row/v1"
                    if role == "funding"
                    else "bar_row/v1"
                ),
                "sha256": sha256_prefixed(payload),
                "bytes": len(payload),
                "records": len(rows),
                "upstream": [
                    {
                        "url": (
                            "https://data.binance.vision/data/futures/um/"
                            f"monthly/validator/{path}.zip"
                        ),
                        "sha256": "sha256:" + ("0" * 64),
                    }
                ],
            }
        )

    manifest: JsonObject = {
        "schema": "pack_manifest/v1",
        "pack_id": "validator-fixture",
        "content_hash": "",
        "venue": "binance-um",
        "window": {"start_ts": START_TS, "end_ts": END_TS},
        "bar_intervals_ms": [HOUR_MS],
        "decision_bar_ms": HOUR_MS,
        "warmup_bars": 2,
        "markets": {"BTC": _market_descriptor()},
        "files": files,
        "default_lookback": {"bars": 2, "funding_prints": 2},
        "regime_description": (
            "Synthetic validator fixture with aligned hourly BTC series."
        ),
        "claim_label": "survival-stress",
        "created_by_version": "0.1.0",
    }
    _refresh_content_hash(manifest)
    _write_manifest(root, manifest)
    return root


def _market_descriptor() -> JsonObject:
    return {
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
            "interval_ms": FUNDING_INTERVAL_MS,
            "settlement_offsets_ms": [
                0,
                FUNDING_INTERVAL_MS,
                2 * FUNDING_INTERVAL_MS,
            ],
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


def _read_manifest(root: Path) -> JsonObject:
    return cast(
        JsonObject,
        json.loads((root / "manifest.json").read_text(encoding="utf-8")),
    )


def _write_manifest(root: Path, manifest: JsonObject) -> None:
    (root / "manifest.json").write_bytes(canonical_bytes(manifest))


def _read_rows(path: Path) -> list[JsonObject]:
    return [
        cast(JsonObject, json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _rewrite_rows(
    root: Path,
    relative: str,
    rows: list[JsonObject],
) -> None:
    payload = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    (root / relative).write_bytes(payload)
    manifest = _read_manifest(root)
    entry = next(
        item for item in _manifest_files(manifest) if item["path"] == relative
    )
    entry["sha256"] = sha256_prefixed(payload)
    entry["bytes"] = len(payload)
    entry["records"] = len(rows)
    _refresh_content_hash(manifest)
    _write_manifest(root, manifest)


def _projected_content_hash(manifest: JsonObject) -> str:
    files = [
        {
            key: entry[key]
            for key in ("path", "sha256", "bytes", "records")
        }
        for entry in _manifest_files(manifest)
    ]
    return content_hash({"schema": "pack_content/v1", "files": files})


def _refresh_content_hash(manifest: JsonObject) -> None:
    manifest["content_hash"] = _projected_content_hash(manifest)


def _manifest_files(manifest: JsonObject) -> list[JsonObject]:
    return [
        _object(value)
        for value in _array(manifest["files"])
    ]


def _funding_descriptor(manifest: JsonObject) -> JsonObject:
    markets = _object(manifest["markets"])
    btc = _object(markets["BTC"])
    return _object(btc["funding"])


def _object(value: object) -> JsonObject:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast(JsonObject, value)


def _array(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)
