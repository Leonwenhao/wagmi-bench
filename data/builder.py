# SPDX-License-Identifier: Apache-2.0
"""Deterministic Binance raw-archive to IC-1 pack builder."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from data.binance import (
    ArchiveSource,
    VerifiedArchive,
    read_verified_csv_rows,
    verify_archive,
)
from spec.canonical import canonical_bytes, content_hash, sha256_prefixed

SeriesRole = Literal["trade", "mark", "index", "funding"]
TimestampUnit = Literal["ms", "us"]

_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_MARKET_ALIAS_RE = re.compile(r"^[A-Z0-9]{1,12}$")
_DECIMAL_RE = re.compile(
    r"^(?P<sign>[+-]?)(?P<whole>[0-9]+)"
    r"(?:\.(?P<fraction>[0-9]*))?"
    r"(?:[eE](?P<exponent>[+-]?[0-9]+))?$"
)
_BAR_ROLES: tuple[SeriesRole, ...] = ("trade", "mark", "index")
_ROLE_ORDER: dict[SeriesRole, int] = {
    "trade": 0,
    "mark": 1,
    "index": 2,
    "funding": 3,
}
_INTERVAL_LABELS = {3_600_000: "1h", 14_400_000: "4h"}
_DAY_MS = 86_400_000
_FUNDING_STAMP_JITTER_TOLERANCE_MS = 1_000


class PackBuildError(Exception):
    """Raised when raw inputs cannot deterministically form an IC-1 pack."""


@dataclass(frozen=True, slots=True)
class RawSeriesArchive:
    """One verified-at-build-time archive contributing to a pack series."""

    role: SeriesRole
    interval_ms: int
    source: ArchiveSource
    member_name: str | None = None
    timestamp_unit: TimestampUnit = "ms"

    def __post_init__(self) -> None:
        if self.role == "funding":
            if self.interval_ms != 0:
                raise PackBuildError("funding archives must use interval_ms=0")
        elif self.interval_ms not in _INTERVAL_LABELS:
            raise PackBuildError(
                "bar archive interval_ms must be 3600000 or 14400000"
            )
        if self.timestamp_unit not in {"ms", "us"}:
            raise PackBuildError(
                f"unsupported timestamp unit: {self.timestamp_unit!r}"
            )


@dataclass(frozen=True, slots=True)
class PackBuildConfig:
    """Operator-authored metadata and venue parameters for one pack."""

    pack_id: str
    market_alias: str
    market_descriptor: Mapping[str, object]
    window_start_ts: int
    window_end_ts: int
    decision_bar_ms: int
    warmup_bars: int
    default_lookback_bars: int
    default_funding_prints: int
    regime_description: str
    created_by_version: str
    venue: str = "binance-um"

    def __post_init__(self) -> None:
        if _PACK_ID_RE.fullmatch(self.pack_id) is None:
            raise PackBuildError(f"invalid pack_id: {self.pack_id!r}")
        if _MARKET_ALIAS_RE.fullmatch(self.market_alias) is None:
            raise PackBuildError(
                f"invalid market alias: {self.market_alias!r}"
            )
        if self.window_start_ts < 0 or self.window_end_ts <= self.window_start_ts:
            raise PackBuildError("pack window must be non-negative and non-empty")
        if self.decision_bar_ms not in _INTERVAL_LABELS:
            raise PackBuildError(
                "decision_bar_ms must be 3600000 or 14400000"
            )
        if self.warmup_bars < 0:
            raise PackBuildError("warmup_bars must be non-negative")
        if self.default_lookback_bars < 1:
            raise PackBuildError("default_lookback_bars must be positive")
        if self.default_funding_prints < 0:
            raise PackBuildError(
                "default_funding_prints must be non-negative"
            )
        if not self.venue:
            raise PackBuildError("venue must be non-empty")
        if not self.regime_description:
            raise PackBuildError("regime_description must be non-empty")
        if not self.created_by_version:
            raise PackBuildError("created_by_version must be non-empty")


@dataclass(frozen=True, slots=True)
class BuiltPack:
    """Paths and content identity returned after a successful pack build."""

    directory: Path
    manifest_path: Path
    content_hash: str
    series_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _BuiltSeries:
    path: str
    payload: bytes
    metadata: dict[str, object]


def build_pack(
    config: PackBuildConfig,
    sources: Sequence[RawSeriesArchive],
    output_dir: Path,
) -> BuiltPack:
    """Build a deterministic IC-1 pack after verifying every source archive."""

    if output_dir.exists():
        raise PackBuildError(f"output directory already exists: {output_dir}")
    if not sources:
        raise PackBuildError("at least one raw archive is required")

    source_list = list(sources)
    _validate_source_matrix(config, source_list)

    # This complete pass happens before any ZIP member is opened. If one
    # checksum fails, no archive is read and no output directory is created.
    verified: list[tuple[RawSeriesArchive, VerifiedArchive]] = [
        (source, verify_archive(source.source)) for source in source_list
    ]

    grouped: dict[
        tuple[SeriesRole, int], list[tuple[RawSeriesArchive, VerifiedArchive]]
    ] = {}
    for source, archive in verified:
        grouped.setdefault((source.role, source.interval_ms), []).append(
            (source, archive)
        )

    tick_size_micro = _required_positive_int(
        config.market_descriptor, "tick_size_micro"
    )
    funding_interval_ms, settlement_offsets_ms = _funding_schedule(
        config.market_descriptor
    )
    built: list[_BuiltSeries] = []
    for key in sorted(
        grouped, key=lambda item: (_ROLE_ORDER[item[0]], item[1])
    ):
        role, interval_ms = key
        contributors = sorted(grouped[key], key=lambda item: item[0].source.url)
        records: list[dict[str, int]] = []
        upstream: list[dict[str, object]] = []
        for source, archive in contributors:
            csv_rows = read_verified_csv_rows(
                archive, member_name=source.member_name
            )
            records.extend(
                _convert_rows(
                    role=role,
                    interval_ms=interval_ms,
                    rows=csv_rows,
                    timestamp_unit=source.timestamp_unit,
                    tick_size_micro=tick_size_micro,
                    funding_interval_ms=funding_interval_ms,
                    settlement_offsets_ms=settlement_offsets_ms,
                    window_start_ts=config.window_start_ts,
                    window_end_ts=config.window_end_ts,
                    source_url=source.source.url,
                )
            )
            upstream.append(
                {"url": source.source.url, "sha256": archive.sha256}
            )

        ordered = _sort_unique_records(records, role=role)
        if not ordered:
            raise PackBuildError(
                f"{role}/{interval_ms} produced no rows inside the pack window"
            )
        payload = b"".join(canonical_bytes(row) + b"\n" for row in ordered)
        relative_path = _series_path(role, interval_ms)
        row_schema = (
            "funding_row/v1" if role == "funding" else "bar_row/v1"
        )
        metadata: dict[str, object] = {
            "path": relative_path,
            "role": role,
            "market": config.market_alias,
            "interval_ms": interval_ms,
            "row_schema": row_schema,
            "sha256": sha256_prefixed(payload),
            "bytes": len(payload),
            "records": len(ordered),
            "upstream": upstream,
        }
        built.append(
            _BuiltSeries(
                path=relative_path,
                payload=payload,
                metadata=metadata,
            )
        )

    built.sort(key=lambda item: item.path)
    file_metadata = [item.metadata for item in built]
    content_projection = {
        "schema": "pack_content/v1",
        "files": [
            {
                key: metadata[key]
                for key in ("path", "sha256", "bytes", "records")
            }
            for metadata in file_metadata
        ],
    }
    pack_content_hash = content_hash(content_projection)
    bar_intervals = sorted(
        {source.interval_ms for source in source_list if source.role != "funding"}
    )
    manifest: dict[str, object] = {
        "schema": "pack_manifest/v1",
        "pack_id": config.pack_id,
        "content_hash": pack_content_hash,
        "venue": config.venue,
        "window": {
            "start_ts": config.window_start_ts,
            "end_ts": config.window_end_ts,
        },
        "bar_intervals_ms": bar_intervals,
        "decision_bar_ms": config.decision_bar_ms,
        "warmup_bars": config.warmup_bars,
        "markets": {
            config.market_alias: dict(config.market_descriptor),
        },
        "files": file_metadata,
        "default_lookback": {
            "bars": config.default_lookback_bars,
            "funding_prints": config.default_funding_prints,
        },
        "regime_description": config.regime_description,
        "claim_label": "survival-stress",
        "created_by_version": config.created_by_version,
    }
    manifest_payload = canonical_bytes(manifest)

    output_dir.mkdir(parents=True)
    series_paths: list[Path] = []
    for item in built:
        path = output_dir / item.path
        path.write_bytes(item.payload)
        series_paths.append(path)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(manifest_payload)
    return BuiltPack(
        directory=output_dir,
        manifest_path=manifest_path,
        content_hash=pack_content_hash,
        series_paths=tuple(series_paths),
    )


def _validate_source_matrix(
    config: PackBuildConfig, sources: Sequence[RawSeriesArchive]
) -> None:
    seen: set[tuple[SeriesRole, int, str]] = set()
    bar_intervals: set[int] = set()
    funding_count = 0
    role_intervals: set[tuple[SeriesRole, int]] = set()
    for source in sources:
        identity = (source.role, source.interval_ms, source.source.url)
        if identity in seen:
            raise PackBuildError(f"duplicate raw source: {identity!r}")
        seen.add(identity)
        role_intervals.add((source.role, source.interval_ms))
        if source.role == "funding":
            funding_count += 1
        else:
            bar_intervals.add(source.interval_ms)
    if funding_count == 0:
        raise PackBuildError("at least one funding archive is required")
    for interval in bar_intervals:
        missing = [
            role for role in _BAR_ROLES if (role, interval) not in role_intervals
        ]
        if missing:
            raise PackBuildError(
                f"bar interval {interval} is missing roles: {missing}"
            )
    if config.decision_bar_ms not in bar_intervals:
        raise PackBuildError(
            "decision_bar_ms has no trade/mark/index source matrix"
        )


def _convert_rows(
    *,
    role: SeriesRole,
    interval_ms: int,
    rows: Sequence[Sequence[str]],
    timestamp_unit: TimestampUnit,
    tick_size_micro: int,
    funding_interval_ms: int,
    settlement_offsets_ms: tuple[int, ...],
    window_start_ts: int,
    window_end_ts: int,
    source_url: str,
) -> list[dict[str, int]]:
    if role == "funding":
        return _convert_funding_rows(
            rows=rows,
            timestamp_unit=timestamp_unit,
            funding_interval_ms=funding_interval_ms,
            settlement_offsets_ms=settlement_offsets_ms,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            source_url=source_url,
        )
    return _convert_bar_rows(
        role=role,
        interval_ms=interval_ms,
        rows=rows,
        timestamp_unit=timestamp_unit,
        tick_size_micro=tick_size_micro,
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        source_url=source_url,
    )


def _convert_bar_rows(
    *,
    role: SeriesRole,
    interval_ms: int,
    rows: Sequence[Sequence[str]],
    timestamp_unit: TimestampUnit,
    tick_size_micro: int,
    window_start_ts: int,
    window_end_ts: int,
    source_url: str,
) -> list[dict[str, int]]:
    start, columns = _bar_columns(rows)
    converted: list[dict[str, int]] = []
    for index, row in enumerate(rows[start:], start=start + 1):
        if not row or all(not value.strip() for value in row):
            continue
        label = f"{source_url} CSV row {index}"
        _require_columns(row, columns.values(), label)
        ts = _timestamp_ms(row[columns["ts"]], timestamp_unit, f"{label} ts")
        if ts < window_start_ts or ts >= window_end_ts:
            continue
        open_ticks = _price_ticks(
            row[columns["open"]],
            tick_size_micro,
            f"{label} open",
            rounding="nearest",
        )
        high_ticks = _price_ticks(
            row[columns["high"]],
            tick_size_micro,
            f"{label} high",
            rounding="ceil",
        )
        low_ticks = _price_ticks(
            row[columns["low"]],
            tick_size_micro,
            f"{label} low",
            rounding="floor",
        )
        close_ticks = _price_ticks(
            row[columns["close"]],
            tick_size_micro,
            f"{label} close",
            rounding="nearest",
        )
        if low_ticks > min(open_ticks, close_ticks, high_ticks):
            raise PackBuildError(f"{label}: low exceeds another OHLC value")
        if high_ticks < max(open_ticks, close_ticks, low_ticks):
            raise PackBuildError(f"{label}: high is below another OHLC value")
        volume = (
            _nonnegative(
                _scaled_decimal(
                    row[columns["volume"]], 100_000_000, f"{label} volume"
                ),
                f"{label} volume",
            )
            if role == "trade"
            else 0
        )
        converted.append(
            {
                "ts": ts,
                "available_at": ts + interval_ms,
                "o": open_ticks,
                "h": high_ticks,
                "l": low_ticks,
                "c": close_ticks,
                "v_base_1e8": volume,
            }
        )
    return converted


def _convert_funding_rows(
    *,
    rows: Sequence[Sequence[str]],
    timestamp_unit: TimestampUnit,
    funding_interval_ms: int,
    settlement_offsets_ms: tuple[int, ...],
    window_start_ts: int,
    window_end_ts: int,
    source_url: str,
) -> list[dict[str, int]]:
    start, columns = _funding_columns(rows)
    converted: list[dict[str, int]] = []
    for index, row in enumerate(rows[start:], start=start + 1):
        if not row or all(not value.strip() for value in row):
            continue
        label = f"{source_url} CSV row {index}"
        _require_columns(row, columns.values(), label)
        raw_ts = _timestamp_ms(
            row[columns["ts"]],
            timestamp_unit,
            f"{label} ts",
        )
        ts = _normalize_funding_timestamp(
            raw_ts,
            settlement_offsets_ms,
            label=f"{label} ts",
        )
        if ts < window_start_ts or ts >= window_end_ts:
            continue
        interval_column = columns.get("interval_hours")
        if interval_column is not None:
            observed_interval_ms = _scaled_decimal(
                row[interval_column],
                3_600_000,
                f"{label} funding interval hours",
            )
            if observed_interval_ms != funding_interval_ms:
                raise PackBuildError(
                    f"{label}: funding interval {observed_interval_ms} ms "
                    f"does not match market descriptor {funding_interval_ms} ms"
                )
        converted.append(
            {
                "ts": ts,
                "available_at": ts,
                "rate_1e8": _scaled_decimal(
                    row[columns["rate"]],
                    100_000_000,
                    f"{label} funding rate",
                ),
            }
        )
    return converted


def _bar_columns(
    rows: Sequence[Sequence[str]],
) -> tuple[int, dict[str, int]]:
    if not rows:
        raise PackBuildError("bar CSV is empty")
    first = rows[0]
    if first and not _is_integer(first[0]):
        header = _header_map(first)
        return 1, {
            "ts": _header_index(header, ("opentime", "timestamp", "ts")),
            "open": _header_index(header, ("open",)),
            "high": _header_index(header, ("high",)),
            "low": _header_index(header, ("low",)),
            "close": _header_index(header, ("close",)),
            "volume": _header_index(
                header, ("volume", "baseassetvolume", "vol")
            ),
        }
    return 0, {
        "ts": 0,
        "open": 1,
        "high": 2,
        "low": 3,
        "close": 4,
        "volume": 5,
    }


def _funding_columns(
    rows: Sequence[Sequence[str]],
) -> tuple[int, dict[str, int]]:
    if not rows:
        raise PackBuildError("funding CSV is empty")
    first = rows[0]
    if first and not _is_integer(first[0]):
        header = _header_map(first)
        result = {
            "ts": _header_index(
                header, ("calctime", "fundingtime", "timestamp", "time", "ts")
            ),
            "rate": _header_index(
                header, ("lastfundingrate", "fundingrate", "rate")
            ),
        }
        interval_index = header.get("fundingintervalhours")
        if interval_index is not None:
            result["interval_hours"] = interval_index
        return 1, result
    if len(first) >= 3:
        # Binance bulk fundingRate archives use:
        # calc_time,funding_interval_hours,last_funding_rate
        return 0, {"ts": 0, "interval_hours": 1, "rate": 2}
    if len(first) == 2:
        return 0, {"ts": 0, "rate": 1}
    raise PackBuildError("funding CSV needs at least timestamp and rate columns")


def _sort_unique_records(
    records: Sequence[dict[str, int]], *, role: SeriesRole
) -> list[dict[str, int]]:
    ordered = sorted(records, key=lambda row: row["ts"])
    previous: int | None = None
    for row in ordered:
        current = row["ts"]
        if current == previous:
            raise PackBuildError(f"duplicate {role} timestamp: {current}")
        previous = current
    return ordered


def _series_path(role: SeriesRole, interval_ms: int) -> str:
    if role == "funding":
        return "funding.jsonl"
    label = _INTERVAL_LABELS.get(interval_ms)
    if label is None:
        raise PackBuildError(f"unsupported bar interval: {interval_ms}")
    prefix = {"trade": "bars", "mark": "mark", "index": "index"}[role]
    return f"{prefix}_{label}.jsonl"


def _timestamp_ms(value: str, unit: TimestampUnit, label: str) -> int:
    try:
        raw = int(value.strip())
    except ValueError as exc:
        raise PackBuildError(f"{label} must be an integer: {value!r}") from exc
    if raw < 0:
        raise PackBuildError(f"{label} must be non-negative")
    if unit == "ms":
        return raw
    if raw % 1000:
        raise PackBuildError(
            f"{label} microseconds must convert exactly to milliseconds"
        )
    return raw // 1000


def _price_ticks(
    value: str,
    tick_size_micro: int,
    label: str,
    *,
    rounding: Literal["floor", "ceil", "nearest"],
) -> int:
    """Convert an upstream decimal price to the pack's integer tick quantum.

    Binance mark and index archives carry more decimal places than the
    instrument's tradable price tick.  The pack has one frozen price quantum
    for all three series, so conversion must quantize those observed values
    explicitly rather than reject real archives or instantiate a float.

    Highs round outward up and lows outward down so liquidation-wick evidence
    is conservative.  Opens and closes use nearest tick with exact half-ticks
    rounded up.  Prices are non-negative, making floor/ceil unambiguous.
    """

    text = value.strip()
    match = _DECIMAL_RE.fullmatch(text)
    if match is None:
        raise PackBuildError(f"{label} is not an exact decimal: {value!r}")
    if match.group("sign") == "-":
        raise PackBuildError(f"{label} must be non-negative")

    fraction = match.group("fraction") or ""
    digits = int(f"{match.group('whole')}{fraction}")
    exponent = int(match.group("exponent") or "0") - len(fraction)
    if exponent >= 0:
        numerator = digits * _power_of_ten(exponent) * 1_000_000
        denominator = tick_size_micro
    else:
        numerator = digits * 1_000_000
        denominator = _power_of_ten(-exponent) * tick_size_micro

    quotient, remainder = divmod(numerator, denominator)
    if rounding == "ceil" and remainder:
        return quotient + 1
    if rounding == "nearest" and remainder * 2 >= denominator:
        return quotient + 1
    return quotient


def _scaled_decimal(value: str, scale: int, label: str) -> int:
    text = value.strip()
    match = _DECIMAL_RE.fullmatch(text)
    if match is None:
        raise PackBuildError(f"{label} is not an exact decimal: {value!r}")
    sign = -1 if match.group("sign") == "-" else 1
    fraction = match.group("fraction") or ""
    digits = int(f"{match.group('whole')}{fraction}")
    exponent = int(match.group("exponent") or "0")
    decimal_power = exponent - len(fraction)
    if decimal_power >= 0:
        return sign * digits * _power_of_ten(decimal_power) * scale
    numerator = sign * digits * scale
    denominator = _power_of_ten(-decimal_power)
    quotient, remainder = divmod(abs(numerator), denominator)
    if remainder:
        raise PackBuildError(
            f"{label}={value!r} cannot be represented exactly at scale {scale}"
        )
    return quotient if numerator >= 0 else -quotient


def _required_positive_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if type(value) is not int or value <= 0:
        raise PackBuildError(f"market_descriptor.{key} must be a positive int")
    return value


def _funding_schedule(
    market_descriptor: Mapping[str, object],
) -> tuple[int, tuple[int, ...]]:
    funding = market_descriptor.get("funding")
    if not isinstance(funding, Mapping):
        raise PackBuildError("market_descriptor.funding must be an object")
    interval = funding.get("interval_ms")
    if type(interval) is not int or interval <= 0:
        raise PackBuildError(
            "market_descriptor.funding.interval_ms must be a positive int"
        )
    raw_offsets = funding.get("settlement_offsets_ms")
    if not isinstance(raw_offsets, list) or not raw_offsets:
        raise PackBuildError(
            "market_descriptor.funding.settlement_offsets_ms must be a "
            "non-empty array"
        )
    offsets: list[int] = []
    for value in raw_offsets:
        if type(value) is not int or not 0 <= value < _DAY_MS:
            raise PackBuildError(
                "funding settlement offsets must be integer milliseconds "
                "inside one UTC day"
            )
        offsets.append(value)
    if len(set(offsets)) != len(offsets):
        raise PackBuildError("funding settlement offsets must be unique")
    return interval, tuple(sorted(offsets))


def _normalize_funding_timestamp(
    raw_ts: int,
    settlement_offsets_ms: tuple[int, ...],
    *,
    label: str,
) -> int:
    """Snap Binance's sub-second calc_time jitter to the venue clock."""

    day_start = raw_ts - (raw_ts % _DAY_MS)
    candidates = [
        base + offset
        for base in (
            day_start - _DAY_MS,
            day_start,
            day_start + _DAY_MS,
        )
        for offset in settlement_offsets_ms
    ]
    normalized = min(candidates, key=lambda item: (abs(item - raw_ts), item))
    jitter = abs(normalized - raw_ts)
    if jitter > _FUNDING_STAMP_JITTER_TOLERANCE_MS:
        raise PackBuildError(
            f"{label}={raw_ts} is {jitter} ms from the nearest declared "
            "funding settlement"
        )
    return normalized


def _nonnegative(value: int, label: str) -> int:
    if value < 0:
        raise PackBuildError(f"{label} must be non-negative")
    return value


def _header_map(row: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, value in enumerate(row):
        token = "".join(char for char in value.lower() if char.isalnum())
        if token:
            if token in result:
                raise PackBuildError(f"duplicate CSV header: {value!r}")
            result[token] = index
    return result


def _header_index(
    header: Mapping[str, int], candidates: Sequence[str]
) -> int:
    for candidate in candidates:
        if candidate in header:
            return header[candidate]
    raise PackBuildError(
        f"CSV header is missing one of the required columns: {list(candidates)}"
    )


def _require_columns(
    row: Sequence[str], indices: Iterable[int], label: str
) -> None:
    maximum = max(indices)
    if len(row) <= maximum:
        raise PackBuildError(
            f"{label} has {len(row)} columns; requires index {maximum}"
        )


def _is_integer(value: str) -> bool:
    try:
        int(value.strip())
    except ValueError:
        return False
    return True


def _power_of_ten(exponent: int) -> int:
    if exponent < 0:
        raise PackBuildError("internal decimal exponent must be non-negative")
    result = 1
    for _ in range(exponent):
        result *= 10
    return result
