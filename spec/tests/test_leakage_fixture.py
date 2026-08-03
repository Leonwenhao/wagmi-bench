# SPDX-License-Identifier: Apache-2.0
"""CI gate for the leakage-probe fixture (ISO-1, C0.4).

Closes M0 audit round-3 finding ISO1-GEN-DRIFT: the generator must be a faithful
source-of-truth for EVERY committed pack file — including sentinels.json, whose
audit-hardened `rule` wording once drifted from the generator without any test
going red (content_hash covers only the 4 series files). Two locks:

1. Regeneration byte-identity: tools/gen_fixture.py, run into a temp dir, must
   reproduce all six committed files byte-for-byte (DET-5 style).
2. Pin agreement: tools/validate_fixture.py must exit 0 against the committed
   fixture; it internally re-checks per-file sha256/bytes against the manifest
   AND pins sentinels.json + manifest.json (the files outside content_hash
   coverage) to recorded audit hashes.
"""
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "leakage-probe"
TOOLS = FIXTURE / "tools"

PACK_FILES = [
    "bars_4h.jsonl",
    "funding.jsonl",
    "index_4h.jsonl",
    "manifest.json",
    "mark_4h.jsonl",
    "sentinels.json",
]


def test_generator_reproduces_committed_pack_byte_identical(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "regen"
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "gen_fixture.py"), str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"gen_fixture.py failed:\n{proc.stdout}\n{proc.stderr}"
    for name in PACK_FILES:
        regenerated = (out / name).read_bytes()
        committed = (FIXTURE / name).read_bytes()
        assert regenerated == committed, (
            f"{name}: regeneration is NOT byte-identical to the committed file. "
            "The generator and the committed fixture have drifted (ISO1-GEN-DRIFT). "
            "Fix tools/gen_fixture.py (the source of truth), regenerate, and update "
            "the pinned hashes in tools/validate_fixture.py + the README hash table."
        )


def test_validator_passes_on_committed_pack() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "validate_fixture.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"validate_fixture.py failed (includes the sentinels.json/manifest.json "
        f"sha256 pins):\n{proc.stdout}\n{proc.stderr}"
    )
    assert "ALL CHECKS PASSED" in proc.stdout
