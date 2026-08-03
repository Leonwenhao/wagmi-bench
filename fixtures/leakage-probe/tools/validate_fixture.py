#!/usr/bin/env python3
"""Validate the leakage-probe fixture against the IC-1 schemas + manifest self-consistency."""
import hashlib
import json
import pathlib
import sys

from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMAS = pathlib.Path(__file__).resolve().parents[3] / "spec" / "schemas"

# Pinned bytes for the two pack files NOT covered by content_hash (which spans only the
# 4 series files). sentinels.json in particular carries audit-hardened normative wording
# (the SECONDARY-TRIPWIRE rule, M0 audit); pinning its sha256 makes any drift between the
# committed file and the generator loud (ISO1-GEN-DRIFT, M0 audit round 3). If you change
# either file, change it in tools/gen_fixture.py, regenerate, and update these pins + the
# README hash table in the same commit.
PINNED_SHA256 = {
    "sentinels.json": "sha256:de3cd5b0eda9a9444d10194fdbc9c5dc3cf9be13ce6d9b3ff9da74185eafed2b",
    "manifest.json": "sha256:0c9f68e397ae4da2f1737a2d6fcb3249a007a263f1d09ae5e9c0bcf1bd5a8962",
}

def load(p):
    return json.loads(p.read_text())

pack_schema = load(SCHEMAS / "pack_manifest.v1.schema.json")
row_schemas = {
    "bar_row/v1": load(SCHEMAS / "bar_row.v1.schema.json"),
    "funding_row/v1": load(SCHEMAS / "funding_row.v1.schema.json"),
}

failures = []
def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" - {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)

# 1. manifest validates
manifest = load(ROOT / "manifest.json")
errs = list(Draft202012Validator(pack_schema).iter_errors(manifest))
check("manifest.json validates against pack_manifest/v1", not errs,
      "; ".join(e.message for e in errs[:5]))

# 2. every JSONL row validates against its declared row_schema; hashes/bytes/records match
def jcs(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

for f in manifest["files"]:
    p = ROOT / f["path"]
    data = p.read_bytes()
    check(f'{f["path"]}: bytes == {f["bytes"]}', len(data) == f["bytes"])
    check(f'{f["path"]}: sha256 matches manifest',
          "sha256:" + hashlib.sha256(data).hexdigest() == f["sha256"])
    lines = data.split(b"\n")
    check(f'{f["path"]}: newline-terminated', data.endswith(b"\n"))
    rows = [json.loads(l) for l in lines if l]
    check(f'{f["path"]}: records == {f["records"]}', len(rows) == f["records"])
    v = Draft202012Validator(row_schemas[f["row_schema"]])
    row_errs = [(i, e.message) for i, r in enumerate(rows) for e in v.iter_errors(r)]
    check(f'{f["path"]}: all {len(rows)} rows validate against {f["row_schema"]}',
          not row_errs, str(row_errs[:5]))
    check(f'{f["path"]}: every line is JCS-canonical', all(jcs(r) == l for r, l in zip(rows, [l for l in lines if l])))
    # no fractional numbers anywhere
    check(f'{f["path"]}: integers only', all(
        all(isinstance(val, (int, str)) for val in r.values()) for r in rows))

# 3. content_hash recomputes
entries = sorted(
    ({"path": f["path"], "sha256": f["sha256"], "bytes": f["bytes"], "records": f["records"]} for f in manifest["files"]),
    key=lambda e: e["path"],
)
recomputed = "sha256:" + hashlib.sha256(jcs({"schema": "pack_content/v1", "files": entries})).hexdigest()
check("content_hash recomputes from pack_content/v1 file list", recomputed == manifest["content_hash"])

# 4. poison rows agree with sentinels.json; non-poison rows obey available_at consistency
sent = load(ROOT / "sentinels.json")
poison_by_file = {}
for pr in sent["poison_rows"]:
    poison_by_file.setdefault(pr["file"], {})[pr["row_index"]] = pr

all_sentinels = set(sent["sentinel_values"])
seen_sentinels = set()
for f in manifest["files"]:
    rows = [json.loads(l) for l in (ROOT / f["path"]).read_bytes().split(b"\n") if l]
    for i, r in enumerate(rows):
        pr = poison_by_file.get(f["path"], {}).get(i)
        if pr:
            check(f'{f["path"]}[{i}]: poison available_at is AFTER window end',
                  r["available_at"] == sent["poison_available_at"] > manifest["window"]["end_ts"])
            for field, val in pr["sentinel_fields"].items():
                check(f'{f["path"]}[{i}].{field}: sentinel value matches registry', r.get(field) == val)
                seen_sentinels.add(val)
        else:
            if f["role"] == "funding":
                ok = r["available_at"] == r["ts"]
            else:
                ok = r["available_at"] == r["ts"] + f["interval_ms"]
            check(f'{f["path"]}[{i}]: clean-row available_at consistent', ok)
            check(f'{f["path"]}[{i}]: no sentinel values in clean row',
                  not (set(r.values()) & all_sentinels))
check("every registry sentinel_value appears in exactly the declared poison rows",
      seen_sentinels == all_sentinels)

# 5. pinned hashes for the files outside content_hash coverage (sentinels.json, manifest.json)
for name, pinned in sorted(PINNED_SHA256.items()):
    actual = "sha256:" + hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    check(f"{name}: sha256 matches pinned audit hash", actual == pinned,
          f"expected {pinned}, got {actual}")

# 6. maintenance = half initial at max-leverage tier (IC-1 data invariant)
for alias, m in manifest["markets"].items():
    tier = m["margin"]["tiers"][0]
    check(f"markets.{alias}: maintenance ~ half initial (ceil)",
          tier["maintenance_rate_1e8"] * 2 in (tier["initial_rate_1e8"], tier["initial_rate_1e8"] + 1)
          or tier["maintenance_rate_1e8"] == tier["initial_rate_1e8"] // 2 + (tier["initial_rate_1e8"] % 2))

print()
if failures:
    print(f"{len(failures)} FAILURES")
    sys.exit(1)
print("ALL CHECKS PASSED")
