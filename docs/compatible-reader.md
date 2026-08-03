# Compatible Reader Guide

This guide is the shortest implementation path for an independent WAGMI Bench
v1 pack and evidence reader. It is intentionally procedural: a third party
should be able to reject unsafe input and verify stored evidence without
importing WAGMI Bench's Python modules.

The JSON Schemas are normative. The frozen IC documents explain semantics, and
the generated field reference exhaustively documents every field:

- [IC-1 Pack Format](contracts/IC-1.md)
- [IC-5 Evidence Bundle](contracts/IC-5.md)
- [Schema field reference](contracts/field-reference.md)
- [Schema versioning policy](contracts/VERSIONING.md)

## 1. Use a strict JSON and path layer

For every JSON or JSONL document:

- decode UTF-8 strictly;
- reject duplicate object keys;
- reject `NaN`, infinities, and fractional/exponent number lexemes;
- validate against the exact declared schema major;
- reject unknown schema majors;
- retain the original bytes for hashing.

For every manifest-relative path:

- require normalized relative POSIX syntax;
- reject empty, absolute, `.`, and `..` components;
- reject symlinks in every path component;
- resolve the final path and confirm it stays below the declared root.

Use RFC 8785 JSON Canonicalization Scheme (JCS) for constructed hash payloads.
Hashes are lowercase SHA-256 with the `sha256:` prefix unless a field says
otherwise.

## 2. Read a pack

1. Parse `manifest.json` as `pack_manifest/v1`.
2. Confirm `claim_label == "survival-stress"`.
3. For each `files[]` entry in path order:
   - resolve the path safely;
   - read the exact bytes;
   - compare `bytes`;
   - split JSONL lines without accepting blank lines and compare `records`;
   - compare SHA-256;
   - validate each line against the declared `row_schema`.
4. Require one trade, mark, and index series plus funding for every declared
   market.
5. Enforce the timestamp, `available_at`, interval, settlement, cap, and
   market-descriptor invariants in IC-1.
6. Recompute the content hash as JCS over:

```json
{
  "schema": "pack_content/v1",
  "files": [
    {
      "path": "bars_1h.jsonl",
      "sha256": "sha256:...",
      "bytes": 123,
      "records": 4
    }
  ]
}
```

The `files` projection is sorted by `path` and contains exactly those four
fields. Compare its SHA-256 with `manifest.content_hash`.

Do not infer price units. Multiply integer price ticks by the market
descriptor's `tick_size_micro` to obtain micro-quote units. Quantity, rate,
ratio, leverage, and money scales are named in their fields.

## 3. Inventory an evidence bundle

A complete v1 bundle contains:

```text
manifest.json
agent_manifest.json
observations/NNNN.json
raw/NNNN-aK.txt
events.jsonl
chain.jsonl
decisions/NNNN.json
ledger.jsonl
ledger_stress_2x.jsonl
metrics.json
chain.json
```

`redaction.json` is optional and valid only for a declared share-profile
sub-bundle. No other top-level material is allowed in a sealed bundle.

Parse `manifest.json` as `bundle_manifest/v1`. Validate every listed
`spec_versions` entry and compare the JCS SHA-256 of `agent_manifest.json` with
`agent_manifest_sha256`. Confirm the supplied pack manifest bytes and pack
content hash match the two recorded pack commitments.

## 4. Recompute chain genesis

For each stream name (`events`, `decisions`):

```text
run_config_sha256 = SHA256(JCS(manifest.run_config))

genesis = SHA256(JCS({
  "schema": "chain_genesis/v1",
  "stream": STREAM,
  "run_id": manifest.run_id,
  "pack_content_hash": manifest.pack.content_hash,
  "agent_manifest_sha256": manifest.agent_manifest_sha256,
  "run_config_sha256": run_config_sha256
}))
```

Read `chain.jsonl` in order. Every line is a `chain_link/v1`. For the
corresponding stored record bytes:

```text
record_sha256 = SHA256(RECORD_BYTES)

link_i = SHA256(JCS({
  "schema": "chain_link/v1",
  "stream": STREAM,
  "seq": i,
  "record_sha256": record_sha256,
  "prev": genesis if i == 0 else link_(i-1)
}))
```

Compare the constructed link with the stored link at every sequence. Event
record bytes are each `events.jsonl` line without its newline. Decision record
bytes are the complete contents of `decisions/NNNN.json`.

## 5. Verify completion and the seal

If `chain.json` is absent and all durable record/link pairs form a valid
prefix, report `TRUNCATED`, never complete. An unlinked record-first tail is
crash residue and is not part of the verified prefix.

If `chain.json` exists:

1. validate it as `chain/v1`;
2. compare each stream head and exact record count;
3. compare every declared non-chained file hash and blob count;
4. require the last event to be `EpisodeEnd`;
5. remove only the `root` member from the parsed seal, JCS-canonicalize the
   remainder, hash it, and compare the result with `root`.

Any mismatch is `CORRUPT` and should identify the first path, stream, and
sequence available. Hash the stored bytes; do not canonicalize a stored record
before hashing it.

## 6. Validate the audit projections

For each turn, require exactly one observation and decision record. Confirm:

- the `saw.observation_ref` resolves safely and its hash matches;
- every `said.attempts[].raw_ref` resolves and hashes, unless its absence is
  exactly declared by `redaction.json`;
- `event_seq_range` is contiguous and belongs to the same run and turn;
- the `meant`, `rules`, `happened`, `cost_to_hold`, and `account_after`
  projection agrees with the canonical event range.

For every ledger row:

```text
d_nav_micro
  == d_price_pnl_micro
   + d_funding_micro
   + d_fees_micro
   + d_liq_penalty_micro
```

Require primary and `stress_2x` metric objects to have identical key sets and
require `metrics.claim_label == "survival-stress"`.

## 7. Keep verification and replay separate

A compatible reader can verify stored evidence without implementing the
exchange engine. Economic replay is a stronger operation: it also requires the
exact `engine_version`, pack, schema set, and run configuration, then
regenerates both ledgers, metrics, and decision projections from the recorded
action/timing events.

Never advertise a schema-valid read as byte-identical replay. Never treat a
model reinvocation as evidence replay.

## Independent-reader acceptance

A reader is ready for adversarial review when it:

- accepts the committed complete synthetic bundle;
- reports a seal-less valid prefix as `TRUNCATED`;
- rejects one flipped byte in each record/file class;
- rejects a symlink and `../` path;
- rejects duplicate JSON keys and fractional JSON numbers;
- names the first bad stream/sequence;
- recomputes the same pack content hash and bundle root on two platforms;
- checks the stored ledger invariant without importing project code.
