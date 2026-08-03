# SPDX-License-Identifier: Apache-2.0
"""WAGMI Bench deterministic market-data pipeline (C1)."""

from data.binance import (
    ArchiveError,
    ArchiveSource,
    BinanceBulkFetcher,
    BinanceDataError,
    BulkURLRejected,
    ChecksumError,
    DownloadError,
    VerifiedArchive,
    read_verified_csv_rows,
    validate_bulk_url,
    verify_archive,
)
from data.builder import (
    BuiltPack,
    PackBuildConfig,
    PackBuildError,
    RawSeriesArchive,
    build_pack,
)
from data.catalog import (
    COVID_BLACK_THURSDAY,
    ArchiveDefinition,
    PackCatalogError,
    PackDefinition,
    available_pack_ids,
    fetch_and_build_pack,
    get_pack_definition,
)

__all__ = [
    "ArchiveError",
    "ArchiveDefinition",
    "ArchiveSource",
    "BinanceBulkFetcher",
    "BinanceDataError",
    "BulkURLRejected",
    "BuiltPack",
    "COVID_BLACK_THURSDAY",
    "ChecksumError",
    "DownloadError",
    "PackBuildConfig",
    "PackBuildError",
    "PackCatalogError",
    "PackDefinition",
    "RawSeriesArchive",
    "VerifiedArchive",
    "available_pack_ids",
    "build_pack",
    "fetch_and_build_pack",
    "get_pack_definition",
    "read_verified_csv_rows",
    "validate_bulk_url",
    "verify_archive",
]
