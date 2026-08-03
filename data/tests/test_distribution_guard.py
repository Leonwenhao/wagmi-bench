# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from data.distribution_guard import (
    REPORT_SCHEMA,
    DistributionGuardError,
    scan_distribution,
    scan_distribution_artifact,
    scan_entry,
    scan_tracked_repository,
)


def test_current_git_tracked_tree_contains_no_market_data() -> None:
    tracked_count, findings = scan_tracked_repository(Path.cwd())

    assert tracked_count > 0
    assert findings == ()


def test_pack_series_and_unapproved_jsonl_are_refused() -> None:
    pack_findings = scan_entry(
        origin="tracked",
        path="packs/example/bars_1h.jsonl",
        prefix=b'{"open_ts":1}\n',
    )
    fixture_findings = scan_entry(
        origin="tracked",
        path="fixtures/new-real-series/bars_1h.jsonl",
        prefix=b'{"open_ts":1}\n',
    )

    assert {finding.reason for finding in pack_findings} == {
        "packs may contain only <pack-id>/manifest.json",
        "tracked/distributed JSONL is not an approved synthetic fixture",
    }
    assert {finding.reason for finding in fixture_findings} == {
        "tracked/distributed JSONL is not an approved synthetic fixture",
    }


def test_exact_synthetic_fixture_and_pack_manifest_are_allowed() -> None:
    fixture = scan_entry(
        origin="tracked",
        path="fixtures/golden-mini/pack/bars_1h.jsonl",
        prefix=b'{"open_ts":1}\n',
    )
    manifest = scan_entry(
        origin="tracked",
        path="packs/covid-black-thursday/manifest.json",
        prefix=b'{"schema_version":"1.0.0"}\n',
    )

    assert fixture == ()
    assert manifest == ()


def test_archive_magic_and_disguised_numeric_csv_are_refused() -> None:
    renamed_zip = scan_entry(
        origin="tracked",
        path="notes/payload.txt",
        prefix=b"PK\x03\x04not-really-text",
    )
    numeric_csv = scan_entry(
        origin="tracked",
        path="notes/rows.txt",
        prefix=(
            b"1583366400000,9000.0,9100.0,8900.0,9050.0,1.0\n"
            b"1583370000000,9050.0,9150.0,9000.0,9125.0,2.0\n"
        ),
    )

    assert [finding.reason for finding in renamed_zip] == [
        "embedded ZIP archive content is forbidden"
    ]
    assert [finding.reason for finding in numeric_csv] == [
        "content resembles raw Binance market-data rows"
    ]


def test_wheel_and_sdist_members_are_scanned_in_stable_order(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "tradeevolve-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("data/catalog.py", b"# safe source\n")
        archive.writestr(
            "packs/example/mark_1h.jsonl",
            b'{"open_ts":1}\n',
        )

    sdist = tmp_path / "tradeevolve-0.1.0.tar.gz"
    payload = b"open_time,open,high,low,close,volume\n"
    with tarfile.open(sdist, mode="w:gz") as archive:
        member = tarfile.TarInfo(
            "tradeevolve-0.1.0/data/raw/BTCUSDT-1h-2020-03.csv"
        )
        member.size = len(payload)
        member.mtime = 0
        archive.addfile(member, io.BytesIO(payload))

    wheel_findings = scan_distribution_artifact(wheel)
    sdist_findings = scan_distribution_artifact(sdist)

    assert [finding.path for finding in wheel_findings] == [
        "packs/example/mark_1h.jsonl",
        "packs/example/mark_1h.jsonl",
    ]
    assert {finding.reason for finding in sdist_findings} == {
        "content resembles raw Binance market-data rows",
        "data/raw content is local-only",
        "raw/archive market-data file extension is forbidden",
    }


def test_report_json_is_canonical_and_artifacts_are_name_sorted(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.whl"
    second = tmp_path / "b.whl"
    for artifact in (second, first):
        with zipfile.ZipFile(artifact, mode="w") as archive:
            archive.writestr("data/catalog.py", b"# safe\n")

    report = scan_distribution(
        repo_root=Path.cwd(),
        artifacts=(second, first),
        scan_tracked=False,
    )
    encoded = report.to_json()

    assert report.ok
    assert report.artifacts == ("a.whl", "b.whl")
    assert encoded == json.dumps(
        json.loads(encoded),
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert json.loads(encoded)["schema"] == REPORT_SCHEMA


def test_distribution_artifact_symlink_is_rejected(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.whl"
    with zipfile.ZipFile(target, mode="w") as archive:
        archive.writestr("data/catalog.py", b"# safe\n")
    link = tmp_path / "linked.whl"
    link.symlink_to(target)

    with pytest.raises(DistributionGuardError, match="symlink"):
        scan_distribution_artifact(link)
