# SPDX-License-Identifier: Apache-2.0
"""RFC 8785 (JCS) canonical JSON, content hashing, and hash chains.

This module is the single serialization boundary for everything that gets
hashed in WAGMI Bench (DET-2, DET-3). It enforces, at encode time:

- RFC 8785 canonical form: no whitespace, object members sorted by UTF-16
  code units, minimal string escaping, UTF-8 output bytes.
- The project-wide float ban (DET-4 at the serialization boundary): any
  ``float`` anywhere in a payload raises :class:`FloatNotAllowedError`.
  All money/price/rate fields are integers (ticks, micro-USDT, 1e-8 units),
  so canonical numbers are exactly the I-JSON-safe integers and nothing else.
- I-JSON integer range: |n| must be <= 2**53 - 1 so every canonical number
  round-trips exactly through any IEEE-754/ECMAScript JSON implementation
  (RFC 8785 serializes numbers via ECMAScript Number::toString).

Hash-chain construction follows ``docs/contracts/IC-5.md`` / ``chain/v1``:

    run_config_sha256 = SHA256(JCS(manifest.run_config))
    genesis(stream)   = SHA256(JCS({schema:"chain_genesis/v1", stream, run_id,
                                    pack_content_hash, agent_manifest_sha256,
                                    run_config_sha256}))
    link_i            = SHA256(JCS({schema:"chain_link/v1", stream, seq: i,
                                    record_sha256: h_i, prev: link_{i-1}}))
                        # prev = genesis for i = 0

Chains are linear; verification is whole-prefix and the first diverging seq
NAMES the record (SCH-3). Verification operates on stored bytes, never on
re-encoded parsed objects. Prefix consistency alone cannot see a ROLLBACK
(records and links jointly truncated); sealed-bundle verification therefore
binds to the seal's stream_head via verify_chain(expected_head=...,
expected_count=...) — see verify_chain's docstring.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, TypedDict

__all__ = [
    "CanonicalizationError",
    "ChainBuilder",
    "ChainLink",
    "ChainVerificationError",
    "FloatNotAllowedError",
    "StreamName",
    "canonical_bytes",
    "chain_genesis",
    "chain_link_value",
    "content_hash",
    "run_config_sha256",
    "seal_root",
    "sha256_prefixed",
    "verify_chain",
]

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class CanonicalizationError(ValueError):
    """A value cannot be canonicalized under RFC 8785 + project rules."""


class FloatNotAllowedError(CanonicalizationError):
    """A float reached the serialization boundary (forbidden project-wide).

    Money, prices, and rates are integer fixed-point (ticks, micro-USDT,
    1e-8 units); a float in a hashed payload is always a bug (DET-4).
    """


class ChainVerificationError(Exception):
    """Hash-chain verification failed; names the first offending entry."""

    def __init__(self, stream: str, seq: int, reason: str) -> None:
        self.stream: Final[str] = stream
        self.seq: Final[int] = seq
        self.reason: Final[str] = reason
        super().__init__(f"chain verification failed at stream={stream!r} seq={seq}: {reason}")


# --------------------------------------------------------------------------
# RFC 8785 canonical JSON encoding
# --------------------------------------------------------------------------

# Largest integer exactly representable as an IEEE-754 double (2**53 - 1).
# RFC 8785 serializes numbers through ECMAScript Number::toString; integers
# beyond this bound would not survive a JS round-trip byte-identically.
_MAX_SAFE_INT: Final[int] = 2**53 - 1

# RFC 8785 3.2.2.2: two-character escape shortcuts; all other control
# characters use lowercase \u00xx. '/' is never escaped.
_ESCAPES: Final[dict[str, str]] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _encode_string(value: str, out: list[str]) -> None:
    out.append('"')
    for ch in value:
        escaped = _ESCAPES.get(ch)
        if escaped is not None:
            out.append(escaped)
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')


def _sort_key(key: str) -> bytes:
    # RFC 8785 3.2.3: property names sort on their UTF-16 code units.
    # Big-endian UTF-16 bytes compare identically to code-unit order
    # (surrogate pairs included, since they are just code units here).
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:  # lone surrogates etc.
        raise CanonicalizationError(f"object key is not valid Unicode: {key!r}") from exc


def _encode(value: object, out: list[str], path: str) -> None:
    if value is None:
        out.append("null")
    elif isinstance(value, bool):  # must precede int: bool is an int subclass
        out.append("true" if value else "false")
    elif isinstance(value, float):
        raise FloatNotAllowedError(
            f"float {value!r} at {path}: floats are forbidden in hashed payloads; "
            "use integer fixed-point units (ticks / micro-USDT / 1e-8 rates)"
        )
    elif isinstance(value, int):
        # Read the numeric value through the BASE type only (DET-2): an int
        # subclass (e.g. a typed Micros/Ticks money wrapper) may override
        # __str__/__int__/__lt__, and none of those overrides may be allowed
        # to influence the hashed bytes. int.__index__ returns the stored
        # integer value via the base C implementation, and int.__repr__
        # renders its canonical decimal form; subclass methods are never
        # consulted, so canonical_bytes(W(5)) == canonical_bytes(5) for any
        # int subclass W.
        plain = int.__index__(value)
        if not -_MAX_SAFE_INT <= plain <= _MAX_SAFE_INT:
            raise CanonicalizationError(
                f"integer {plain} at {path} exceeds I-JSON safe range (|n| <= 2**53-1); "
                "it would not round-trip through IEEE-754 JSON implementations"
            )
        out.append(int.__repr__(plain))
    elif isinstance(value, str):
        _encode_string(value, out)
    elif isinstance(value, Mapping):
        for key in value.keys():
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"object key {key!r} at {path} is {type(key).__name__}; keys must be str"
                )
        out.append("{")
        first = True
        for key in sorted(value.keys(), key=_sort_key):
            if not first:
                out.append(",")
            first = False
            _encode_string(key, out)
            out.append(":")
            _encode(value[key], out, f"{path}.{key}")
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode(item, out, f"{path}[{i}]")
        out.append("]")
    else:
        raise CanonicalizationError(
            f"type {type(value).__name__} at {path} is not JCS-encodable "
            "(allowed: None, bool, int, str, Mapping, list/tuple)"
        )


def canonical_bytes(value: object) -> bytes:
    """Encode ``value`` as RFC 8785 canonical JSON, returned as UTF-8 bytes.

    Raises :class:`FloatNotAllowedError` on any float and
    :class:`CanonicalizationError` on non-encodable types, non-str keys,
    invalid Unicode, or integers outside the I-JSON safe range.
    """
    out: list[str] = []
    _encode(value, out, "$")
    try:
        return "".join(out).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError(f"payload contains invalid Unicode: {exc}") from exc


# --------------------------------------------------------------------------
# Content hashing
# --------------------------------------------------------------------------


def sha256_prefixed(data: bytes) -> str:
    """SHA-256 of raw bytes in the project's ``sha256:<64-hex>`` form.

    Use this for *stored bytes* (JSONL lines excluding their terminating
    newline, whole files) — verification always hashes stored bytes.
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


def content_hash(value: object) -> str:
    """``sha256:<hex>`` over the RFC 8785 canonical bytes of ``value``.

    This is the content address of a logical record: any two logically equal
    payloads (regardless of key insertion order) hash identically.
    """
    return sha256_prefixed(canonical_bytes(value))


_HASH_PREFIX: Final[str] = "sha256:"
_HASH_LEN: Final[int] = len(_HASH_PREFIX) + 64


def _require_hash(value: str, what: str) -> str:
    if (
        len(value) != _HASH_LEN
        or not value.startswith(_HASH_PREFIX)
        or any(c not in "0123456789abcdef" for c in value[len(_HASH_PREFIX) :])
    ):
        raise CanonicalizationError(f"{what} must match 'sha256:<64 lowercase hex>', got {value!r}")
    return value


# --------------------------------------------------------------------------
# Hash chain (chain/v1, docs/contracts/IC-5.md)
# --------------------------------------------------------------------------

StreamName = Literal["events", "decisions"]


class ChainLink(TypedDict):
    """One ``chain_link/v1`` line of ``chain.jsonl``."""

    schema: str
    stream: str
    seq: int
    record_sha256: str
    link_sha256: str


def run_config_sha256(run_config: Mapping[str, object]) -> str:
    """``SHA256(JCS(bundle_manifest.run_config))`` in prefixed form."""
    return content_hash(run_config)


def chain_genesis(
    stream: StreamName,
    *,
    run_id: str,
    pack_content_hash: str,
    agent_manifest_sha256: str,
    run_config_sha256: str,
) -> str:
    """Genesis link value binding a chain to run identity (chain/v1).

    ``genesis = SHA256(JCS({schema:"chain_genesis/v1", stream, run_id,
    pack_content_hash, agent_manifest_sha256, run_config_sha256}))`` — a chain
    cannot be transplanted between runs.
    """
    return content_hash(
        {
            "schema": "chain_genesis/v1",
            "stream": stream,
            "run_id": run_id,
            "pack_content_hash": _require_hash(pack_content_hash, "pack_content_hash"),
            "agent_manifest_sha256": _require_hash(agent_manifest_sha256, "agent_manifest_sha256"),
            "run_config_sha256": _require_hash(run_config_sha256, "run_config_sha256"),
        }
    )


def chain_link_value(stream: StreamName, seq: int, record_sha256: str, prev: str) -> str:
    """Link value: ``SHA256(JCS({schema:"chain_link/v1", stream, seq,
    record_sha256, prev}))`` with ``prev`` = previous link (genesis for seq 0).
    """
    if seq < 0:
        raise CanonicalizationError(f"seq must be >= 0, got {seq}")
    return content_hash(
        {
            "schema": "chain_link/v1",
            "stream": stream,
            "seq": seq,
            "record_sha256": _require_hash(record_sha256, "record_sha256"),
            "prev": _require_hash(prev, "prev"),
        }
    )


@dataclass
class ChainBuilder:
    """Incrementally build one linear chain over stored record bytes.

    Feed each record's stored bytes (JSONL line without its terminating
    newline; decision file: whole file bytes) to :meth:`append`, which returns
    the ``chain_link/v1`` dict to write (and fsync) alongside the record.
    """

    stream: StreamName
    genesis: str
    _head: str = field(init=False)
    _count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        _require_hash(self.genesis, "genesis")
        self._head = self.genesis

    @property
    def head(self) -> str:
        """Current head link value (genesis if no records appended)."""
        return self._head

    @property
    def count(self) -> int:
        """Number of records chained so far."""
        return self._count

    def append(self, record_bytes: bytes) -> ChainLink:
        record_hash = sha256_prefixed(record_bytes)
        link = chain_link_value(self.stream, self._count, record_hash, self._head)
        entry: ChainLink = {
            "schema": "chain_link/v1",
            "stream": self.stream,
            "seq": self._count,
            "record_sha256": record_hash,
            "link_sha256": link,
        }
        self._head = link
        self._count += 1
        return entry

    def stream_head(self) -> dict[str, object]:
        """The chain/v1 ``stream_head`` summary for the seal."""
        return {"genesis": self.genesis, "head": self._head, "count": self._count}


def verify_chain(
    stream: StreamName,
    genesis: str,
    records: Sequence[bytes],
    links: Iterable[ChainLink],
    *,
    expected_head: str | None = None,
    expected_count: int | None = None,
) -> str:
    """Verify a whole chain prefix against stored record bytes.

    Recomputes every record hash and link value from ``records`` (stored
    bytes) and compares against the stored ``chain.jsonl`` links. Returns the
    final head link value on success. Raises :class:`ChainVerificationError`
    naming the first offending entry (stream, seq, reason) on any divergence:
    wrong stream/seq/schema, record hash mismatch (tamper), link mismatch, or
    record/link count mismatch.

    Rollback/truncation binding (DET-2/SCH-3): internal consistency alone
    cannot detect a ROLLBACK — dropping the last N records together with
    their last N links leaves a perfectly consistent shorter prefix (and an
    empty prefix "verifies" trivially against any well-formed genesis). Pass
    ``expected_head`` and ``expected_count`` from the sealed ``chain.json``
    ``stream_head`` (:meth:`ChainBuilder.stream_head`) to bind verification
    to the sealed end of the chain; a recomputed head or record count that
    does not match raises :class:`ChainVerificationError` naming the first
    missing seq. Callers verifying a SEALED bundle must always pass both;
    omitting them is only legitimate for the crash-recovery TRUNCATED
    verdict (IC-5), where no seal exists and the caller reports the prefix
    as a prefix, never as the complete stream.
    """
    _require_hash(genesis, "genesis")
    if expected_head is not None:
        _require_hash(expected_head, "expected_head")
    if expected_count is not None and expected_count != len(records):
        raise ChainVerificationError(
            stream,
            min(expected_count, len(records)),
            f"{len(records)} records but sealed stream_head.count is {expected_count} "
            "(rollback/truncation or forged seal)",
        )
    prev = genesis
    seq = -1
    link_list = list(links)
    if len(link_list) != len(records):
        raise ChainVerificationError(
            stream,
            min(len(link_list), len(records)),
            f"{len(records)} records but {len(link_list)} chain links",
        )
    for seq, (record_bytes, link) in enumerate(zip(records, link_list, strict=True)):
        if link.get("schema") != "chain_link/v1":
            raise ChainVerificationError(stream, seq, f"bad link schema {link.get('schema')!r}")
        if link.get("stream") != stream:
            raise ChainVerificationError(
                stream, seq, f"link belongs to stream {link.get('stream')!r}"
            )
        if link.get("seq") != seq:
            raise ChainVerificationError(stream, seq, f"link seq is {link.get('seq')!r}")
        expected_record = sha256_prefixed(record_bytes)
        if link.get("record_sha256") != expected_record:
            raise ChainVerificationError(
                stream,
                seq,
                f"record hash mismatch: stored {link.get('record_sha256')!r}, "
                f"recomputed {expected_record!r} (record tampered or link forged)",
            )
        expected_link = chain_link_value(stream, seq, expected_record, prev)
        if link.get("link_sha256") != expected_link:
            raise ChainVerificationError(
                stream,
                seq,
                f"link value mismatch: stored {link.get('link_sha256')!r}, "
                f"recomputed {expected_link!r}",
            )
        prev = expected_link
    if expected_head is not None and prev != expected_head:
        raise ChainVerificationError(
            stream,
            max(len(records) - 1, 0),
            f"recomputed head {prev!r} does not match sealed stream_head.head "
            f"{expected_head!r} (rollback past the seal, or a foreign chain)",
        )
    return prev


def seal_root(seal: Mapping[str, object]) -> str:
    """Root of a chain/v1 seal: SHA-256 over its JCS bytes with ``root`` removed.

    Accepts the seal with or without a ``root`` member already present; the
    member is excluded from hashing either way. This value is the bundle's
    single publishable identity.
    """
    unrooted = {k: v for k, v in seal.items() if k != "root"}
    return content_hash(unrooted)
