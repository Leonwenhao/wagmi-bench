# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from data.binance import (
    ArchiveSource,
    BinanceBulkFetcher,
    BulkURLRejected,
    ChecksumError,
    HTTPResponse,
    VerifiedArchive,
    read_verified_csv_rows,
    validate_bulk_url,
    verify_archive,
)


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers: Mapping[str, str] = dict(headers or {})
        self._body = body
        self._offset = 0
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, results: list[HTTPResponse | BaseException]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, str], int]] = []

    def open(
        self, url: str, headers: Mapping[str, str], timeout_seconds: int
    ) -> HTTPResponse:
        self.calls.append((url, dict(headers), timeout_seconds))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.mark.parametrize(
    "url",
    [
        "http://data.binance.vision/data/file.zip",
        "https://fapi.binance.com/data/file.zip",
        "https://data.binance.vision.evil.example/data/file.zip",
        "https://user@data.binance.vision/data/file.zip",
        "https://data.binance.vision:444/data/file.zip",
        "https://data.binance.vision:bad/data/file.zip",
        "https://data.binance.vision/futures/file.zip",
        "https://data.binance.vision/data/file.zip?token=x",
    ],
)
def test_bulk_url_rejects_every_non_bulk_origin(url: str) -> None:
    with pytest.raises(BulkURLRejected):
        validate_bulk_url(url)


def test_download_resumes_from_partial_file(tmp_path: Path) -> None:
    response = FakeResponse(
        206,
        b"def",
        {
            "content-length": "3",
            "content-range": "bytes 3-5/6",
        },
    )
    transport = FakeTransport([response])
    fetcher = BinanceBulkFetcher(
        transport=transport,
        retries=0,
        chunk_size=2,
    )
    destination = tmp_path / "archive.zip"
    destination.with_name("archive.zip.part").write_bytes(b"abc")

    result = fetcher.download(
        "https://data.binance.vision/data/archive.zip",
        destination,
    )

    assert result.read_bytes() == b"abcdef"
    assert transport.calls[0][1]["Range"] == "bytes=3-"
    assert response.closed


def test_download_retries_without_wall_clock_dependency(tmp_path: Path) -> None:
    response = FakeResponse(200, b"complete", {"content-length": "8"})
    transport = FakeTransport([OSError("temporary network failure"), response])
    delays: list[int] = []
    fetcher = BinanceBulkFetcher(
        transport=transport,
        retries=1,
        backoff_milliseconds=10,
        delay=delays.append,
    )

    destination = fetcher.download(
        "https://data.binance.vision/data/retry.zip",
        tmp_path / "retry.zip",
    )

    assert destination.read_bytes() == b"complete"
    assert delays == [10]
    assert len(transport.calls) == 2


def test_fetch_archive_always_fetches_sibling_checksum(tmp_path: Path) -> None:
    archive = FakeResponse(200, b"zip", {"content-length": "3"})
    checksum = FakeResponse(200, b"sum", {"content-length": "3"})
    transport = FakeTransport([archive, checksum])
    fetcher = BinanceBulkFetcher(transport=transport, retries=0)
    url = "https://data.binance.vision/data/futures/example.zip"

    source = fetcher.fetch_archive(url, tmp_path)

    assert source.archive_path == tmp_path / "example.zip"
    assert source.checksum_path == tmp_path / "example.zip.CHECKSUM"
    assert [call[0] for call in transport.calls] == [url, f"{url}.CHECKSUM"]


def test_checksum_is_mandatory_and_filename_bound(tmp_path: Path) -> None:
    source = _archive_source(tmp_path, "bars.zip", "1,2,3\n")
    source.checksum_path.write_text(
        f"{'0' * 64}  {source.archive_path.name}\n",
        encoding="utf-8",
    )

    with pytest.raises(ChecksumError, match="SHA-256 mismatch"):
        verify_archive(source)


def test_verified_archive_cannot_be_constructed_without_verification(
    tmp_path: Path,
) -> None:
    source = _archive_source(tmp_path, "bars.zip", "1,2,3\n")
    digest = "sha256:" + hashlib.sha256(
        source.archive_path.read_bytes()
    ).hexdigest()

    with pytest.raises(TypeError, match="must come from verify_archive"):
        VerifiedArchive(source=source, sha256=digest, _proof=object())


def test_verified_archive_is_rechecked_before_csv_read(tmp_path: Path) -> None:
    source = _archive_source(tmp_path, "bars.zip", "1,2,3\n")
    verified = verify_archive(source)
    with source.archive_path.open("ab") as stream:
        stream.write(b"tamper")

    with pytest.raises(ChecksumError, match="changed after verification"):
        read_verified_csv_rows(verified)


def test_verified_csv_read_returns_exact_rows(tmp_path: Path) -> None:
    source = _archive_source(
        tmp_path,
        "bars.zip",
        "open_time,open\n1000,10.0\n",
    )

    rows = read_verified_csv_rows(verify_archive(source))

    assert rows == [["open_time", "open"], ["1000", "10.0"]]


def _archive_source(
    directory: Path,
    archive_name: str,
    csv_text: str,
) -> ArchiveSource:
    archive_path = directory / archive_name
    member_name = archive_name.removesuffix(".zip") + ".csv"
    info = zipfile.ZipInfo(member_name)
    info.date_time = (1980, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(archive_path, mode="w") as zipped:
        zipped.writestr(info, csv_text.encode("utf-8"))
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = directory / f"{archive_name}.CHECKSUM"
    checksum_path.write_text(
        f"{digest}  {archive_name}\n",
        encoding="utf-8",
    )
    return ArchiveSource(
        url=f"https://data.binance.vision/data/{archive_name}",
        archive_path=archive_path,
        checksum_path=checksum_path,
    )
