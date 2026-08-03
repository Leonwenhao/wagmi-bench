# Replay and Verification

WAGMI Bench separates three operations:

1. **Run:** ask an agent for decisions and seal the observed trace.
2. **Verify:** inspect stored bytes, schemas, chains, and the final seal without
   contacting the agent.
3. **Replay:** feed the recorded decisions and timing outcomes through the
   exact compatible engine and compare the regenerated economic artifacts byte
   for byte.

Replay is economic reproducibility. It does not promise that an external model
will emit the same answer in a second live invocation.

## Verify a bundle

The `report`, `replay`, and `share` commands all require a `COMPLETE` verifier
result before producing an artifact. To inspect the verifier directly:

```sh
uv run python -c \
  'from recorder.verify import verify_bundle; r=verify_bundle("bundles/quickstart"); print(r.verdict, r.root, r.message)'
```

The result is one of:

| Verdict | Meaning | Allowed next step |
|---|---|---|
| `COMPLETE` | Final seal, schemas, file hashes, both record chains, record counts, decision projections, and `EpisodeEnd` agree. | Replay, report, or verifiable share-sub-bundle generation. |
| `TRUNCATED` | No final seal exists, but the durable record/link prefix is valid. | Preserve and inspect as crash evidence; do not style as a finished run. |
| `CORRUPT` | A layout, path, byte, schema, hash, link, count, projection, or seal check failed. | Preserve the original and investigate the named first failure. |

A verifier error names the relevant path and, when possible, the stream and
sequence. Do not “repair” an evidence bundle in place. Copy it for analysis and
retain the original bytes.

## Replay exactly

Use the exact pack referenced by the bundle:

```sh
uv run wagmibench replay \
  --bundle bundles/quickstart \
  --pack fixtures/golden-mini/pack
```

`uv run wagmibench …` requires the project to be installed into the
environment (`uv sync` does this). In an environment where the project is not
installed — for example a review flow using `uv run --locked --no-sync` — run
the module directly from the repository root instead:

```sh
PYTHONPATH=. uv run --locked --no-sync python -m cli.main replay \
  --bundle bundles/quickstart \
  --pack fixtures/golden-mini/pack
```

For a source-built catalog pack:

```sh
uv run wagmibench replay \
  --bundle bundles/covid-momentum \
  --pack covid-black-thursday
```

Success prints the run id, evidence root, compared files, and number of
decision records. Replay compares:

- `ledger.jsonl`;
- `ledger_stress_2x.jsonl`;
- `metrics.json`;
- every derived `decisions/NNNN.json` record.

The stored event stream is canonical. Decision records are regenerated from it
and compared, so the denormalized audit view cannot silently disagree with the
event trace.

## Compatibility refusal

Replay refuses instead of guessing when:

- the recomputed pack content hash differs from
  `manifest.pack.content_hash`;
- the installed engine version differs from `manifest.engine_version`;
- the installed schema registry differs from `manifest.spec_versions`;
- the bundle is not `COMPLETE`.

There is no best-effort override in v1. Follow the exact version named by the
error, use the exact local pack bytes, and leave the bundle unchanged. The
[schema versioning policy](contracts/VERSIONING.md) explains how future majors
coexist with immutable history.

## What the evidence root binds

The bundle manifest pins the run, pack, agent manifest, engine, schema versions,
run configuration, and time-rebase offset. Two linear hash chains bind every
event and decision record to that genesis. The final `chain.json` seal binds
the stream heads and counts, non-chained file hashes, blob counts, and one
publishable root.

Verification hashes the stored bytes. It does not parse and re-serialize a
record before hashing, so alternate whitespace or key order is still a byte
change and is detected.

The authoritative layout and formulas are in the frozen
[IC-5 Evidence Bundle contract](contracts/IC-5.md). For a clean independent
implementation sequence, use the [Compatible reader guide](compatible-reader.md).

## Redacted share bundles

Only verbatim raw model-response blobs may be removed under the v1 share
profile. A `redaction.json` manifest lists each removal, original hash, byte
count, reason, and parent root. Chained records, observations, ledgers,
verdicts, and fills cannot be removed.

Create one from a complete parent bundle:

```sh
uv run wagmibench share \
  --bundle bundles/quickstart \
  --output shares/quickstart
```

The command verifies both parent and result, prints the unchanged parent
evidence root and removal count, and leaves the parent untouched. The share
directory remains replayable with the exact pack. Use `wagmibench report` when
you need the static claim-labeled SVG card.

A declared raw-blob absence verifies with disclosure. An undeclared missing
blob is corruption. Redaction is for model text, not secrets: bundle schemas
are designed to be secret-free before redaction.

## Incident handling

If verification fails:

1. stop downstream report/share generation;
2. record the verifier verdict, path, stream, and sequence;
3. hash and preserve the original directory;
4. confirm the expected engine, schema, and pack versions;
5. reproduce against a copy only;
6. file a security report if tamper bypass, path escape, secret exposure, or
   verifier inconsistency is suspected.

See [SECURITY.md](../SECURITY.md) for private reporting guidance.
