# SPDX-License-Identifier: Apache-2.0
"""Deterministic semantic validation for locally built IC-1 packs.

The builder proves provenance and canonical construction.  This module is the
independent read-only gate over the resulting bytes: it rechecks integrity,
validates every frozen-schema instance, and enforces the C1.4 invariants that
JSON Schema cannot express.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, cast

from jsonschema import Draft202012Validator

from data.binance import BulkURLRejected, validate_bulk_url
from spec.canonical import canonical_bytes, content_hash, sha256_prefixed

JsonObject = dict[str, object]

_ROOT: Final = Path(__file__).resolve().parents[1]
_SCHEMA_DIR: Final = _ROOT / "spec" / "schemas"
_DAY_MS: Final = 86_400_000
_HOUR_MS: Final = 3_600_000
_BTCUSDT_FUNDING_INTERVAL_MS: Final = 8 * _HOUR_MS
_BTCUSDT_SETTLEMENT_OFFSETS_MS: Final[tuple[int, ...]] = (
    0,
    8 * _HOUR_MS,
    16 * _HOUR_MS,
)

# IC-1 requires a deterministic mark-vs-trade "sanity" check but does not
# freeze a threshold.  Five percent close-to-close is deliberately broad: it
# catches role swaps, unit errors, and gross corruption while retaining the
# verified COVID pack's largest close divergence (~2.575%).  This is a data
# validation policy, not an execution assumption or performance claim.
MARK_TRADE_MAX_DIVERGENCE_1E8: Final = 5_000_000


class PackValidationError(ValueError):
    """A locally built pack violates IC-1 or a C1.4 semantic invariant."""


@dataclass(frozen=True, slots=True)
class PackValidationResult:
    """Compact successful-validation receipt suitable for nightly summaries."""

    pack_id: str
    content_hash: str
    files: int
    bar_rows: int
    funding_rows: int
    actionable_bars: int


@dataclass(frozen=True, slots=True)
class _Series:
    path: str
    market: str
    role: str
    interval_ms: int
    rows: tuple[JsonObject, ...]


@dataclass(frozen=True, slots=True)
class _FundingPolicy:
    interval_ms: int
    settlement_offsets_ms: tuple[int, ...]
    floor_1e8: int
    cap_1e8: int


def _load_schema(name: str) -> JsonObject:
    value = json.loads(
        (_SCHEMA_DIR / f"{name}.v1.schema.json").read_text(encoding="utf-8")
    )
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} schema root is not an object")
    return cast(JsonObject, value)


_VALIDATORS: Final[dict[str, Draft202012Validator]] = {
    name: Draft202012Validator(_load_schema(name))
    for name in ("pack_manifest", "bar_row", "funding_row")
}


def _fail(code: str, message: str) -> NoReturn:
    raise PackValidationError(f"{code}: {message}")


def _reject_float(token: str) -> NoReturn:
    _fail("NON_INTEGER_JSON", f"fractional JSON number is forbidden: {token}")


def _reject_constant(token: str) -> NoReturn:
    _fail("NON_INTEGER_JSON", f"non-finite JSON number is forbidden: {token}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> JsonObject:
    value: JsonObject = {}
    for key, item in pairs:
        if key in value:
            _fail("DUPLICATE_JSON_KEY", f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _decode_json(raw: bytes, *, source: str) -> object:
    try:
        return cast(
            object,
            json.loads(
                raw.decode("utf-8"),
                parse_float=_reject_float,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            ),
        )
    except UnicodeDecodeError as exc:
        raise PackValidationError(f"INVALID_UTF8: {source}") from exc
    except json.JSONDecodeError as exc:
        raise PackValidationError(
            f"INVALID_JSON: {source} at byte {exc.pos}"
        ) from exc


def _object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        _fail("TYPE_ERROR", f"{field} must be an object")
    return cast(JsonObject, value)


def _array(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        _fail("TYPE_ERROR", f"{field} must be an array")
    return cast(list[object], value)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("TYPE_ERROR", f"{field} must be an integer")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("TYPE_ERROR", f"{field} must be a string")
    return value


def _validate_schema(name: str, value: object, *, context: str) -> None:
    errors = sorted(
        _VALIDATORS[name].iter_errors(value),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(item) for item in error.absolute_path)
    suffix = f" at {location}" if location else ""
    _fail("SCHEMA_INVALID", f"{context}{suffix}: {error.message}")


def _safe_series_path(root: Path, relative: str) -> Path:
    candidate = root / relative
    if candidate.is_symlink():
        _fail("UNSAFE_PATH", f"{relative} must not be a symlink")
    resolved_root = root.resolve()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PackValidationError(
            f"MISSING_FILE: cannot read declared series {relative}"
        ) from exc
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        _fail("UNSAFE_PATH", f"declared series escapes pack root: {relative}")
    return resolved


def _read_series(
    root: Path,
    entry: Mapping[str, object],
) -> _Series:
    relative = _string(entry.get("path"), "files[].path")
    role = _string(entry.get("role"), f"{relative}.role")
    market = _string(entry.get("market"), f"{relative}.market")
    interval_ms = _integer(
        entry.get("interval_ms"),
        f"{relative}.interval_ms",
    )
    row_schema = _string(
        entry.get("row_schema"),
        f"{relative}.row_schema",
    )
    expected_schema = "funding_row/v1" if role == "funding" else "bar_row/v1"
    if row_schema != expected_schema:
        _fail(
            "ROW_SCHEMA",
            f"{relative} declares {row_schema}, expected {expected_schema}",
        )

    upstream = _array(entry.get("upstream"), f"{relative}.upstream")
    if not upstream:
        _fail("UPSTREAM_PROVENANCE", f"{relative} has no upstream archive")
    for index, raw_source in enumerate(upstream):
        source = _object(raw_source, f"{relative}.upstream[{index}]")
        url = _string(
            source.get("url"),
            f"{relative}.upstream[{index}].url",
        )
        try:
            validate_bulk_url(url)
        except BulkURLRejected as exc:
            raise PackValidationError(
                f"UPSTREAM_ORIGIN: {relative}: {exc}"
            ) from exc

    raw = _safe_series_path(root, relative).read_bytes()
    expected_bytes = _integer(entry.get("bytes"), f"{relative}.bytes")
    if len(raw) != expected_bytes:
        _fail(
            "BYTE_COUNT",
            f"{relative}: expected {expected_bytes}, found {len(raw)}",
        )
    expected_sha256 = _string(entry.get("sha256"), f"{relative}.sha256")
    actual_sha256 = sha256_prefixed(raw)
    if actual_sha256 != expected_sha256:
        _fail(
            "FILE_HASH",
            f"{relative}: expected {expected_sha256}, found {actual_sha256}",
        )
    if raw and not raw.endswith(b"\n"):
        _fail("JSONL_CANONICAL", f"{relative} is not newline-terminated")
    lines = raw[:-1].split(b"\n") if raw else []
    if any(not line for line in lines):
        _fail("JSONL_CANONICAL", f"{relative} contains a blank record")
    expected_records = _integer(
        entry.get("records"),
        f"{relative}.records",
    )
    if len(lines) != expected_records:
        _fail(
            "RECORD_COUNT",
            f"{relative}: expected {expected_records}, found {len(lines)}",
        )

    schema_name = "funding_row" if role == "funding" else "bar_row"
    rows: list[JsonObject] = []
    for index, line in enumerate(lines, 1):
        context = f"{relative}:{index}"
        row = _object(_decode_json(line, source=context), context)
        _validate_schema(schema_name, row, context=context)
        try:
            canonical = canonical_bytes(row)
        except ValueError as exc:
            raise PackValidationError(
                f"JSONL_CANONICAL: {context}: {exc}"
            ) from exc
        if line != canonical:
            _fail("JSONL_CANONICAL", f"{context} is not JCS-canonical")
        rows.append(row)
    return _Series(
        path=relative,
        market=market,
        role=role,
        interval_ms=interval_ms,
        rows=tuple(rows),
    )


def _strict_timestamp_order(series: _Series) -> tuple[int, ...]:
    timestamps = tuple(
        _integer(row.get("ts"), f"{series.path}[{index}].ts")
        for index, row in enumerate(series.rows)
    )
    if len(set(timestamps)) != len(timestamps):
        _fail(
            "DUPLICATE_TIMESTAMP",
            f"{series.path} contains a duplicated timestamp",
        )
    if any(
        timestamps[index - 1] >= timestamps[index]
        for index in range(1, len(timestamps))
    ):
        _fail(
            "TIMESTAMP_ORDER",
            f"{series.path} timestamps are not strictly increasing",
        )
    return timestamps


def _validate_bar_rows(series: _Series) -> tuple[int, ...]:
    timestamps = _strict_timestamp_order(series)
    for index, row in enumerate(series.rows):
        ts = timestamps[index]
        available_at = _integer(
            row.get("available_at"),
            f"{series.path}[{index}].available_at",
        )
        if available_at != ts + series.interval_ms:
            _fail(
                "AVAILABLE_AT",
                f"{series.path}[{index}] must have available_at=ts+interval",
            )
        open_ticks = _integer(row.get("o"), f"{series.path}[{index}].o")
        high_ticks = _integer(row.get("h"), f"{series.path}[{index}].h")
        low_ticks = _integer(row.get("l"), f"{series.path}[{index}].l")
        close_ticks = _integer(row.get("c"), f"{series.path}[{index}].c")
        if low_ticks > min(open_ticks, close_ticks, high_ticks):
            _fail("BAR_OHLC", f"{series.path}[{index}] low exceeds OHLC")
        if high_ticks < max(open_ticks, close_ticks, low_ticks):
            _fail("BAR_OHLC", f"{series.path}[{index}] high is below OHLC")
    return timestamps


def _funding_policy(
    *,
    venue: str,
    alias: str,
    descriptor: Mapping[str, object],
) -> _FundingPolicy:
    funding = _object(
        descriptor.get("funding"),
        f"markets.{alias}.funding",
    )
    interval_ms = _integer(
        funding.get("interval_ms"),
        f"markets.{alias}.funding.interval_ms",
    )
    raw_offsets = _array(
        funding.get("settlement_offsets_ms"),
        f"markets.{alias}.funding.settlement_offsets_ms",
    )
    offsets = tuple(
        _integer(
            value,
            f"markets.{alias}.funding.settlement_offsets_ms[{index}]",
        )
        for index, value in enumerate(raw_offsets)
    )
    if tuple(sorted(set(offsets))) != offsets:
        _fail(
            "FUNDING_INTERVAL",
            f"{alias} funding settlement offsets must be sorted and unique",
        )
    if interval_ms <= 0 or _DAY_MS % interval_ms:
        _fail(
            "FUNDING_INTERVAL",
            f"{alias} funding interval must divide one UTC day exactly",
        )
    if len(offsets) != _DAY_MS // interval_ms:
        _fail(
            "FUNDING_INTERVAL",
            f"{alias} settlement offset count does not match its interval",
        )
    cyclic_gaps = tuple(
        (
            offsets[(index + 1) % len(offsets)]
            - offsets[index]
        )
        % _DAY_MS
        for index in range(len(offsets))
    )
    if any(gap != interval_ms for gap in cyclic_gaps):
        _fail(
            "FUNDING_INTERVAL",
            f"{alias} settlement offsets do not form an exact interval",
        )

    instrument = _string(
        descriptor.get("instrument"),
        f"markets.{alias}.instrument",
    )
    if venue == "binance-um" and instrument == "binance-um:BTCUSDT":
        if (
            interval_ms != _BTCUSDT_FUNDING_INTERVAL_MS
            or offsets != _BTCUSDT_SETTLEMENT_OFFSETS_MS
        ):
            _fail(
                "FUNDING_INTERVAL",
                "Binance BTCUSDT must settle exactly at 00/08/16 UTC",
            )

    floor_1e8 = _integer(
        funding.get("floor_1e8"),
        f"markets.{alias}.funding.floor_1e8",
    )
    cap_1e8 = _integer(
        funding.get("cap_1e8"),
        f"markets.{alias}.funding.cap_1e8",
    )
    if floor_1e8 > 0 or cap_1e8 < 0 or floor_1e8 > cap_1e8:
        _fail(
            "FUNDING_CAP",
            f"{alias} funding floor/cap do not bound zero",
        )

    margin = _object(
        descriptor.get("margin"),
        f"markets.{alias}.margin",
    )
    raw_tiers = _array(margin.get("tiers"), f"markets.{alias}.margin.tiers")
    if not raw_tiers:
        _fail("MARGIN_INVARIANT", f"{alias} has no margin tier")
    tiers = [
        _object(value, f"markets.{alias}.margin.tiers[{index}]")
        for index, value in enumerate(raw_tiers)
    ]
    caps = tuple(
        _integer(
            tier.get("notional_cap_micro"),
            f"markets.{alias}.margin.tiers[{index}].notional_cap_micro",
        )
        for index, tier in enumerate(tiers)
    )
    if any(caps[index - 1] >= caps[index] for index in range(1, len(caps))):
        _fail(
            "MARGIN_INVARIANT",
            f"{alias} margin tier caps are not strictly increasing",
        )
    max_leverage_tier = tiers[0]
    initial = _integer(
        max_leverage_tier.get("initial_rate_1e8"),
        f"markets.{alias}.margin.tiers[0].initial_rate_1e8",
    )
    maintenance = _integer(
        max_leverage_tier.get("maintenance_rate_1e8"),
        f"markets.{alias}.margin.tiers[0].maintenance_rate_1e8",
    )
    if maintenance * 2 != initial:
        _fail(
            "MARGIN_INVARIANT",
            f"{alias} max-leverage maintenance must equal half initial",
        )
    return _FundingPolicy(
        interval_ms=interval_ms,
        settlement_offsets_ms=offsets,
        floor_1e8=floor_1e8,
        cap_1e8=cap_1e8,
    )


def _expected_funding_timestamps(
    *,
    window_start_ts: int,
    window_end_ts: int,
    offsets: Sequence[int],
) -> tuple[int, ...]:
    first_day = window_start_ts - (window_start_ts % _DAY_MS)
    expected: list[int] = []
    for day_start in range(first_day, window_end_ts, _DAY_MS):
        for offset in offsets:
            stamp = day_start + offset
            if window_start_ts <= stamp < window_end_ts:
                expected.append(stamp)
    return tuple(expected)


def _validate_funding_rows(
    series: _Series,
    policy: _FundingPolicy,
    *,
    window_start_ts: int,
    window_end_ts: int,
) -> tuple[int, ...]:
    timestamps = _strict_timestamp_order(series)
    for index, row in enumerate(series.rows):
        ts = timestamps[index]
        available_at = _integer(
            row.get("available_at"),
            f"{series.path}[{index}].available_at",
        )
        if available_at != ts:
            _fail(
                "AVAILABLE_AT",
                f"{series.path}[{index}] must have available_at=ts",
            )
        if ts % _DAY_MS not in policy.settlement_offsets_ms:
            _fail(
                "FUNDING_SETTLEMENT",
                f"{series.path}[{index}] is off the declared settlement clock",
            )
        rate = _integer(
            row.get("rate_1e8"),
            f"{series.path}[{index}].rate_1e8",
        )
        if not policy.floor_1e8 <= rate <= policy.cap_1e8:
            _fail(
                "FUNDING_CAP",
                f"{series.path}[{index}] rate is outside the declared bounds",
            )
    if any(
        timestamps[index] - timestamps[index - 1] != policy.interval_ms
        for index in range(1, len(timestamps))
    ):
        _fail(
            "FUNDING_GAP",
            f"{series.path} does not advance by the exact funding interval",
        )
    expected = _expected_funding_timestamps(
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        offsets=policy.settlement_offsets_ms,
    )
    if timestamps != expected:
        _fail(
            "FUNDING_GAP",
            f"{series.path} does not exactly cover the pack window",
        )
    return timestamps


def _validate_bar_grid(
    *,
    market: str,
    interval_ms: int,
    role_timestamps: Mapping[str, tuple[int, ...]],
    window_start_ts: int,
    window_end_ts: int,
) -> None:
    trade = role_timestamps["trade"]
    if trade != role_timestamps["mark"] or trade != role_timestamps["index"]:
        _fail(
            "BAR_ALIGNMENT",
            f"{market}/{interval_ms} trade, mark, and index timestamps differ",
        )
    if (window_end_ts - window_start_ts) % interval_ms:
        _fail(
            "BAR_GAP",
            f"{market}/{interval_ms} window is not bar-aligned",
        )
    expected = tuple(range(window_start_ts, window_end_ts, interval_ms))
    if trade != expected:
        _fail(
            "BAR_GAP",
            f"{market}/{interval_ms} does not exactly cover the pack window",
        )


def _validate_mark_trade_divergence(
    trade: _Series,
    mark: _Series,
) -> None:
    for index, (trade_row, mark_row) in enumerate(
        zip(trade.rows, mark.rows, strict=True)
    ):
        trade_close = _integer(
            trade_row.get("c"),
            f"{trade.path}[{index}].c",
        )
        mark_close = _integer(
            mark_row.get("c"),
            f"{mark.path}[{index}].c",
        )
        if trade_close <= 0:
            _fail(
                "MARK_TRADE_DIVERGENCE",
                f"{trade.path}[{index}] has a non-positive trade close",
            )
        difference = abs(mark_close - trade_close)
        if (
            difference * 100_000_000
            > trade_close * MARK_TRADE_MAX_DIVERGENCE_1E8
        ):
            _fail(
                "MARK_TRADE_DIVERGENCE",
                f"{trade.market}/{trade.interval_ms} close divergence exceeds "
                f"{MARK_TRADE_MAX_DIVERGENCE_1E8}e-8 at row {index}",
            )


def validate_pack(pack_dir: str | Path) -> PackValidationResult:
    """Validate one fully built pack and return a compact success receipt."""

    root = Path(pack_dir)
    manifest_path = root / "manifest.json"
    try:
        manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise PackValidationError(
            f"MISSING_MANIFEST: cannot read {manifest_path}"
        ) from exc
    manifest = _object(
        _decode_json(manifest_raw, source="manifest.json"),
        "manifest.json",
    )
    _validate_schema("pack_manifest", manifest, context="manifest.json")
    try:
        canonical_manifest = canonical_bytes(manifest)
    except ValueError as exc:
        raise PackValidationError(
            f"MANIFEST_CANONICAL: manifest.json: {exc}"
        ) from exc
    if manifest_raw != canonical_manifest:
        _fail("MANIFEST_CANONICAL", "manifest.json is not JCS-canonical")

    window = _object(manifest.get("window"), "window")
    window_start_ts = _integer(window.get("start_ts"), "window.start_ts")
    window_end_ts = _integer(window.get("end_ts"), "window.end_ts")
    if window_end_ts <= window_start_ts:
        _fail("WINDOW", "window.end_ts must be after window.start_ts")

    raw_intervals = _array(
        manifest.get("bar_intervals_ms"),
        "bar_intervals_ms",
    )
    intervals = tuple(
        _integer(value, f"bar_intervals_ms[{index}]")
        for index, value in enumerate(raw_intervals)
    )
    if tuple(sorted(set(intervals))) != intervals:
        _fail(
            "MANIFEST_INTERVALS",
            "bar_intervals_ms must be sorted and unique",
        )
    decision_bar_ms = _integer(
        manifest.get("decision_bar_ms"),
        "decision_bar_ms",
    )
    if decision_bar_ms not in intervals:
        _fail(
            "MANIFEST_INTERVALS",
            "decision_bar_ms is not present in bar_intervals_ms",
        )

    raw_files = _array(manifest.get("files"), "files")
    entries = [
        _object(value, f"files[{index}]")
        for index, value in enumerate(raw_files)
    ]
    paths = tuple(
        _string(entry.get("path"), f"files[{index}].path")
        for index, entry in enumerate(entries)
    )
    if tuple(sorted(paths)) != paths:
        _fail("MANIFEST_FILE_ORDER", "manifest files must be sorted by path")
    if len(set(paths)) != len(paths):
        _fail("MANIFEST_FILE_ORDER", "manifest contains duplicate file paths")

    series = tuple(_read_series(root, entry) for entry in entries)
    projection_files = [
        {
            key: entry[key]
            for key in ("path", "sha256", "bytes", "records")
        }
        for entry in entries
    ]
    actual_content_hash = content_hash(
        {
            "schema": "pack_content/v1",
            "files": projection_files,
        }
    )
    expected_content_hash = _string(
        manifest.get("content_hash"),
        "content_hash",
    )
    if actual_content_hash != expected_content_hash:
        _fail(
            "CONTENT_HASH",
            f"expected {expected_content_hash}, found {actual_content_hash}",
        )

    market_values = _object(manifest.get("markets"), "markets")
    markets = {
        alias: _object(value, f"markets.{alias}")
        for alias, value in sorted(market_values.items())
    }
    venue = _string(manifest.get("venue"), "venue")
    policies = {
        alias: _funding_policy(
            venue=venue,
            alias=alias,
            descriptor=descriptor,
        )
        for alias, descriptor in markets.items()
    }

    by_key: dict[tuple[str, str, int], _Series] = {}
    for item in series:
        if item.market not in markets:
            _fail(
                "UNKNOWN_MARKET",
                f"{item.path} references undeclared market {item.market}",
            )
        if item.role == "funding":
            if item.interval_ms != 0:
                _fail("FUNDING_INTERVAL", f"{item.path} interval must be zero")
        elif item.interval_ms not in intervals:
            _fail(
                "MANIFEST_INTERVALS",
                f"{item.path} interval is not declared",
            )
        key = (item.market, item.role, item.interval_ms)
        if key in by_key:
            _fail("DUPLICATE_SERIES", f"duplicate series key {key!r}")
        by_key[key] = item

    bar_rows = 0
    funding_rows = 0
    decision_counts: list[int] = []
    for alias in sorted(markets):
        funding_key = (alias, "funding", 0)
        funding_series = by_key.get(funding_key)
        if funding_series is None:
            _fail("MISSING_SERIES", f"{alias} is missing funding")
        _validate_funding_rows(
            funding_series,
            policies[alias],
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
        )
        funding_rows += len(funding_series.rows)

        for interval_ms in intervals:
            role_series: dict[str, _Series] = {}
            for role in ("trade", "mark", "index"):
                role_item = by_key.get((alias, role, interval_ms))
                if role_item is None:
                    _fail(
                        "MISSING_SERIES",
                        f"{alias}/{interval_ms} is missing {role}",
                    )
                role_series[role] = role_item
            role_timestamps = {
                role: _validate_bar_rows(item)
                for role, item in sorted(role_series.items())
            }
            _validate_bar_grid(
                market=alias,
                interval_ms=interval_ms,
                role_timestamps=role_timestamps,
                window_start_ts=window_start_ts,
                window_end_ts=window_end_ts,
            )
            _validate_mark_trade_divergence(
                role_series["trade"],
                role_series["mark"],
            )
            bar_rows += sum(len(item.rows) for item in role_series.values())
            if interval_ms == decision_bar_ms:
                decision_counts.append(len(role_series["trade"].rows))

    expected_keys = {
        (alias, role, interval_ms)
        for alias in markets
        for interval_ms in intervals
        for role in ("trade", "mark", "index")
    } | {(alias, "funding", 0) for alias in markets}
    unexpected = sorted(set(by_key) - expected_keys)
    if unexpected:
        _fail("UNEXPECTED_SERIES", f"undeclared series matrices: {unexpected!r}")

    warmup_bars = _integer(manifest.get("warmup_bars"), "warmup_bars")
    if not decision_counts:
        _fail("WARMUP_LENGTH", "pack has no decision-interval trade series")
    actionable_bars = min(decision_counts) - warmup_bars - 1
    if actionable_bars < 1:
        _fail(
            "WARMUP_LENGTH",
            "warmup leaves no decision turn plus final holding bar",
        )

    return PackValidationResult(
        pack_id=_string(manifest.get("pack_id"), "pack_id"),
        content_hash=expected_content_hash,
        files=len(series),
        bar_rows=bar_rows,
        funding_rows=funding_rows,
        actionable_bars=actionable_bars,
    )
