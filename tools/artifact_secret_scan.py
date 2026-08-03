#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Scan a completed artifact tree for credentials without logging them.

The scanner reads only explicitly named credentials from the repository
``.env`` file, derives the same common reversible encodings rejected at the
HTTP ingress boundary, and checks every regular file in the target tree.  A
successful run writes a secret-free, hash-bound receipt outside that tree.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final
from urllib.parse import quote_from_bytes

ROOT: Final = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sandbox.orchestration import (  # noqa: E402
    PreflightFailure,
    load_protected_credentials,
)
from spec.canonical import canonical_bytes, sha256_prefixed  # noqa: E402

DEFAULT_CREDENTIAL_ENV_NAMES: Final = ("FIREWORKS_API_KEY",)
SYNTHETIC_CANARIES: Final = (
    "TE_SYNTHETIC_SECRET_CANARY_7b72a199",
    "TE_SYNTHETIC_SECRET_CANARY_2b12d8fb",
)

_BACKSLASH: Final = 0x5C
_HEX_DIGITS: Final = frozenset(b"0123456789abcdefABCDEF")
_JSON_SIMPLE_ESCAPES: Final = {
    ord('"'): b'"',
    ord("\\"): b"\\",
    ord("/"): b"/",
    ord("b"): b"\b",
    ord("f"): b"\f",
    ord("n"): b"\n",
    ord("r"): b"\r",
    ord("t"): b"\t",
}
# An artifact may nest one JSON document inside another's string values, so a
# single unescape pass is not enough to expose a doubly escaped credential.
MAX_UNESCAPE_PASSES: Final = 3


class SecretScanError(RuntimeError):
    """The artifact tree could not be proved free of protected values."""


def _is_hex_quad(digits: bytes) -> bool:
    return len(digits) == 4 and all(digit in _HEX_DIGITS for digit in digits)


def _json_escaped(raw: bytes, *, upper: bool) -> bytes | None:
    """Return the fully ``\\uXXXX``-escaped form of ``raw``, if representable."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    template = "\\u{:04X}" if upper else "\\u{:04x}"
    parts: list[str] = []
    for char in text:
        code = ord(char)
        if code > 0xFFFF:
            offset = code - 0x10000
            parts.append(template.format(0xD800 + (offset >> 10)))
            parts.append(template.format(0xDC00 + (offset & 0x3FF)))
        else:
            parts.append(template.format(code))
    return "".join(parts).encode("ascii")


def decode_json_escapes(text: bytes) -> bytes:
    """Resolve JSON string escapes so encoded credentials cannot hide.

    Mirrors ``harness.http._decode_json_escapes``: escaping a *single*
    character of a credential defeats plain substring matching, so the scanned
    buffer is decoded rather than only comparing whole-credential encodings.
    Unrecognised escapes are preserved verbatim, making this a superset scan
    that never hides material the raw buffer already shows.
    """

    decoded = bytearray()
    index = 0
    length = len(text)
    while index < length:
        byte = text[index]
        if byte != _BACKSLASH or index + 1 >= length:
            decoded.append(byte)
            index += 1
            continue
        simple = _JSON_SIMPLE_ESCAPES.get(text[index + 1])
        if simple is not None:
            decoded += simple
            index += 2
            continue
        digits = text[index + 2 : index + 6]
        if text[index + 1] not in (ord("u"), ord("U")) or not _is_hex_quad(digits):
            decoded.append(byte)
            index += 1
            continue
        code = int(digits, 16)
        index += 6
        if 0xD800 <= code <= 0xDBFF and index + 6 <= length:
            low_digits = text[index + 2 : index + 6]
            if (
                text[index] == _BACKSLASH
                and text[index + 1] in (ord("u"), ord("U"))
                and _is_hex_quad(low_digits)
                and 0xDC00 <= int(low_digits, 16) <= 0xDFFF
            ):
                low = int(low_digits, 16) - 0xDC00
                code = 0x10000 + ((code - 0xD800) << 10) + low
                index += 6
        decoded += chr(code).encode("utf-8", "surrogatepass")
    return bytes(decoded)


def scan_variants(payload: bytes) -> tuple[bytes, ...]:
    """Return the raw payload plus its progressively JSON-unescaped forms."""

    if _BACKSLASH not in payload:
        return (payload,)
    variants = [payload]
    seen = {payload}
    current = payload
    for _ in range(MAX_UNESCAPE_PASSES):
        current = decode_json_escapes(current)
        if current in seen:
            break
        seen.add(current)
        variants.append(current)
        if _BACKSLASH not in current:
            break
    return tuple(variants)


def protected_representations(
    values: Sequence[str | bytes],
) -> tuple[bytes, ...]:
    """Return exact and common reversible encodings without logging inputs."""

    protected: set[bytes] = set()
    for index, value in enumerate(values):
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if not isinstance(raw, bytes) or not raw:
            raise SecretScanError(
                f"protected value {index} must be non-empty bytes or text"
            )
        protected.update(
            {
                raw,
                raw.hex().encode("ascii"),
                raw.hex().upper().encode("ascii"),
                base64.b64encode(raw),
                base64.b64encode(raw).rstrip(b"="),
                base64.urlsafe_b64encode(raw),
                base64.urlsafe_b64encode(raw).rstrip(b"="),
                quote_from_bytes(raw).encode("ascii"),
            }
        )
        protected.update(
            escaped
            for escaped in (
                _json_escaped(raw, upper=False),
                _json_escaped(raw, upper=True),
            )
            if escaped
        )
    return tuple(sorted(protected, key=lambda item: (-len(item), item)))


def _resolved_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise SecretScanError(f"{label} must be a plain directory")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise SecretScanError(f"{label} could not be resolved") from exc


def _regular_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    try:
        candidates = sorted(root.rglob("*"), key=lambda path: path.as_posix())
    except OSError as exc:
        raise SecretScanError("artifact tree could not be enumerated") from exc
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SecretScanError(
                f"artifact tree contains a symlink: {relative}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise SecretScanError(
                f"artifact tree contains a non-regular entry: {relative}"
            )
        files.append(path)
    if not files:
        raise SecretScanError("artifact tree contains no files")
    return tuple(files)


def scan_tree(
    root: Path,
    protected: Sequence[bytes],
) -> dict[str, object]:
    """Scan regular files and return a secret-free content commitment."""

    resolved_root = _resolved_directory(root, "artifact root")
    if not protected or any(not item for item in protected):
        raise SecretScanError("protected representations must be non-empty")

    entries: list[dict[str, object]] = []
    total_bytes = 0
    for path in _regular_files(resolved_root):
        relative = path.relative_to(resolved_root).as_posix()
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise SecretScanError(
                f"artifact file could not be read: {relative}"
            ) from exc
        if any(
            representation in variant
            for variant in scan_variants(payload)
            for representation in protected
        ):
            raise SecretScanError(
                f"protected credential representation found in: {relative}"
            )
        total_bytes += len(payload)
        entries.append(
            {
                "path": relative,
                "bytes": len(payload),
                "sha256": sha256_prefixed(payload),
            }
        )

    tree_document = {
        "schema": "artifact_secret_scan_tree/v1",
        "files": entries,
    }
    return {
        "files_scanned": len(entries),
        "bytes_scanned": total_bytes,
        "tree_sha256": sha256_prefixed(canonical_bytes(tree_document)),
    }


def build_receipt(
    *,
    root: Path,
    credential_env_names: Sequence[str],
    representation_count: int,
    scan: dict[str, object],
    created_at_ms: int,
) -> dict[str, object]:
    """Build the public receipt; credential values never enter this object."""

    return {
        "schema": "artifact_secret_scan/v1",
        "status": "PASS",
        "created_at_ms": created_at_ms,
        "root": str(root),
        "credential_env_names": list(credential_env_names),
        "synthetic_canary_count": len(SYNTHETIC_CANARIES),
        "representation_count": representation_count,
        "match_count": 0,
        **scan,
    }


def _reject_symlink_path(path: Path, label: str) -> None:
    """Reject a symlink at the path or in any existing ancestor."""

    current = path
    while True:
        if current.is_symlink():
            raise SecretScanError(
                f"{label} may not traverse a symlink: {current}"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _receipt_destination(path: Path, scanned_root: Path) -> Path:
    """Create and resolve a plain parent, then prove the receipt is outside."""

    candidate = path.absolute()
    _reject_symlink_path(candidate, "receipt path")
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SecretScanError("receipt directory could not be created") from exc
    _reject_symlink_path(candidate, "receipt path")
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise SecretScanError("receipt directory could not be resolved") from exc
    if not parent.is_dir():
        raise SecretScanError("receipt parent must be a plain directory")
    destination = parent / candidate.name
    try:
        destination.relative_to(scanned_root)
    except ValueError:
        pass
    else:
        raise SecretScanError(
            "receipt must be outside the scanned artifact tree"
        )
    return destination


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SecretScanError(
            "receipt directory could not be opened for fsync"
        ) from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_receipt(path: Path, receipt: dict[str, object]) -> None:
    """Atomically create one immutable, owner-readable receipt."""

    destination = path.absolute()
    _reject_symlink_path(destination, "receipt path")
    if destination.exists() or destination.is_symlink():
        raise SecretScanError("receipt path already exists")
    if not destination.parent.is_dir():
        raise SecretScanError("receipt parent must be a plain directory")
    temporary = destination.with_name(
        f".{destination.name}.tmp-{secrets.token_hex(8)}"
    )
    payload = canonical_bytes(receipt) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(descriptor, view[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, destination, follow_symlinks=False)
        _fsync_directory(destination.parent)
    except FileExistsError as exc:
        raise SecretScanError("receipt path already exists") from exc
    except OSError as exc:
        raise SecretScanError("receipt could not be written") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SecretScanError(
                "temporary receipt could not be removed"
            ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--credential-env-name",
        action="append",
        dest="credential_env_names",
        default=None,
        help="Credential name to load; repeat for multiple values.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    names = tuple(args.credential_env_names or DEFAULT_CREDENTIAL_ENV_NAMES)
    try:
        root = _resolved_directory(args.root.absolute(), "artifact root")
        receipt = _receipt_destination(args.receipt, root)
        credential_values = load_protected_credentials(
            args.env_file.absolute(),
            names,
        )
        protected = protected_representations(
            (*credential_values, *SYNTHETIC_CANARIES)
        )
        scan = scan_tree(root, protected)
        document = build_receipt(
            root=root,
            credential_env_names=names,
            representation_count=len(protected),
            scan=scan,
            created_at_ms=time.time_ns() // 1_000_000,
        )
        write_receipt(receipt, document)
    except (PreflightFailure, SecretScanError) as exc:
        print(f"artifact secret scan failed: {exc}", file=sys.stderr)
        return 1
    print(
        "artifact secret scan PASS: "
        f"{document['files_scanned']} files, "
        f"{document['bytes_scanned']} bytes, "
        f"{document['tree_sha256']}; receipt={receipt}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
