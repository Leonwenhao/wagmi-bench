# Contributing to WAGMI Bench

WAGMI Bench is an evidence product: a change is complete when its behavior is
specified, tested, and independently verifiable. Please keep pull requests
narrow and attach the receipt that demonstrates the requested property.

The project is currently a local build candidate, not a public release. These
instructions define the intended contribution discipline without claiming an
active contributor community.

## Set up

Use Python 3.12 or newer and `uv`:

```sh
uv sync --group dev
uv run pytest -q
uv run mypy
uv run python -m data.distribution_guard --repo-root .
```

Before requesting review, also build and scan the distributable artifacts:

```sh
uv run python -m build --no-isolation --outdir dist
uv run python -m data.distribution_guard \
  --repo-root . \
  --artifact-dir dist \
  --skip-tracked
```

Do not commit generated `dist/`, bundles, reports, raw archives, or built market
series.

## Choose the right change path

1. Search existing issues.
2. Use the closest issue template and state the smallest reproducible case.
3. For a bug, add a failing test before the fix where practical.
4. For a new public field or wire behavior, stop and follow the frozen-contract
   process below.
5. Keep unrelated formatting and refactors out of the patch.

## Frozen interfaces

The v1 contracts in `docs/contracts/` and schemas in `spec/schemas/` are frozen.
Do not edit them in an ordinary bug fix. A compatibility change requires the
process in [Schema Versioning & Evolution Policy](docs/contracts/VERSIONING.md):

- a new schema version where required;
- migration notes;
- a written component-by-component impact assessment;
- updated examples and tests;
- founder approval before changing a frozen v1 contract.

Golden fixtures are economic oracles. Never update expected bytes merely to
make a test pass. Explain the intended semantic change and obtain the required
review first.

## Determinism rules

- Money, price, rate, and ratio paths use fixed-point integers, never binary
  floating point.
- Hashed JSON uses JCS (RFC 8785); duplicate keys, non-finite values, and
  fractional JSON numbers are rejected.
- Engine economics must not read wall time, locale, timezone, or unseeded
  randomness.
- Evidence directories are immutable. Tests must write to a fresh temporary
  path rather than overwrite a bundle.
- A replay-affecting change needs byte-comparison tests and an explicit
  compatibility assessment.

## Claim and data rules

All historical scenario artifacts use the `survival-stress` claim label.
Describe findings in terms of survival, drawdown, liquidation distance,
funding drag, turnover, and rule violations. Do not present a historical
scenario result as evidence of future performance.

Never commit or attach upstream market data. The repository may contain pack
manifests, exact source recipes, sibling checksums, and approved synthetic
fixtures only. Run the distribution guard before review.

References to a venue or provider must be factual source/mechanics statements,
not affiliation, sponsorship, or endorsement claims.

## Security and secrets

Do not place API keys or credentials in code, fixtures, issue bodies, logs,
commands, screenshots, bundles, reports, action comments, URLs, or manifests.
If a secret may have been exposed, rotate it before doing anything else and
follow [SECURITY.md](SECURITY.md).

Changes to sandboxing, egress, path validation, redaction, verification, or
canonicalization need adversarial tests and a separate reviewer.

## Pull-request receipt

Include:

- the review-spec IDs affected;
- commands run and concise results;
- fixture, bundle, or content hashes when the change concerns deterministic
  output;
- platform details for platform-sensitive work;
- explicit confirmation that no raw market data, secrets, or generated evidence
  were added;
- any remaining limitation or waiver request.

By contributing, you agree that your contribution is submitted under the
[Apache License 2.0](LICENSE).
