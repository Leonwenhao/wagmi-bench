# SPDX-License-Identifier: Apache-2.0
"""Offline-safe Binance bulk archive acquisition and verification.

The only network origin this module can contact is the HTTPS bulk repository
``data.binance.vision``.  It deliberately contains no Binance REST client.
Archives and their sibling ``.CHECKSUM`` files are fetched separately; callers
must obtain a :class:`VerifiedArchive` before ZIP contents can be read.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import io
import re
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

BINANCE_BULK_HOST = "data.binance.vision"
_CHECKSUM_RE = re.compile(r"^(?P<digest>[0-9A-Fa-f]{64})[ \t]+[*]?(?P<name>.+?)$")
_VERIFICATION_PROOF = object()


class BinanceDataError(Exception):
    """Base class for deterministic data-pipeline failures."""


class BulkURLRejected(BinanceDataError):
    """Raised when a URL is outside the allowed Binance bulk origin."""


class DownloadError(BinanceDataError):
    """Raised when a bulk download cannot be completed."""


class ChecksumError(BinanceDataError):
    """Raised when mandatory upstream checksum verification fails."""


class ArchiveError(BinanceDataError):
    """Raised when a verified archive cannot be read safely."""


@dataclass(frozen=True, slots=True)
class ArchiveSource:
    """Local archive bytes plus their exact upstream provenance."""

    url: str
    archive_path: Path
    checksum_path: Path

    def __post_init__(self) -> None:
        parsed = validate_bulk_url(self.url)
        if not parsed.path.endswith(".zip"):
            raise BulkURLRejected(f"archive URL must end in .zip: {self.url!r}")
        if Path(parsed.path).name != self.archive_path.name:
            raise BinanceDataError(
                "archive filename must match the upstream URL basename: "
                f"{self.archive_path.name!r} != {Path(parsed.path).name!r}"
            )
        expected_checksum = self.archive_path.with_name(
            f"{self.archive_path.name}.CHECKSUM"
        )
        if self.checksum_path != expected_checksum:
            raise BinanceDataError(
                "checksum must be the archive's sibling <archive>.CHECKSUM: "
                f"expected {expected_checksum}, got {self.checksum_path}"
            )


@dataclass(frozen=True, slots=True)
class VerifiedArchive:
    """An archive whose Binance-published sibling checksum has matched."""

    source: ArchiveSource
    sha256: str
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _VERIFICATION_PROOF:
            raise TypeError("VerifiedArchive instances must come from verify_archive()")


class HTTPResponse(Protocol):
    """The response surface needed by :class:`BinanceBulkFetcher`."""

    status: int
    headers: Mapping[str, str]

    def read(self, amount: int = -1) -> bytes:
        """Read response bytes."""

    def close(self) -> None:
        """Release response resources."""


class HTTPTransport(Protocol):
    """Injectable HTTPS transport used to keep tests fully offline."""

    def open(
        self, url: str, headers: Mapping[str, str], timeout_seconds: int
    ) -> HTTPResponse:
        """Open one allowed bulk URL."""


class _HTTPSResponse:
    """Adapter that closes both the response and its HTTPS connection."""

    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPSConnection,
    ) -> None:
        self.status = response.status
        self.headers: Mapping[str, str] = {
            key.lower(): value for key, value in response.getheaders()
        }
        self._response = response
        self._connection = connection

    def read(self, amount: int = -1) -> bytes:
        return self._response.read(amount)

    def close(self) -> None:
        self._response.close()
        self._connection.close()


class BinanceBulkHTTPSTransport:
    """Direct HTTPS transport with no redirect or alternate-host code path."""

    def open(
        self, url: str, headers: Mapping[str, str], timeout_seconds: int
    ) -> HTTPResponse:
        parsed = validate_bulk_url(url)
        connection = http.client.HTTPSConnection(
            BINANCE_BULK_HOST,
            port=443,
            timeout=timeout_seconds,
        )
        try:
            connection.request("GET", parsed.path, headers=dict(headers))
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        return _HTTPSResponse(response, connection)


class _RetryableDownloadError(DownloadError):
    """Internal marker for failures safe to retry using the partial file."""


class BinanceBulkFetcher:
    """Resumable, retrying downloader for Binance's checksummed bulk files."""

    def __init__(
        self,
        *,
        transport: HTTPTransport | None = None,
        retries: int = 3,
        timeout_seconds: int = 30,
        chunk_size: int = 1024 * 1024,
        backoff_milliseconds: int = 0,
        delay: Callable[[int], None] | None = None,
    ) -> None:
        if retries < 0:
            raise ValueError("retries must be non-negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if backoff_milliseconds < 0:
            raise ValueError("backoff_milliseconds must be non-negative")
        self._transport = transport or BinanceBulkHTTPSTransport()
        self._retries = retries
        self._timeout_seconds = timeout_seconds
        self._chunk_size = chunk_size
        self._backoff_milliseconds = backoff_milliseconds
        self._delay = delay or _no_delay

    def fetch_archive(self, url: str, destination_dir: Path) -> ArchiveSource:
        """Fetch one ``.zip`` and its mandatory sibling ``.CHECKSUM`` file."""

        parsed = validate_bulk_url(url)
        if not parsed.path.endswith(".zip"):
            raise BulkURLRejected(f"archive URL must end in .zip: {url!r}")
        destination_dir.mkdir(parents=True, exist_ok=True)
        archive_path = destination_dir / Path(parsed.path).name
        checksum_path = destination_dir / f"{archive_path.name}.CHECKSUM"
        self.download(url, archive_path)
        self.download(f"{url}.CHECKSUM", checksum_path)
        return ArchiveSource(
            url=url,
            archive_path=archive_path,
            checksum_path=checksum_path,
        )

    def download(self, url: str, destination: Path) -> Path:
        """Download an allowed URL atomically, resuming from ``.part`` bytes."""

        validate_bulk_url(url)
        if destination.is_file():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        part_path = destination.with_name(f"{destination.name}.part")
        last_error: BaseException | None = None

        for attempt in range(self._retries + 1):
            try:
                self._download_once(url, part_path)
                part_path.replace(destination)
                return destination
            except (
                _RetryableDownloadError,
                OSError,
                TimeoutError,
                http.client.HTTPException,
            ) as exc:
                last_error = exc
                if attempt == self._retries:
                    break
                self._delay(self._backoff_milliseconds * (2**attempt))

        raise DownloadError(
            f"download failed after {self._retries + 1} attempt(s): {url}"
        ) from last_error

    def _download_once(self, url: str, part_path: Path) -> None:
        offset = part_path.stat().st_size if part_path.is_file() else 0
        headers = {
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "tradeevolve/0.1 bulk-fetcher",
        }
        if offset:
            headers["Range"] = f"bytes={offset}-"

        response = self._transport.open(url, headers, self._timeout_seconds)
        try:
            status = response.status
            if status in {408, 425, 429} or 500 <= status <= 599:
                raise _RetryableDownloadError(f"retryable HTTP status {status}")
            if status not in {200, 206}:
                raise DownloadError(f"unexpected HTTP status {status} for {url}")

            mode = "wb"
            if offset and status == 206:
                content_range = response.headers.get("content-range", "")
                if not content_range.startswith(f"bytes {offset}-"):
                    raise _RetryableDownloadError(
                        "resume response has an invalid Content-Range: "
                        f"{content_range!r}"
                    )
                mode = "ab"

            expected_length = _optional_nonnegative_int(
                response.headers.get("content-length"), "Content-Length"
            )
            received = 0
            with part_path.open(mode) as output:
                while True:
                    chunk = response.read(self._chunk_size)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
            if expected_length is not None and received != expected_length:
                raise _RetryableDownloadError(
                    f"incomplete response: expected {expected_length} bytes, "
                    f"received {received}"
                )
        finally:
            response.close()


def validate_bulk_url(url: str) -> SplitResult:
    """Accept only credential-free HTTPS URLs on ``data.binance.vision``."""

    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise BulkURLRejected(f"bulk URL must use HTTPS: {url!r}")
    if parsed.hostname != BINANCE_BULK_HOST:
        raise BulkURLRejected(
            f"bulk URL host must be {BINANCE_BULK_HOST!r}: {url!r}"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise BulkURLRejected(f"bulk URL has an invalid port: {url!r}") from exc
    if port not in {None, 443}:
        raise BulkURLRejected(f"bulk URL may only use HTTPS port 443: {url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise BulkURLRejected(f"bulk URL must not contain credentials: {url!r}")
    if parsed.query or parsed.fragment:
        raise BulkURLRejected(f"bulk URL must not contain query/fragment: {url!r}")
    if not parsed.path.startswith("/data/"):
        raise BulkURLRejected(f"bulk URL path must start with /data/: {url!r}")
    if parsed.path.endswith("/"):
        raise BulkURLRejected(f"bulk URL must name a file: {url!r}")
    return parsed


def verify_archive(source: ArchiveSource) -> VerifiedArchive:
    """Verify the exact sibling checksum before granting archive-read access."""

    if not source.archive_path.is_file():
        raise ChecksumError(f"archive does not exist: {source.archive_path}")
    if not source.checksum_path.is_file():
        raise ChecksumError(
            f"mandatory checksum does not exist: {source.checksum_path}"
        )
    expected = _checksum_for_filename(
        source.checksum_path.read_text(encoding="utf-8"),
        source.archive_path.name,
    )
    actual = _sha256_hex(source.archive_path)
    if actual != expected:
        raise ChecksumError(
            f"SHA-256 mismatch for {source.archive_path.name}: "
            f"expected {expected}, got {actual}"
        )
    return VerifiedArchive(
        source=source,
        sha256=f"sha256:{actual}",
        _proof=_VERIFICATION_PROOF,
    )


def read_verified_csv_rows(
    archive: VerifiedArchive, *, member_name: str | None = None
) -> list[list[str]]:
    """Read one CSV member, rejecting any archive changed after verification."""

    current = f"sha256:{_sha256_hex(archive.source.archive_path)}"
    if current != archive.sha256:
        raise ChecksumError(
            f"archive changed after verification: {archive.source.archive_path}"
        )
    try:
        with zipfile.ZipFile(archive.source.archive_path, mode="r") as zipped:
            member = _select_csv_member(zipped, member_name)
            if member.flag_bits & 0x1:
                raise ArchiveError(f"encrypted ZIP member is unsupported: {member.filename}")
            with zipped.open(member, mode="r") as raw:
                with io.TextIOWrapper(
                    raw, encoding="utf-8-sig", newline=""
                ) as text_stream:
                    rows = [list(row) for row in csv.reader(text_stream)]
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ArchiveError(
            f"cannot read verified archive {archive.source.archive_path}: {exc}"
        ) from exc
    if not rows:
        raise ArchiveError(f"CSV member is empty: {member.filename}")
    return rows


def _select_csv_member(
    zipped: zipfile.ZipFile, requested: str | None
) -> zipfile.ZipInfo:
    files = [entry for entry in zipped.infolist() if not entry.is_dir()]
    if requested is not None:
        matches = [entry for entry in files if entry.filename == requested]
        if len(matches) != 1:
            raise ArchiveError(
                f"requested CSV member {requested!r} was not found exactly once"
            )
        selected = matches[0]
        if not selected.filename.lower().endswith(".csv"):
            raise ArchiveError(f"requested member is not CSV: {selected.filename!r}")
        return selected
    csv_files = [
        entry for entry in files if entry.filename.lower().endswith(".csv")
    ]
    if len(csv_files) != 1:
        raise ArchiveError(
            f"archive must contain exactly one CSV member, found {len(csv_files)}"
        )
    return csv_files[0]


def _checksum_for_filename(checksum_text: str, filename: str) -> str:
    matches: list[str] = []
    for raw_line in checksum_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _CHECKSUM_RE.fullmatch(line)
        if match is None:
            raise ChecksumError(f"malformed .CHECKSUM line: {raw_line!r}")
        listed_name = match.group("name")
        if Path(listed_name).name == filename:
            matches.append(match.group("digest").lower())
    if len(matches) != 1:
        raise ChecksumError(
            f".CHECKSUM must name {filename!r} exactly once; found {len(matches)}"
        )
    return matches[0]


def _sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_nonnegative_int(value: str | None, label: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise _RetryableDownloadError(f"invalid {label}: {value!r}") from exc
    if parsed < 0:
        raise _RetryableDownloadError(f"invalid {label}: {value!r}")
    return parsed


def _no_delay(_: int) -> None:
    """Default retry delay used by deterministic/offline callers."""
