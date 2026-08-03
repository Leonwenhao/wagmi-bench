#!/usr/bin/env python3
"""Deterministic generator for the C0.4 leakage-probe fixture pack (ISO-1).

Rebuilding produces byte-identical output (no wall clock, no randomness) for ALL
SIX pack files: the 4 series files, manifest.json, AND sentinels.json. CI enforces
this (spec/tests/test_leakage_fixture.py regenerates into a temp dir and byte-diffs
against the committed files), so any edit to a committed fixture file MUST be made
here first and regenerated — and any edit here that changes committed bytes fails
CI until the committed files are consciously updated with it. This closed the M0
audit round-3 ISO1-GEN-DRIFT finding: the audit-hardened sentinels.json `rule`
wording is now source-of-truthed in this generator, so the documented regeneration
procedure can no longer silently revert it.

Usage: gen_fixture.py [output_dir]   (default: the committed fixture directory)
"""
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
ROOT.mkdir(parents=True, exist_ok=True)

# ---- JCS (RFC 8785) for our value domain: ASCII keys, integers, ASCII strings ----
def jcs(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256_hex(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()

# ---- time base: 2020-06-01 00:00:00 UTC, 4h bars, 8 bars ----
START = 1590969600000
BAR = 14400000
N = 8
END = START + N * BAR                     # 1591084800000, exclusive
POISON_AVAILABLE_AT = END + 604800000     # 1591689600000: one week AFTER window end

# ---- sentinel registry values ----
S_TRADE = {"l": 424242000100, "o": 424242000101, "c": 424242000102, "h": 424242000103, "v_base_1e8": 424242000104}
S_MARK  = {"l": 424242000200, "o": 424242000201, "c": 424242000202, "h": 424242000203}
S_INDEX = {"l": 424242000300, "o": 424242000301, "c": 424242000302, "h": 424242000303}
S_FUNDING_RATE = 42424242

POISON_TRADE_IDX, POISON_MARK_IDX, POISON_INDEX_IDX = 5, 3, 6

# per-bar (o, h, l, c) in ticks (tick_size_micro = 100000 -> 0.10 USDT; ~9500 USDT)
TRADE = [
    (95000, 95600, 94800, 95400, 250000000000),
    (95400, 95900, 95200, 95700, 180000000000),
    (95700, 95800, 95000, 95100, 210000000000),
    (95100, 95500, 94700, 95300, 190000000000),
    (95300, 96000, 95250, 95900, 230000000000),
    None,  # poison
    (95900, 96400, 95700, 96100, 175000000000),
    (96100, 96300, 95600, 95800, 205000000000),
]
MARK = [
    (95010, 95610, 94810, 95410),
    (95410, 95910, 95210, 95710),
    (95710, 95810, 95010, 95110),
    None,  # poison
    (95310, 96010, 95260, 95910),
    (95910, 96110, 95710, 95860),
    (95860, 96410, 95710, 96110),
    (96110, 96310, 95610, 95810),
]
INDEX = [
    (94990, 95590, 94790, 95390),
    (95390, 95890, 95190, 95690),
    (95690, 95790, 94990, 95090),
    (95090, 95490, 94690, 95290),
    (95290, 95990, 95240, 95890),
    (95890, 96090, 95690, 95840),
    None,  # poison
    (96090, 96290, 95590, 95790),
]

def bar_rows(table, poison_idx, sentinels, with_volume):
    rows = []
    for i in range(N):
        ts = START + i * BAR
        if i == poison_idx:
            row = {
                "ts": ts,
                "available_at": POISON_AVAILABLE_AT,
                "o": sentinels["o"], "h": sentinels["h"], "l": sentinels["l"], "c": sentinels["c"],
                "v_base_1e8": sentinels["v_base_1e8"] if with_volume else 0,
            }
        else:
            t = table[i]
            row = {
                "ts": ts,
                "available_at": ts + BAR,
                "o": t[0], "h": t[1], "l": t[2], "c": t[3],
                "v_base_1e8": t[4] if with_volume else 0,
            }
        rows.append(row)
    return rows

FUNDING_TS = [START, START + 28800000, START + 57600000, START + 86400000]  # 00/08/16/00 UTC
POISON_FUNDING_TS = START + 57600000  # 2020-06-01 16:00 UTC
FUNDING_RATES = {FUNDING_TS[0]: 10000, FUNDING_TS[1]: 12500, FUNDING_TS[3]: -8000}

funding_rows = []
for ts in FUNDING_TS:
    if ts == POISON_FUNDING_TS:
        funding_rows.append({"ts": ts, "available_at": POISON_AVAILABLE_AT, "rate_1e8": S_FUNDING_RATE})
    else:
        funding_rows.append({"ts": ts, "available_at": ts, "rate_1e8": FUNDING_RATES[ts]})

series = {
    "bars_4h.jsonl":  ("trade",   BAR, "bar_row/v1",     bar_rows(TRADE, POISON_TRADE_IDX, S_TRADE, True)),
    "mark_4h.jsonl":  ("mark",    BAR, "bar_row/v1",     bar_rows(MARK,  POISON_MARK_IDX,  S_MARK,  False)),
    "index_4h.jsonl": ("index",   BAR, "bar_row/v1",     bar_rows(INDEX, POISON_INDEX_IDX, S_INDEX, False)),
    "funding.jsonl":  ("funding", 0,   "funding_row/v1", funding_rows),
}

files_meta = []
for path, (role, interval, row_schema, rows) in series.items():
    data = b"".join(jcs(r) + b"\n" for r in rows)
    (ROOT / path).write_bytes(data)
    files_meta.append({
        "path": path, "role": role, "market": "BTC", "interval_ms": interval,
        "row_schema": row_schema, "sha256": sha256_hex(data),
        "bytes": len(data), "records": len(rows), "upstream": [],
    })

# content_hash over pack_content/v1, entries sorted by path
content_entries = sorted(
    ({"path": f["path"], "sha256": f["sha256"], "bytes": f["bytes"], "records": f["records"]} for f in files_meta),
    key=lambda e: e["path"],
)
content_hash = sha256_hex(jcs({"schema": "pack_content/v1", "files": content_entries}))

manifest = {
    "schema": "pack_manifest/v1",
    "pack_id": "leakage-probe",
    "content_hash": content_hash,
    "venue": "binance-um",
    "window": {"start_ts": START, "end_ts": END},
    "bar_intervals_ms": [BAR],
    "decision_bar_ms": BAR,
    "warmup_bars": 2,
    "markets": {
        "BTC": {
            "instrument": "binance-um:BTCUSDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "tick_size_micro": 100000,
            "qty_step_base_1e8": 100000,
            "min_notional_micro": 10000000,
            "leverage_cap_lev_1e4": 30000,
            "margin": {
                "tiers": [
                    {"notional_cap_micro": 9007199254740991, "initial_rate_1e8": 33333334, "maintenance_rate_1e8": 16666667}
                ],
                "liquidation_penalty_1e8": 500000,
            },
            "funding": {
                "interval_ms": 28800000,
                "settlement_offsets_ms": [0, 28800000, 57600000],
                "cap_1e8": 300000,
                "floor_1e8": -300000,
            },
            "fees": {"maker_rate_1e8": 20000, "taker_rate_1e8": 40000},
            "execution": {
                "half_spread_1e8": 2500,
                "impact_model": "linear",
                "impact_coeff_1e8": 0,
                "participation_cap_1e8": 1000000,
                "cost_profile_multipliers_1e4": {"primary": 10000, "stress_2x": 20000},
            },
            "calibration_note": "Synthetic fixture values; deliberately uncalibrated. This pack exists only for the ISO-1 leakage probe.",
        }
    },
    "files": sorted(files_meta, key=lambda f: f["path"]),
    "default_lookback": {"bars": 4, "funding_prints": 3},
    "regime_description": "SYNTHETIC LEAKAGE-PROBE FIXTURE, not a market regime. Eight flat synthetic 4h bars near 9500 USDT with four deliberately poisoned rows whose available_at lies one week after the pack window and whose price/rate fields carry sentinel values from sentinels.json. Exists solely to make ISO-1 look-ahead failures loud (C0.4). Never run a benchmark from this pack.",
    "claim_label": "survival-stress",
    "created_by_version": "0.1.0",
}
(ROOT / "manifest.json").write_bytes(jcs(manifest) + b"\n")

sentinels = {
    "schema": "leakage_sentinel_registry/v1",
    "pack_id": "leakage-probe",
    "content_hash": content_hash,
    "rule": "SECONDARY TRIPWIRE (necessary, not sufficient - see README): if ANY integer listed in sentinel_values ever appears in ANY field of ANY emitted observation (or any other agent-visible surface), the run FAILS ISO-1. Detection is by value equality on stored observation bytes. The PRIMARY ISO-1 check is the max(available_at) <= clock_ts property cross-checked against pack bytes, with field->source-row provenance mapping for surfaces lacking in-band available_at (funding prints, mark/index scalars); passing this value scan alone does not prove leak-freedom.",
    "poison_available_at": POISON_AVAILABLE_AT,
    "poison_rows": [
        {"file": "bars_4h.jsonl",  "row_index": POISON_TRADE_IDX, "ts": START + POISON_TRADE_IDX * BAR,
         "available_at": POISON_AVAILABLE_AT,
         "sentinel_fields": {"o": S_TRADE["o"], "h": S_TRADE["h"], "l": S_TRADE["l"], "c": S_TRADE["c"], "v_base_1e8": S_TRADE["v_base_1e8"]}},
        {"file": "mark_4h.jsonl",  "row_index": POISON_MARK_IDX,  "ts": START + POISON_MARK_IDX * BAR,
         "available_at": POISON_AVAILABLE_AT,
         "sentinel_fields": {"o": S_MARK["o"], "h": S_MARK["h"], "l": S_MARK["l"], "c": S_MARK["c"]}},
        {"file": "index_4h.jsonl", "row_index": POISON_INDEX_IDX, "ts": START + POISON_INDEX_IDX * BAR,
         "available_at": POISON_AVAILABLE_AT,
         "sentinel_fields": {"o": S_INDEX["o"], "h": S_INDEX["h"], "l": S_INDEX["l"], "c": S_INDEX["c"]}},
        {"file": "funding.jsonl",  "row_index": 2, "ts": POISON_FUNDING_TS,
         "available_at": POISON_AVAILABLE_AT,
         "sentinel_fields": {"rate_1e8": S_FUNDING_RATE}},
    ],
    "sentinel_values": sorted(
        list(S_TRADE.values()) + list(S_MARK.values()) + list(S_INDEX.values()) + [S_FUNDING_RATE]
    ),
}
(ROOT / "sentinels.json").write_bytes(json.dumps(sentinels, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n")

print("content_hash:", content_hash)
for f in sorted(files_meta, key=lambda x: x["path"]):
    print(f'{f["path"]}: {f["records"]} records, {f["bytes"]} bytes, {f["sha256"]}')
