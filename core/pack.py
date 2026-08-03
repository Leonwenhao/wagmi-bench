# SPDX-License-Identifier: Apache-2.0
"""Typed, float-rejecting pack loading with strict series separation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Mapping, cast

from core.config import EffectiveLookback, rebase_timestamp

SHA256_PREFIX: Final = "sha256:"


class PackError(ValueError):
    """Raised when a pack cannot be loaded without ambiguity."""


def _reject_fractional_number(token: str) -> None:
    raise PackError(f"fractional JSON number is forbidden: {token}")


def _reject_nonfinite_number(token: str) -> None:
    raise PackError(f"non-finite JSON number is forbidden: {token}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PackError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, *, source: Path) -> object:
    try:
        text = raw.decode("utf-8")
        return cast(
            object,
            json.loads(
                text,
                parse_float=_reject_fractional_number,
                parse_constant=_reject_nonfinite_number,
                object_pairs_hook=_reject_duplicate_keys,
            ),
        )
    except UnicodeDecodeError as exc:
        raise PackError(f"{source}: invalid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise PackError(f"{source}: invalid JSON at byte {exc.pos}") from exc


def _as_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise PackError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _as_array(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise PackError(f"{field} must be an array")
    return cast(list[object], value)


def _as_int(value: object, field: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PackError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise PackError(f"{field} must be >= {minimum}")
    return value


def _as_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise PackError(f"{field} must be a string")
    return value


@dataclass(frozen=True, slots=True)
class RowProvenance:
    """The exact stored row behind an emitted value."""

    market: str
    role: str
    path: str
    row_index: int
    ts: int
    available_at: int


@dataclass(frozen=True, slots=True)
class BarRow:
    ts: int
    available_at: int
    o: int
    h: int
    l: int
    c: int
    v_base_1e8: int
    provenance: RowProvenance

    def observation_mapping(self, *, offset_ms: int) -> dict[str, int]:
        return {
            "ts": rebase_timestamp(self.ts, offset_ms=offset_ms),
            "available_at": rebase_timestamp(
                self.available_at,
                offset_ms=offset_ms,
            ),
            "o": self.o,
            "h": self.h,
            "l": self.l,
            "c": self.c,
            "v_base_1e8": self.v_base_1e8,
        }


@dataclass(frozen=True, slots=True)
class FundingRow:
    ts: int
    available_at: int
    rate_1e8: int
    provenance: RowProvenance

    def observation_mapping(self, *, offset_ms: int) -> dict[str, int]:
        return {
            "ts": rebase_timestamp(self.ts, offset_ms=offset_ms),
            "rate_1e8": self.rate_1e8,
        }


@dataclass(frozen=True, slots=True)
class FundingSpec:
    interval_ms: int
    settlement_offsets_ms: tuple[int, ...]
    cap_1e8: int
    floor_1e8: int


@dataclass(frozen=True, slots=True)
class MarginTier:
    notional_cap_micro: int
    initial_rate_1e8: int
    maintenance_rate_1e8: int


@dataclass(frozen=True, slots=True)
class MarketSpec:
    alias: str
    tick_size_micro: int
    qty_step_base_1e8: int
    min_notional_micro: int
    maker_fee_rate_1e8: int
    taker_fee_rate_1e8: int
    leverage_cap_lev_1e4: int
    margin_tiers: tuple[MarginTier, ...]
    liquidation_penalty_1e8: int
    funding: FundingSpec
    half_spread_1e8: int
    impact_model: Literal["linear", "sqrt"]
    impact_coeff_1e8: int
    participation_cap_1e8: int
    cost_profile_multipliers_1e4: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class MarketData:
    """One market's never-conflated source streams."""

    spec: MarketSpec
    trade: tuple[BarRow, ...]
    mark: tuple[BarRow, ...]
    index: tuple[BarRow, ...]
    funding: tuple[FundingRow, ...]

    def available_trade(self, clock_ts: int) -> tuple[BarRow, ...]:
        return tuple(row for row in self.trade if row.available_at <= clock_ts)

    def available_mark(self, clock_ts: int) -> tuple[BarRow, ...]:
        return tuple(row for row in self.mark if row.available_at <= clock_ts)

    def available_index(self, clock_ts: int) -> tuple[BarRow, ...]:
        return tuple(row for row in self.index if row.available_at <= clock_ts)

    def available_funding(self, clock_ts: int) -> tuple[FundingRow, ...]:
        return tuple(row for row in self.funding if row.available_at <= clock_ts)


@dataclass(frozen=True, slots=True)
class PackData:
    """The deterministic engine-facing subset of an IC-1 pack."""

    root: Path
    pack_id: str
    content_hash: str
    window_start_ts: int
    window_end_ts: int
    decision_bar_ms: int
    warmup_bars: int
    default_lookback: EffectiveLookback
    markets: Mapping[str, MarketData]
    manifest: Mapping[str, object]

    @property
    def market_aliases(self) -> tuple[str, ...]:
        return tuple(sorted(self.markets))

    @property
    def bars_total(self) -> int:
        """Number of actionable bars, retaining one final holding bar."""

        shortest = min(len(market.trade) for market in self.markets.values())
        total = shortest - self.warmup_bars - 1
        if total < 1:
            raise PackError("pack has no actionable decision bars")
        return total

    def market(self, alias: str) -> MarketData:
        try:
            return self.markets[alias]
        except KeyError as exc:
            raise PackError(f"unknown market: {alias}") from exc

    def clock_real_ts(self, turn: int) -> int:
        """Return the scheduled close for ``turn``.

        This is derived from the pack window and decision interval, never from
        a row's stored ``available_at``.  The leakage fixture intentionally
        contains a future ``available_at`` that must not advance this clock.
        """

        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 0:
            raise PackError("turn must be a non-negative integer")
        if turn >= self.bars_total:
            raise PackError(
                f"turn {turn} is outside episode range 0..{self.bars_total - 1}"
            )
        close_number = self.warmup_bars + turn + 1
        clock = self.window_start_ts + close_number * self.decision_bar_ms
        if clock > self.window_end_ts:
            raise PackError("scheduled virtual clock exceeds pack window")
        return clock


def _load_rows(
    *,
    raw: bytes,
    source: Path,
    market: str,
    role: str,
) -> tuple[BarRow, ...] | tuple[FundingRow, ...]:
    lines = raw.splitlines()
    if any(not line for line in lines):
        raise PackError(f"{source}: blank JSONL record")

    if role == "funding":
        funding_rows: list[FundingRow] = []
        for index, line in enumerate(lines):
            item = _as_object(_decode_json(line, source=source), f"{source}:{index}")
            ts = _as_int(item.get("ts"), f"{source}:{index}.ts", minimum=0)
            available_at = _as_int(
                item.get("available_at"),
                f"{source}:{index}.available_at",
                minimum=0,
            )
            funding_rows.append(
                FundingRow(
                    ts=ts,
                    available_at=available_at,
                    rate_1e8=_as_int(
                        item.get("rate_1e8"),
                        f"{source}:{index}.rate_1e8",
                    ),
                    provenance=RowProvenance(
                        market=market,
                        role=role,
                        path=source.name,
                        row_index=index,
                        ts=ts,
                        available_at=available_at,
                    ),
                )
            )
        return tuple(funding_rows)

    bar_rows: list[BarRow] = []
    for index, line in enumerate(lines):
        item = _as_object(_decode_json(line, source=source), f"{source}:{index}")
        ts = _as_int(item.get("ts"), f"{source}:{index}.ts", minimum=0)
        available_at = _as_int(
            item.get("available_at"),
            f"{source}:{index}.available_at",
            minimum=0,
        )
        bar_rows.append(
            BarRow(
                ts=ts,
                available_at=available_at,
                o=_as_int(item.get("o"), f"{source}:{index}.o", minimum=0),
                h=_as_int(item.get("h"), f"{source}:{index}.h", minimum=0),
                l=_as_int(item.get("l"), f"{source}:{index}.l", minimum=0),
                c=_as_int(item.get("c"), f"{source}:{index}.c", minimum=0),
                v_base_1e8=_as_int(
                    item.get("v_base_1e8"),
                    f"{source}:{index}.v_base_1e8",
                    minimum=0,
                ),
                provenance=RowProvenance(
                    market=market,
                    role=role,
                    path=source.name,
                    row_index=index,
                    ts=ts,
                    available_at=available_at,
                ),
            )
        )
    return tuple(bar_rows)


def _safe_pack_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root):
        raise PackError(f"series path escapes pack directory: {relative}")
    return candidate


def _read_declared_file(
    root: Path,
    entry: Mapping[str, object],
    *,
    verify_integrity: bool,
) -> tuple[Path, bytes]:
    relative = _as_str(entry.get("path"), "files[].path")
    path = _safe_pack_path(root, relative)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PackError(f"cannot read series file: {relative}") from exc

    if verify_integrity:
        expected_bytes = _as_int(entry.get("bytes"), f"{relative}.bytes", minimum=0)
        if len(raw) != expected_bytes:
            raise PackError(f"{relative}: byte count mismatch")
        expected_hash = _as_str(entry.get("sha256"), f"{relative}.sha256")
        actual_hash = SHA256_PREFIX + hashlib.sha256(raw).hexdigest()
        if actual_hash != expected_hash:
            raise PackError(f"{relative}: SHA-256 mismatch")
        expected_records = _as_int(
            entry.get("records"),
            f"{relative}.records",
            minimum=0,
        )
        if len(raw.splitlines()) != expected_records:
            raise PackError(f"{relative}: record count mismatch")
    return path, raw


def _strictly_increasing(values: tuple[int, ...]) -> bool:
    return all(
        values[index - 1] < values[index]
        for index in range(1, len(values))
    )


def _market_spec(alias: str, raw: Mapping[str, object]) -> MarketSpec:
    funding = _as_object(raw.get("funding"), f"markets.{alias}.funding")
    fees = _as_object(raw.get("fees"), f"markets.{alias}.fees")
    margin = _as_object(raw.get("margin"), f"markets.{alias}.margin")
    execution = _as_object(raw.get("execution"), f"markets.{alias}.execution")
    profile_raw = _as_object(
        execution.get("cost_profile_multipliers_1e4"),
        f"markets.{alias}.execution.cost_profile_multipliers_1e4",
    )
    profiles = {
        name: _as_int(value, f"markets.{alias}.cost_profiles.{name}", minimum=1)
        for name, value in sorted(profile_raw.items())
    }
    offsets = tuple(
        _as_int(
            item,
            f"markets.{alias}.funding.settlement_offsets_ms",
            minimum=0,
        )
        for item in _as_array(
            funding.get("settlement_offsets_ms"),
            f"markets.{alias}.funding.settlement_offsets_ms",
        )
    )
    if not offsets:
        raise PackError(f"markets.{alias}.funding settlement offsets are empty")
    tiers: list[MarginTier] = []
    for index, item in enumerate(
        _as_array(margin.get("tiers"), f"markets.{alias}.margin.tiers")
    ):
        tier = _as_object(item, f"markets.{alias}.margin.tiers[{index}]")
        tiers.append(
            MarginTier(
                notional_cap_micro=_as_int(
                    tier.get("notional_cap_micro"),
                    f"markets.{alias}.margin.tiers[{index}].notional_cap_micro",
                    minimum=1,
                ),
                initial_rate_1e8=_as_int(
                    tier.get("initial_rate_1e8"),
                    f"markets.{alias}.margin.tiers[{index}].initial_rate_1e8",
                    minimum=1,
                ),
                maintenance_rate_1e8=_as_int(
                    tier.get("maintenance_rate_1e8"),
                    f"markets.{alias}.margin.tiers[{index}].maintenance_rate_1e8",
                    minimum=1,
                ),
            )
        )
    if not tiers:
        raise PackError(f"markets.{alias}.margin.tiers must not be empty")
    impact_model_raw = _as_str(
        execution.get("impact_model"),
        f"markets.{alias}.execution.impact_model",
    )
    if impact_model_raw not in {"linear", "sqrt"}:
        raise PackError(f"markets.{alias}.execution.impact_model is invalid")
    impact_model = cast(Literal["linear", "sqrt"], impact_model_raw)
    return MarketSpec(
        alias=alias,
        tick_size_micro=_as_int(
            raw.get("tick_size_micro"),
            f"markets.{alias}.tick_size_micro",
            minimum=1,
        ),
        qty_step_base_1e8=_as_int(
            raw.get("qty_step_base_1e8"),
            f"markets.{alias}.qty_step_base_1e8",
            minimum=1,
        ),
        min_notional_micro=_as_int(
            raw.get("min_notional_micro"),
            f"markets.{alias}.min_notional_micro",
            minimum=0,
        ),
        maker_fee_rate_1e8=_as_int(
            fees.get("maker_rate_1e8"),
            f"markets.{alias}.fees.maker_rate_1e8",
            minimum=0,
        ),
        taker_fee_rate_1e8=_as_int(
            fees.get("taker_rate_1e8"),
            f"markets.{alias}.fees.taker_rate_1e8",
            minimum=0,
        ),
        leverage_cap_lev_1e4=_as_int(
            raw.get("leverage_cap_lev_1e4"),
            f"markets.{alias}.leverage_cap_lev_1e4",
            minimum=1,
        ),
        margin_tiers=tuple(tiers),
        liquidation_penalty_1e8=_as_int(
            margin.get("liquidation_penalty_1e8"),
            f"markets.{alias}.margin.liquidation_penalty_1e8",
            minimum=0,
        ),
        funding=FundingSpec(
            interval_ms=_as_int(
                funding.get("interval_ms"),
                f"markets.{alias}.funding.interval_ms",
                minimum=1,
            ),
            settlement_offsets_ms=tuple(sorted(offsets)),
            cap_1e8=_as_int(
                funding.get("cap_1e8"),
                f"markets.{alias}.funding.cap_1e8",
            ),
            floor_1e8=_as_int(
                funding.get("floor_1e8"),
                f"markets.{alias}.funding.floor_1e8",
            ),
        ),
        half_spread_1e8=_as_int(
            execution.get("half_spread_1e8"),
            f"markets.{alias}.execution.half_spread_1e8",
            minimum=0,
        ),
        impact_model=impact_model,
        impact_coeff_1e8=_as_int(
            execution.get("impact_coeff_1e8"),
            f"markets.{alias}.execution.impact_coeff_1e8",
            minimum=0,
        ),
        participation_cap_1e8=_as_int(
            execution.get("participation_cap_1e8"),
            f"markets.{alias}.execution.participation_cap_1e8",
            minimum=1,
        ),
        cost_profile_multipliers_1e4=MappingProxyType(profiles),
    )


def load_pack(
    root: str | Path,
    *,
    verify_integrity: bool = True,
) -> PackData:
    """Load a pack without ever conflating trade, mark, and index series."""

    pack_root = Path(root)
    manifest_path = pack_root / "manifest.json"
    try:
        manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise PackError(f"cannot read pack manifest: {manifest_path}") from exc
    manifest = _as_object(
        _decode_json(manifest_raw, source=manifest_path),
        "manifest",
    )
    if manifest.get("schema") != "pack_manifest/v1":
        raise PackError("manifest schema must equal pack_manifest/v1")

    decision_bar_ms = _as_int(
        manifest.get("decision_bar_ms"),
        "decision_bar_ms",
        minimum=1,
    )
    market_objects = _as_object(manifest.get("markets"), "markets")
    if not market_objects:
        raise PackError("pack must declare at least one market")
    specs = {
        alias: _market_spec(alias, _as_object(value, f"markets.{alias}"))
        for alias, value in sorted(market_objects.items())
    }

    selected: dict[str, dict[str, tuple[Path, bytes]]] = {
        alias: {} for alias in specs
    }
    for raw_entry in _as_array(manifest.get("files"), "files"):
        entry = _as_object(raw_entry, "files[]")
        alias = _as_str(entry.get("market"), "files[].market")
        if alias not in specs:
            raise PackError(f"series references undeclared market: {alias}")
        role = _as_str(entry.get("role"), "files[].role")
        row_schema = _as_str(entry.get("row_schema"), "files[].row_schema")
        interval_ms = _as_int(
            entry.get("interval_ms"),
            "files[].interval_ms",
            minimum=0,
        )
        if role not in {"trade", "mark", "index", "funding"}:
            raise PackError(f"unsupported series role: {role}")
        expected_row_schema = (
            "funding_row/v1" if role == "funding" else "bar_row/v1"
        )
        if row_schema != expected_row_schema:
            raise PackError(
                f"{role} series must declare {expected_row_schema}"
            )
        if role != "funding" and interval_ms != decision_bar_ms:
            continue
        if role == "funding" and interval_ms != 0:
            raise PackError("funding file interval_ms must be zero")
        if role in selected[alias]:
            raise PackError(
                f"duplicate {role} series for {alias} at decision interval"
            )
        selected[alias][role] = _read_declared_file(
            pack_root,
            entry,
            verify_integrity=verify_integrity,
        )

    markets: dict[str, MarketData] = {}
    required_roles = {"trade", "mark", "index", "funding"}
    for alias, spec in sorted(specs.items()):
        missing = required_roles - selected[alias].keys()
        if missing:
            raise PackError(
                f"{alias} missing decision series: {', '.join(sorted(missing))}"
            )
        role_rows: dict[
            str,
            tuple[BarRow, ...] | tuple[FundingRow, ...],
        ] = {}
        for role in sorted(required_roles):
            path, raw = selected[alias][role]
            role_rows[role] = _load_rows(
                raw=raw,
                source=path,
                market=alias,
                role=role,
            )
        markets[alias] = MarketData(
            spec=spec,
            trade=cast(tuple[BarRow, ...], role_rows["trade"]),
            mark=cast(tuple[BarRow, ...], role_rows["mark"]),
            index=cast(tuple[BarRow, ...], role_rows["index"]),
            funding=cast(tuple[FundingRow, ...], role_rows["funding"]),
        )
        price_rows = markets[alias]
        if not (
            len(price_rows.trade)
            == len(price_rows.mark)
            == len(price_rows.index)
        ):
            raise PackError(f"{alias}: trade/mark/index series lengths differ")
        trade_ts = tuple(row.ts for row in price_rows.trade)
        if trade_ts != tuple(row.ts for row in price_rows.mark):
            raise PackError(f"{alias}: trade/mark timestamps are not aligned")
        if trade_ts != tuple(row.ts for row in price_rows.index):
            raise PackError(f"{alias}: trade/index timestamps are not aligned")
        if not _strictly_increasing(trade_ts):
            raise PackError(f"{alias}: price-series timestamps are not increasing")
        funding_ts = tuple(row.ts for row in price_rows.funding)
        if not _strictly_increasing(funding_ts):
            raise PackError(f"{alias}: funding timestamps are not increasing")

    window = _as_object(manifest.get("window"), "window")
    defaults = _as_object(manifest.get("default_lookback"), "default_lookback")
    window_start_ts = _as_int(
        window.get("start_ts"),
        "window.start_ts",
        minimum=0,
    )
    window_end_ts = _as_int(
        window.get("end_ts"),
        "window.end_ts",
        minimum=0,
    )
    if window_end_ts <= window_start_ts:
        raise PackError("window.end_ts must be after window.start_ts")
    return PackData(
        root=pack_root,
        pack_id=_as_str(manifest.get("pack_id"), "pack_id"),
        content_hash=_as_str(manifest.get("content_hash"), "content_hash"),
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        decision_bar_ms=decision_bar_ms,
        warmup_bars=_as_int(
            manifest.get("warmup_bars"),
            "warmup_bars",
            minimum=0,
        ),
        default_lookback=EffectiveLookback(
            bars=_as_int(
                defaults.get("bars"),
                "default_lookback.bars",
                minimum=1,
            ),
            funding_prints=_as_int(
                defaults.get("funding_prints"),
                "default_lookback.funding_prints",
                minimum=0,
            ),
        ),
        markets=MappingProxyType(markets),
        manifest=MappingProxyType(manifest),
    )
