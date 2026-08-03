# SPDX-License-Identifier: Apache-2.0
"""Tests for the post-run credential artifact scanner."""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote_from_bytes

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import artifact_secret_scan as scan


def _env_file(tmp_path: Path, secret: bytes) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        f"FIREWORKS_API_KEY={secret.decode('ascii')}\n",
        encoding="utf-8",
    )
    return path


def test_pass_receipt_is_secret_free_and_hash_bound(tmp_path: Path) -> None:
    secret = b"secret-value-with-special_chars_123"
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "receipt.json").write_text('{"status":"COMPLETE"}\n')
    nested = root / "bundle"
    nested.mkdir()
    (nested / "events.jsonl").write_bytes(b'{"safe":true}\n')
    receipt = tmp_path / "scan.json"

    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(receipt),
                "--env-file",
                str(_env_file(tmp_path, secret)),
            ]
        )
        == 0
    )

    raw = receipt.read_bytes()
    document = json.loads(raw)
    assert raw.endswith(b"\n")
    assert secret not in raw
    assert document["schema"] == "artifact_secret_scan/v1"
    assert document["status"] == "PASS"
    assert document["files_scanned"] == 2
    assert document["match_count"] == 0
    assert document["tree_sha256"].startswith("sha256:")
    assert receipt.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param(lambda value: value, id="raw"),
        pytest.param(lambda value: value.hex().encode("ascii"), id="hex"),
        pytest.param(
            lambda value: value.hex().upper().encode("ascii"),
            id="hex-upper",
        ),
        pytest.param(lambda value: base64.b64encode(value), id="base64"),
        pytest.param(
            lambda value: base64.b64encode(value).rstrip(b"="),
            id="base64-unpadded",
        ),
        pytest.param(
            lambda value: base64.urlsafe_b64encode(value),
            id="base64url",
        ),
        pytest.param(
            lambda value: base64.urlsafe_b64encode(value).rstrip(b"="),
            id="base64url-unpadded",
        ),
        pytest.param(
            lambda value: quote_from_bytes(value).encode("ascii"),
            id="url-encoded",
        ),
    ],
)
def test_every_ingress_encoding_fails_closed(
    tmp_path: Path,
    transform: Callable[[bytes], bytes],
) -> None:
    secret = b"secret/value+with?reserved=bytes"
    root = tmp_path / "artifacts"
    root.mkdir()
    encoded = transform(secret)
    (root / "response.txt").write_bytes(b"prefix " + encoded + b" suffix")
    receipt = tmp_path / "scan.json"

    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(receipt),
                "--env-file",
                str(_env_file(tmp_path, secret)),
            ]
        )
        == 1
    )
    assert not receipt.exists()


def _json_escape_chars(
    value: str,
    indices: frozenset[int],
    *,
    upper: bool = False,
) -> str:
    """Escape only the selected characters as JSON ``\\uXXXX`` sequences."""

    template = "\\u{:04X}" if upper else "\\u{:04x}"
    return "".join(
        template.format(ord(char)) if index in indices else char
        for index, char in enumerate(value)
    )


def _mixed_escape(value: str) -> str:
    """Interleave raw, lowercase-hex and uppercase-hex escaped characters."""

    parts: list[str] = []
    for index, char in enumerate(value):
        if index % 3 == 0:
            parts.append(f"\\u{ord(char):04x}")
        elif index % 3 == 1:
            parts.append(f"\\u{ord(char):04X}")
        else:
            parts.append(char)
    return "".join(parts)


@pytest.mark.parametrize(
    "escape",
    [
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset({0})),
            id="single-first-char",
        ),
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset({len(value) // 2})),
            id="single-middle-char",
        ),
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset({len(value) - 1})),
            id="single-last-char",
        ),
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset({2}), upper=True),
            id="single-char-uppercase-hex",
        ),
        pytest.param(_mixed_escape, id="mixed-lower-upper-and-raw"),
        pytest.param(
            lambda value: _json_escape_chars(value, frozenset(range(len(value)))),
            id="fully-escaped",
        ),
    ],
)
def test_json_unicode_escaped_secret_is_found_by_scanner(
    tmp_path: Path,
    escape: Callable[[str], str],
) -> None:
    secret_text = "TE_SYNTHETIC_SCAN_PROBE_f0e1d2c3"
    secret = secret_text.encode("ascii")
    root = tmp_path / "artifacts"
    root.mkdir()
    comment = escape(secret_text)
    assert comment != secret_text
    payload = ('{"comment":"' + comment + '"}\n').encode("ascii")
    # The auditor's bypass: no protected encoding appears as a literal
    # substring, yet a JSON reader recovers the credential.
    assert secret not in payload
    assert json.loads(payload)["comment"] == secret_text
    (root / "response.json").write_bytes(payload)
    receipt = tmp_path / "scan.json"

    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(receipt),
                "--env-file",
                str(_env_file(tmp_path, secret)),
            ]
        )
        == 1
    )
    assert not receipt.exists()


def test_double_escaped_nested_json_secret_is_found_by_scanner(
    tmp_path: Path,
) -> None:
    secret_text = "TE_SYNTHETIC_SCAN_PROBE_f0e1d2c3"
    secret = secret_text.encode("ascii")
    root = tmp_path / "artifacts"
    root.mkdir()
    nested = _json_escape_chars(secret_text, frozenset({0, 5})).replace(
        "\\", "\\\\"
    )
    payload = ('{"nested":"{\\"comment\\":\\"' + nested + '\\"}"}\n').encode(
        "ascii"
    )
    inner = json.loads(payload)["nested"]
    assert secret not in payload
    assert secret not in inner.encode("ascii")
    assert json.loads(inner)["comment"] == secret_text
    (root / "response.json").write_bytes(payload)
    receipt = tmp_path / "scan.json"

    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(receipt),
                "--env-file",
                str(_env_file(tmp_path, secret)),
            ]
        )
        == 1
    )
    assert not receipt.exists()


def test_json_escaped_non_secret_content_still_passes(tmp_path: Path) -> None:
    secret = b"TE_SYNTHETIC_SCAN_PROBE_f0e1d2c3"
    root = tmp_path / "artifacts"
    root.mkdir()
    harmless = "harmless commentary"
    payload = (
        '{"comment":"'
        + _json_escape_chars(harmless, frozenset(range(len(harmless))))
        + '","note":"a\\nb \\"q\\" \\/x\\/"}\n'
    ).encode("ascii")
    (root / "response.json").write_bytes(payload)
    receipt = tmp_path / "scan.json"

    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(receipt),
                "--env-file",
                str(_env_file(tmp_path, secret)),
            ]
        )
        == 0
    )
    document = json.loads(receipt.read_bytes())
    assert document["status"] == "PASS"
    # Unescaping is a scan-only view: the commitment covers the wire bytes.
    assert document["bytes_scanned"] == len(payload)


def test_synthetic_canary_fails_closed(tmp_path: Path) -> None:
    secret = b"ordinary-live-secret"
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "response.txt").write_text(
        scan.SYNTHETIC_CANARIES[0],
        encoding="utf-8",
    )

    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(tmp_path / "scan.json"),
                "--env-file",
                str(_env_file(tmp_path, secret)),
            ]
        )
        == 1
    )


def test_symlink_and_in_tree_receipt_are_rejected(tmp_path: Path) -> None:
    secret = b"ordinary-live-secret"
    env_file = _env_file(tmp_path, secret)
    outside = tmp_path / "outside.txt"
    outside.write_text("safe", encoding="utf-8")
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "safe.txt").write_text("safe", encoding="utf-8")
    (root / "escape").symlink_to(outside)

    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(tmp_path / "scan.json"),
                "--env-file",
                str(env_file),
            ]
        )
        == 1
    )
    (root / "escape").unlink()
    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(root / "scan.json"),
                "--env-file",
                str(env_file),
            ]
        )
        == 1
    )


def test_existing_receipt_is_not_overwritten(tmp_path: Path) -> None:
    secret = b"ordinary-live-secret"
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "safe.txt").write_text("safe", encoding="utf-8")
    receipt = tmp_path / "scan.json"
    receipt.write_text("preserve", encoding="utf-8")

    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(receipt),
                "--env-file",
                str(_env_file(tmp_path, secret)),
            ]
        )
        == 1
    )
    assert receipt.read_text(encoding="utf-8") == "preserve"


def test_outside_looking_symlink_parent_into_tree_is_rejected(
    tmp_path: Path,
) -> None:
    secret = b"ordinary-live-secret"
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "safe.txt").write_text("safe", encoding="utf-8")
    alias = tmp_path / "outside-alias"
    alias.symlink_to(root, target_is_directory=True)
    receipt = alias / "scan.json"

    assert (
        scan.main(
            [
                "--root",
                str(root),
                "--receipt",
                str(receipt),
                "--env-file",
                str(_env_file(tmp_path, secret)),
            ]
        )
        == 1
    )
    assert not receipt.exists()


def test_receipt_publish_race_never_overwrites_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "scan.json"
    original_link = os.link

    def racing_link(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        follow_symlinks: bool = True,
    ) -> None:
        Path(target).write_text("race-winner", encoding="utf-8")
        original_link(
            source,
            target,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(scan.os, "link", racing_link)
    with pytest.raises(scan.SecretScanError, match="already exists"):
        scan.write_receipt(destination, {"status": "PASS"})

    assert destination.read_text(encoding="utf-8") == "race-winner"
    assert list(tmp_path.glob(".scan.json.tmp-*")) == []
