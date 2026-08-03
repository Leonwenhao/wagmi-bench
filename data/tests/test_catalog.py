# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from data.binance import BinanceBulkFetcher, HTTPResponse
from data.catalog import (
    COVID_BLACK_THURSDAY,
    COVID_WINDOW_END_TS,
    COVID_WINDOW_START_TS,
    FOUR_HOURS_MS,
    HOUR_MS,
    PackCatalogError,
    available_pack_ids,
    fetch_and_build_pack,
    get_pack_definition,
)

_ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly"

_CATALOG: Mapping[str, tuple[str, str, int, tuple[str, ...]]] = {
    "covid-black-thursday": (
        "2020-03-05",
        "2020-03-21",
        HOUR_MS,
        ("2020-03",),
    ),
    "china-mining-ban": (
        "2021-05-12",
        "2021-05-25",
        HOUR_MS,
        ("2021-05",),
    ),
    "luna-collapse": (
        "2022-05-05",
        "2022-05-17",
        HOUR_MS,
        ("2022-05",),
    ),
    "ftx-2022": (
        "2022-11-05",
        "2022-11-16",
        HOUR_MS,
        ("2022-11",),
    ),
    "yen-carry-unwind": (
        "2024-07-29",
        "2024-08-09",
        HOUR_MS,
        ("2024-07", "2024-08"),
    ),
    "10-10-cascade": (
        "2025-10-09",
        "2025-10-14",
        HOUR_MS,
        ("2025-10",),
    ),
    "spot-etf-approval": (
        "2024-01-08",
        "2024-01-26",
        FOUR_HOURS_MS,
        ("2024-01",),
    ),
    "etf-rumor-whipsaw": (
        "2023-10-13",
        "2023-11-01",
        FOUR_HOURS_MS,
        ("2023-10",),
    ),
    "election-run": (
        "2024-11-04",
        "2024-12-07",
        FOUR_HOURS_MS,
        ("2024-11", "2024-12"),
    ),
    "q4-2020-institutional-run": (
        "2020-10-01",
        "2021-01-01",
        FOUR_HOURS_MS,
        ("2020-10", "2020-11", "2020-12"),
    ),
    "jan-2021-squeeze": (
        "2021-01-01",
        "2021-02-22",
        FOUR_HOURS_MS,
        ("2021-01", "2021-02"),
    ),
    "summer-2024-range": (
        "2024-06-01",
        "2024-07-29",
        FOUR_HOURS_MS,
        ("2024-06", "2024-07"),
    ),
    "2023-dead-zone": (
        "2023-06-01",
        "2023-10-01",
        FOUR_HOURS_MS,
        ("2023-06", "2023-07", "2023-08", "2023-09"),
    ),
}


class _MemoryResponse:
    def __init__(self, payload: bytes) -> None:
        self.status = 200
        self.headers: Mapping[str, str] = {
            "content-length": str(len(payload))
        }
        self._payload = payload
        self._offset = 0

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._payload) - self._offset
        chunk = self._payload[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        return None


class _InterceptingTransport:
    def __init__(self, payloads: Mapping[str, bytes]) -> None:
        self._payloads = dict(payloads)
        self.calls: list[str] = []

    def open(
        self, url: str, headers: Mapping[str, str], timeout_seconds: int
    ) -> HTTPResponse:
        del headers, timeout_seconds
        self.calls.append(url)
        return _MemoryResponse(self._payloads[url])


def test_covid_definition_pins_window_and_exact_bulk_archives() -> None:
    definition = get_pack_definition("covid-black-thursday")

    assert definition is COVID_BLACK_THURSDAY
    assert definition.config.window_start_ts == COVID_WINDOW_START_TS
    assert definition.config.window_end_ts == COVID_WINDOW_END_TS
    assert definition.config.decision_bar_ms == HOUR_MS
    assert definition.config.warmup_bars == 64
    assert definition.config.market_descriptor["tick_size_micro"] == 10_000
    assert [(item.role, item.interval_ms, item.url) for item in definition.archives] == [
        (
            "trade",
            HOUR_MS,
            "https://data.binance.vision/data/futures/um/monthly/"
            "klines/BTCUSDT/1h/BTCUSDT-1h-2020-03.zip",
        ),
        (
            "mark",
            HOUR_MS,
            "https://data.binance.vision/data/futures/um/monthly/"
            "markPriceKlines/BTCUSDT/1h/BTCUSDT-1h-2020-03.zip",
        ),
        (
            "index",
            HOUR_MS,
            "https://data.binance.vision/data/futures/um/monthly/"
            "indexPriceKlines/BTCUSDT/1h/BTCUSDT-1h-2020-03.zip",
        ),
        (
            "funding",
            0,
            "https://data.binance.vision/data/futures/um/monthly/"
            "fundingRate/BTCUSDT/BTCUSDT-fundingRate-2020-03.zip",
        ),
    ]


def test_catalog_pins_all_scope_windows_and_interval_classes() -> None:
    assert available_pack_ids() == tuple(sorted(_CATALOG))

    for pack_id, (start, end_exclusive, interval_ms, _) in _CATALOG.items():
        definition = get_pack_definition(pack_id)
        assert definition.config.window_start_ts == _utc_ms(start)
        assert definition.config.window_end_ts == _utc_ms(end_exclusive)
        assert definition.config.decision_bar_ms == interval_ms
        assert definition.config.warmup_bars == 64
        assert definition.config.default_lookback_bars == 64
        assert definition.config.default_funding_prints == 6

    crash_ids = {
        "covid-black-thursday",
        "china-mining-ban",
        "luna-collapse",
        "ftx-2022",
        "yen-carry-unwind",
        "10-10-cascade",
    }
    assert {
        pack_id
        for pack_id in available_pack_ids()
        if get_pack_definition(pack_id).config.decision_bar_ms == HOUR_MS
    } == crash_ids

    summer = get_pack_definition("summer-2024-range")
    yen = get_pack_definition("yen-carry-unwind")
    assert summer.config.window_end_ts == yen.config.window_start_ts


def test_every_pack_pins_exact_monthly_bulk_archives() -> None:
    for pack_id, (_, _, interval_ms, months) in _CATALOG.items():
        definition = get_pack_definition(pack_id)
        assert [
            (
                archive.role,
                archive.interval_ms,
                archive.url,
                archive.timestamp_unit,
            )
            for archive in definition.archives
        ] == _expected_archives(interval_ms, months)
        assert all(
            archive.url.startswith(f"{_ARCHIVE_ROOT}/")
            for archive in definition.archives
        )
        assert all(
            "fapi.binance.com" not in archive.url
            and "api.binance.com" not in archive.url
            for archive in definition.archives
        )


def test_era_table_reuses_only_the_documented_conservative_baseline() -> None:
    descriptor_ids: set[int] = set()
    for pack_id in available_pack_ids():
        descriptor = get_pack_definition(pack_id).config.market_descriptor
        descriptor_ids.add(id(descriptor))
        assert descriptor["tick_size_micro"] == 10_000
        assert descriptor["qty_step_base_1e8"] == 100_000
        assert descriptor["min_notional_micro"] == 10_000_000
        assert descriptor["leverage_cap_lev_1e4"] == 30_000
        assert descriptor["funding"] == {
            "interval_ms": 28_800_000,
            "settlement_offsets_ms": [0, 28_800_000, 57_600_000],
            "cap_1e8": 300_000,
            "floor_1e8": -300_000,
        }
        assert descriptor["fees"] == {
            "maker_rate_1e8": 20_000,
            "taker_rate_1e8": 40_000,
        }
        note = cast(str, descriptor["calibration_note"])
        assert "Conservative V1 Binance BTCUSDT descriptor" in note
        assert "pending primary-source verification" in note

    assert len(descriptor_ids) == 7


def test_unknown_pack_fails_before_transport() -> None:
    with pytest.raises(PackCatalogError, match="unknown pack"):
        get_pack_definition("not-a-pack")


def test_fetch_orchestration_uses_only_bulk_archives_and_sibling_checksums(
    tmp_path: Path,
) -> None:
    payloads = _archive_payloads()
    transport = _InterceptingTransport(payloads)
    fetcher = BinanceBulkFetcher(transport=transport, retries=0)

    built = fetch_and_build_pack(
        "covid-black-thursday",
        raw_root=tmp_path / "raw",
        packs_root=tmp_path / "packs",
        fetcher=fetcher,
    )

    expected_archive_urls = [
        archive.url for archive in COVID_BLACK_THURSDAY.archives
    ]
    expected_calls = [
        item
        for url in expected_archive_urls
        for item in (url, f"{url}.CHECKSUM")
    ]
    assert transport.calls == expected_calls
    assert all(
        url.startswith("https://data.binance.vision/data/") for url in transport.calls
    )
    assert all("fapi.binance.com" not in url for url in transport.calls)
    assert all("api.binance.com" not in url for url in transport.calls)

    raw_pack = tmp_path / "raw" / "covid-black-thursday"
    common_name = "BTCUSDT-1h-2020-03.zip"
    assert (raw_pack / "trade" / common_name).is_file()
    assert (raw_pack / "mark" / common_name).is_file()
    assert (raw_pack / "index" / common_name).is_file()
    assert (
        raw_pack
        / "funding"
        / "BTCUSDT-fundingRate-2020-03.zip.CHECKSUM"
    ).is_file()

    manifest = cast(
        dict[str, object],
        json.loads(built.manifest_path.read_text(encoding="utf-8")),
    )
    assert manifest["pack_id"] == "covid-black-thursday"
    files = cast(list[dict[str, object]], manifest["files"])
    assert {cast(str, item["role"]) for item in files} == {
        "trade",
        "mark",
        "index",
        "funding",
    }
    upstream_urls = {
        cast(str, source["url"])
        for item in files
        for source in cast(list[dict[str, object]], item["upstream"])
    }
    assert upstream_urls == set(expected_archive_urls)


def _archive_payloads() -> dict[str, bytes]:
    bar_csv = (
        "open_time,open,high,low,close,volume\n"
        f"{COVID_WINDOW_START_TS},9000.0,9100.0,8900.0,9050.0,1.0\n"
    )
    funding_csv = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        f"{COVID_WINDOW_START_TS},8,-0.00300000\n"
    )
    payloads: dict[str, bytes] = {}
    for archive in COVID_BLACK_THURSDAY.archives:
        csv_text = funding_csv if archive.role == "funding" else bar_csv
        archive_name = Path(archive.url).name
        zipped = _zip_bytes(archive_name.removesuffix(".zip") + ".csv", csv_text)
        digest = hashlib.sha256(zipped).hexdigest()
        payloads[archive.url] = zipped
        payloads[f"{archive.url}.CHECKSUM"] = (
            f"{digest}  {archive_name}\n".encode("utf-8")
        )
    return payloads


def _zip_bytes(member_name: str, csv_text: str) -> bytes:
    output = io.BytesIO()
    info = zipfile.ZipInfo(member_name)
    info.date_time = (1980, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(output, mode="w") as zipped:
        zipped.writestr(info, csv_text.encode("utf-8"))
    return output.getvalue()


def _utc_ms(value: str) -> int:
    instant = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(instant.timestamp() * 1_000)


def _expected_archives(
    interval_ms: int,
    months: tuple[str, ...],
) -> list[tuple[str, int, str, str]]:
    interval_label = {HOUR_MS: "1h", FOUR_HOURS_MS: "4h"}[interval_ms]
    result: list[tuple[str, int, str, str]] = []
    for role, directory in (
        ("trade", "klines"),
        ("mark", "markPriceKlines"),
        ("index", "indexPriceKlines"),
    ):
        for month in months:
            result.append(
                (
                    role,
                    interval_ms,
                    f"{_ARCHIVE_ROOT}/{directory}/BTCUSDT/"
                    f"{interval_label}/BTCUSDT-{interval_label}-{month}.zip",
                    "ms",
                )
            )
    for month in months:
        result.append(
            (
                "funding",
                0,
                f"{_ARCHIVE_ROOT}/fundingRate/BTCUSDT/"
                f"BTCUSDT-fundingRate-{month}.zip",
                "ms",
            )
        )
    return result
