# SPDX-License-Identifier: Apache-2.0
"""Tests for spec.canonical: RFC 8785 vectors, float ban, hash chains.

RFC vectors are taken from RFC 8785 (JCS) sections 3.2.2.2 (string
escaping) and 3.2.3 (property sorting on UTF-16 code units). The RFC's
floating-point vectors are deliberately NOT covered: this project bans
floats in hashed payloads outright (DET-4), so the encoder must raise on
them instead of serializing them.
"""

from __future__ import annotations

import copy

import pytest

from spec.canonical import (
    CanonicalizationError,
    ChainBuilder,
    ChainLink,
    ChainVerificationError,
    FloatNotAllowedError,
    canonical_bytes,
    chain_genesis,
    chain_link_value,
    content_hash,
    run_config_sha256,
    seal_root,
    sha256_prefixed,
    verify_chain,
)

# --------------------------------------------------------------------------
# RFC 8785 test vectors
# --------------------------------------------------------------------------


class TestRfc8785Strings:
    def test_rfc_3_2_2_2_escaping_vector(self) -> None:
        # RFC 8785 3.2.2.2 example string. Parsed value (12 chars):
        #   U+20AC $ U+000F U+000A A ' B " \ \ " /
        value = "\u20ac$\u000f\nA'B\"\\\\\"/"
        assert len(value) == 12
        expected = (
            '"'
            + "\u20ac"  # literal, not escaped
            + "$"
            + "\\u000f"  # control char without shortcut: lowercase \u00xx
            + "\\n"  # shortcut escape
            + "A'B"
            + '\\"'
            + "\\\\"  # first backslash
            + "\\\\"  # second backslash
            + '\\"'
            + "/"  # solidus never escaped
            + '"'
        ).encode("utf-8")
        assert canonical_bytes(value) == expected

    def test_two_char_escape_shortcuts(self) -> None:
        assert canonical_bytes("\b\t\n\f\r\"\\") == b'"\\b\\t\\n\\f\\r\\"\\\\"'

    def test_other_control_chars_use_lowercase_u00xx(self) -> None:
        assert canonical_bytes("\x00\x01\x1f") == b'"\\u0000\\u0001\\u001f"'

    def test_solidus_not_escaped(self) -> None:
        assert canonical_bytes("a/b") == b'"a/b"'

    def test_non_ascii_emitted_literally_as_utf8(self) -> None:
        # U+0080 is a C1 control but >= 0x20: emitted as literal UTF-8.
        assert canonical_bytes("\u0080") == b'"\xc2\x80"'
        assert canonical_bytes("\u20ac") == b'"\xe2\x82\xac"'
        assert canonical_bytes("\U0001f600") == b'"\xf0\x9f\x98\x80"'


class TestRfc8785Sorting:
    def test_rfc_3_2_3_utf16_code_unit_ordering_vector(self) -> None:
        # RFC 8785 3.2.3 example object; expected member order:
        # \r, 1, U+0080, U+00F6, U+20AC, U+1F600 (surrogates D83D DE00), U+FB33
        value = {
            "\u20ac": "Euro Sign",
            "\r": "Carriage Return",
            "דּ": "Hebrew Letter Dalet With Dagesh",
            "1": "One",
            "\U0001f600": "Emoji: Grinning Face",
            "\u0080": "Control",
            "ö": "Latin Small Letter O With Diaeresis",
        }
        expected = (
            "{"
            '"\\r":"Carriage Return",'
            '"1":"One",'
            '"\u0080":"Control",'
            '"ö":"Latin Small Letter O With Diaeresis",'
            '"\u20ac":"Euro Sign",'
            '"\U0001f600":"Emoji: Grinning Face",'
            '"דּ":"Hebrew Letter Dalet With Dagesh"'
            "}"
        ).encode("utf-8")
        assert canonical_bytes(value) == expected

    def test_surrogate_pair_sorts_before_high_bmp_char(self) -> None:
        # U+1F600 encodes as surrogates D83D DE00; D83D < FB33, so the emoji
        # key must precede U+FB33 — the OPPOSITE of code-point order. A naive
        # sorted(keys) gets this wrong.
        value = {"דּ": 1, "\U0001f600": 2}
        assert canonical_bytes(value) == '{"\U0001f600":2,"דּ":1}'.encode("utf-8")
        assert sorted(value.keys()) == ["דּ", "\U0001f600"]  # naive order differs

    def test_empty_key_sorts_first(self) -> None:
        assert canonical_bytes({"a": 1, "": 2}) == b'{"":2,"a":1}'

    def test_shorter_prefix_sorts_first(self) -> None:
        assert canonical_bytes({"ab": 1, "a": 2}) == b'{"a":2,"ab":1}'


class TestRfc8785Structure:
    def test_no_whitespace_nested(self) -> None:
        value = {"b": [1, {"y": None, "x": True}], "a": False}
        assert canonical_bytes(value) == b'{"a":false,"b":[1,{"x":true,"y":null}]}'

    def test_literals(self) -> None:
        assert canonical_bytes([None, True, False]) == b"[null,true,false]"

    def test_empty_containers(self) -> None:
        assert canonical_bytes({}) == b"{}"
        assert canonical_bytes([]) == b"[]"

    def test_tuple_encodes_as_array(self) -> None:
        assert canonical_bytes((1, 2)) == b"[1,2]"


class TestIntegerNormalization:
    def test_plain_integers(self) -> None:
        assert canonical_bytes(0) == b"0"
        assert canonical_bytes(1) == b"1"
        assert canonical_bytes(-5) == b"-5"
        assert canonical_bytes(1000000) == b"1000000"

    def test_max_safe_integer_boundary(self) -> None:
        # RFC 8785 numbers serialize via ECMAScript Number::toString;
        # 2**53 - 1 is the largest integer that survives exactly.
        assert canonical_bytes(9007199254740991) == b"9007199254740991"
        assert canonical_bytes(-9007199254740991) == b"-9007199254740991"
        with pytest.raises(CanonicalizationError):
            canonical_bytes(9007199254740992)
        with pytest.raises(CanonicalizationError):
            canonical_bytes(-9007199254740992)

    def test_bools_are_not_integers(self) -> None:
        assert canonical_bytes({"a": True}) != canonical_bytes({"a": 1})

    def test_int_subclass_str_override_cannot_inject_bytes(self) -> None:
        # DET-2 (M0 audit round 2): a typed money wrapper subclassing int
        # (Micros/Ticks style) that overrides __str__/__int__ must not be able
        # to alter canonical bytes — the encoder reads the stored value via
        # the BASE int implementation only.
        class W(int):
            def __str__(self) -> str:  # malicious / decorative override
                return "PWNED"

            def __int__(self) -> int:  # lying numeric override
                return 999

        assert str(W(5)) == "PWNED"  # the override is really in force
        assert canonical_bytes(W(5)) == b"5"
        assert content_hash(W(5)) == content_hash(5)
        assert canonical_bytes({"px": W(-42)}) == b'{"px":-42}'

    def test_int_subclass_comparison_override_cannot_bypass_range_check(self) -> None:
        class Sneaky(int):
            def __le__(self, other: object) -> bool:
                return True

            def __ge__(self, other: object) -> bool:
                return True

        with pytest.raises(CanonicalizationError):
            canonical_bytes(Sneaky(2**53))
        assert canonical_bytes(Sneaky(7)) == b"7"

    def test_int_enum_encodes_as_plain_integer(self) -> None:
        from enum import IntEnum

        class Side(IntEnum):
            BUY = 1

        assert canonical_bytes({"side": Side.BUY}) == b'{"side":1}'
        assert content_hash(Side.BUY) == content_hash(1)


# --------------------------------------------------------------------------
# Float rejection (DET-4 at the serialization boundary)
# --------------------------------------------------------------------------


class TestFloatRejection:
    @pytest.mark.parametrize(
        "payload",
        [
            1.0,
            0.5,
            -3.14,
            float("nan"),
            float("inf"),
            float("-inf"),
            {"price": 100.0},
            {"nested": {"deep": [1, 2, 3.0]}},
            [0, 1.5],
        ],
    )
    def test_any_float_anywhere_raises(self, payload: object) -> None:
        with pytest.raises(FloatNotAllowedError):
            canonical_bytes(payload)

    def test_error_names_the_path(self) -> None:
        with pytest.raises(FloatNotAllowedError, match=r"\$\.a\[1\]\.px"):
            canonical_bytes({"a": [0, {"px": 1.5}]})

    def test_integer_valued_float_still_rejected(self) -> None:
        # 100.0 == 100 but it is a float and must be rejected, not coerced.
        with pytest.raises(FloatNotAllowedError):
            canonical_bytes({"qty": 100.0})

    @pytest.mark.parametrize("bad", [b"bytes", {1: "int key"}, {("t",): 1}, set(), object()])
    def test_non_json_types_rejected(self, bad: object) -> None:
        with pytest.raises(CanonicalizationError):
            canonical_bytes(bad)


# --------------------------------------------------------------------------
# Stability: logical equality => byte equality
# --------------------------------------------------------------------------


class TestStability:
    def test_insertion_order_is_irrelevant(self) -> None:
        a: dict[str, object] = {"z": 1, "a": 2, "m": {"k2": [1, 2], "k1": "x"}}
        b_inner: dict[str, object] = {"k1": "x"}
        b_inner["k2"] = [1, 2]
        b: dict[str, object] = {"a": 2}
        b["m"] = b_inner
        b["z"] = 1
        assert list(a.keys()) != list(b.keys())  # genuinely different insertion order
        assert canonical_bytes(a) == canonical_bytes(b)
        assert content_hash(a) == content_hash(b)

    def test_repeated_encoding_is_bytewise_stable(self) -> None:
        value = {"ts": 1583107200000, "rate_1e8": -300000, "sym": "BTCUSDT"}
        outs = {canonical_bytes(copy.deepcopy(value)) for _ in range(50)}
        assert len(outs) == 1

    def test_known_hash_cross_check(self) -> None:
        # Independently computed: sha256 of the 7 bytes {"a":1}
        assert (
            content_hash({"a": 1})
            == "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
        )
        assert (
            sha256_prefixed(b"")
            == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )


# --------------------------------------------------------------------------
# Hash chain (chain/v1 per docs/contracts/IC-5.md)
# --------------------------------------------------------------------------

_PACK_HASH = "sha256:" + "11" * 32
_AGENT_HASH = "sha256:" + "22" * 32


def _make_chain(n: int = 5) -> tuple[str, list[bytes], list[ChainLink], ChainBuilder]:
    rc_hash = run_config_sha256({"lookback_bars": 64, "seed": 42})
    genesis = chain_genesis(
        "events",
        run_id="run_0123456789abcdef",
        pack_content_hash=_PACK_HASH,
        agent_manifest_sha256=_AGENT_HASH,
        run_config_sha256=rc_hash,
    )
    records = [
        canonical_bytes({"schema": "event/v1", "seq": i, "type": "MarginUpdate", "ts_ms": 1000 + i})
        for i in range(n)
    ]
    builder = ChainBuilder(stream="events", genesis=genesis)
    links = [builder.append(r) for r in records]
    return genesis, records, links, builder


class TestChain:
    def test_build_and_verify_roundtrip(self) -> None:
        genesis, records, links, builder = _make_chain()
        head = verify_chain("events", genesis, records, links)
        assert head == builder.head
        assert builder.count == 5
        assert builder.stream_head() == {"genesis": genesis, "head": head, "count": 5}

    def test_empty_chain_head_is_genesis(self) -> None:
        genesis, _, _, _ = _make_chain(0)
        assert verify_chain("events", genesis, [], []) == genesis

    def test_link_values_follow_contract_formula(self) -> None:
        genesis, records, links, _ = _make_chain(2)
        h0 = sha256_prefixed(records[0])
        l0 = chain_link_value("events", 0, h0, genesis)  # prev = genesis for seq 0
        assert links[0]["link_sha256"] == l0
        h1 = sha256_prefixed(records[1])
        assert links[1]["link_sha256"] == chain_link_value("events", 1, h1, l0)

    def test_tampered_record_names_the_entry(self) -> None:
        genesis, records, links, _ = _make_chain()
        flipped = bytearray(records[3])
        flipped[5] ^= 0x01  # flip one bit of one byte
        tampered = records[:3] + [bytes(flipped)] + records[4:]
        with pytest.raises(ChainVerificationError) as exc:
            verify_chain("events", genesis, tampered, links)
        assert exc.value.stream == "events"
        assert exc.value.seq == 3
        assert "record hash mismatch" in exc.value.reason
        assert "seq=3" in str(exc.value)

    def test_forged_link_with_matching_record_hash_still_caught(self) -> None:
        # Attacker tampers record 2 AND rewrites its record_sha256 in the
        # stored link: the recomputed link value no longer matches.
        genesis, records, links, _ = _make_chain()
        flipped = bytearray(records[2])
        flipped[0] ^= 0xFF
        tampered_records = records[:2] + [bytes(flipped)] + records[3:]
        forged: list[ChainLink] = [
            copy.deepcopy(link) for link in links
        ]
        forged[2]["record_sha256"] = sha256_prefixed(bytes(flipped))
        with pytest.raises(ChainVerificationError) as exc:
            verify_chain("events", genesis, tampered_records, forged)
        assert exc.value.seq == 2
        assert "link value mismatch" in exc.value.reason

    def test_truncated_links_detected(self) -> None:
        genesis, records, links, _ = _make_chain()
        with pytest.raises(ChainVerificationError):
            verify_chain("events", genesis, records, links[:-1])

    def test_joint_truncation_is_consistent_without_seal_binding(self) -> None:
        # DET-2 (M0 audit round 2): dropping the last record AND its link is a
        # perfectly consistent shorter prefix — prefix verification alone
        # CANNOT catch a rollback. This test documents that contract boundary;
        # the sealed stream_head binding below is what catches it.
        genesis, records, links, _ = _make_chain()
        head = verify_chain("events", genesis, records[:-1], links[:-1])
        assert head == links[-2]["link_sha256"]  # silently returns the shorter head

    def test_joint_truncation_caught_by_sealed_head_binding(self) -> None:
        genesis, records, links, builder = _make_chain()
        sealed = builder.stream_head()
        # Full chain against the seal: passes.
        assert (
            verify_chain(
                "events",
                genesis,
                records,
                links,
                expected_head=str(sealed["head"]),
                expected_count=int(str(sealed["count"])),
            )
            == builder.head
        )
        # Rollback: last record + last link dropped together.
        with pytest.raises(ChainVerificationError) as exc:
            verify_chain(
                "events",
                genesis,
                records[:-1],
                links[:-1],
                expected_head=str(sealed["head"]),
                expected_count=int(str(sealed["count"])),
            )
        assert exc.value.seq == len(records) - 1  # names the first missing seq
        assert "stream_head.count" in exc.value.reason

    def test_empty_prefix_rejected_against_sealed_head(self) -> None:
        genesis, records, links, builder = _make_chain()
        sealed = builder.stream_head()
        # Without binding, an empty prefix "verifies" against any genesis...
        assert verify_chain("events", genesis, [], []) == genesis
        # ...with binding it is named as a rollback to seq 0.
        with pytest.raises(ChainVerificationError) as exc:
            verify_chain(
                "events",
                genesis,
                [],
                [],
                expected_head=str(sealed["head"]),
                expected_count=int(str(sealed["count"])),
            )
        assert exc.value.seq == 0

    def test_head_binding_alone_catches_rollback(self) -> None:
        # Even without a count (e.g. a caller that only kept the head), the
        # recomputed head of a rolled-back prefix cannot match the sealed head.
        genesis, records, links, builder = _make_chain()
        with pytest.raises(ChainVerificationError) as exc:
            verify_chain(
                "events", genesis, records[:-1], links[:-1], expected_head=builder.head
            )
        assert "sealed stream_head.head" in exc.value.reason

    def test_reordered_records_detected(self) -> None:
        genesis, records, links, _ = _make_chain()
        swapped = records[:]
        swapped[1], swapped[2] = swapped[2], swapped[1]
        with pytest.raises(ChainVerificationError) as exc:
            verify_chain("events", genesis, swapped, links)
        assert exc.value.seq == 1

    def test_genesis_binds_run_identity(self) -> None:
        rc = run_config_sha256({"seed": 1})
        g1 = chain_genesis(
            "events",
            run_id="run_" + "0" * 16,
            pack_content_hash=_PACK_HASH,
            agent_manifest_sha256=_AGENT_HASH,
            run_config_sha256=rc,
        )
        g2 = chain_genesis(
            "events",
            run_id="run_" + "1" * 16,
            pack_content_hash=_PACK_HASH,
            agent_manifest_sha256=_AGENT_HASH,
            run_config_sha256=rc,
        )
        g3 = chain_genesis(
            "decisions",
            run_id="run_" + "0" * 16,
            pack_content_hash=_PACK_HASH,
            agent_manifest_sha256=_AGENT_HASH,
            run_config_sha256=rc,
        )
        assert len({g1, g2, g3}) == 3  # different run or stream => different genesis

    def test_chain_transplant_rejected(self) -> None:
        # Links built under one genesis must not verify under another.
        genesis, records, links, _ = _make_chain()
        other = chain_genesis(
            "events",
            run_id="run_" + "f" * 16,
            pack_content_hash=_PACK_HASH,
            agent_manifest_sha256=_AGENT_HASH,
            run_config_sha256=run_config_sha256({"seed": 7}),
        )
        assert other != genesis
        with pytest.raises(ChainVerificationError) as exc:
            verify_chain("events", other, records, links)
        assert exc.value.seq == 0

    def test_malformed_hash_inputs_rejected(self) -> None:
        with pytest.raises(CanonicalizationError):
            chain_link_value("events", 0, "sha256:short", _PACK_HASH)
        with pytest.raises(CanonicalizationError):
            chain_genesis(
                "events",
                run_id="run_" + "0" * 16,
                pack_content_hash="11" * 32,  # missing prefix
                agent_manifest_sha256=_AGENT_HASH,
                run_config_sha256=run_config_sha256({}),
            )


class TestSealRoot:
    _SEAL: dict[str, object] = {
        "schema": "chain/v1",
        "run_id": "run_0123456789abcdef",
        "streams": {
            "events": {"genesis": _PACK_HASH, "head": _AGENT_HASH, "count": 2},
            "decisions": {"genesis": _PACK_HASH, "head": _AGENT_HASH, "count": 1},
        },
        "files": {"manifest.json": _PACK_HASH},
        "blobs": {"observations": {"count": 1}, "raw": {"count": 1}},
        "complete": True,
    }

    def test_root_excludes_root_member(self) -> None:
        root = seal_root(self._SEAL)
        with_root = dict(self._SEAL)
        with_root["root"] = root
        assert seal_root(with_root) == root  # idempotent under self-inclusion
        with_bogus = dict(self._SEAL)
        with_bogus["root"] = "sha256:" + "ab" * 32
        assert seal_root(with_bogus) == root  # existing root value never hashed

    def test_root_sensitive_to_content(self) -> None:
        changed = copy.deepcopy(self._SEAL)
        files = changed["files"]
        assert isinstance(files, dict)
        files["manifest.json"] = "sha256:" + "33" * 32
        assert seal_root(changed) != seal_root(self._SEAL)
