# Security Policy

WAGMI Bench is currently a local build candidate; no public release is claimed.
The active 0.1 development line receives security review, but it is not a
promise of production support.

## Report privately

Do not open a public issue for a vulnerability that could expose credentials,
escape the agent sandbox, bypass egress controls, reveal future/scenario data,
forge or alter evidence, traverse bundle paths, or evade redaction.

When a repository security-advisory channel is configured, use its private
vulnerability-reporting form. Until then, contact the maintainer through an
already-established private channel. Do not send live credentials, raw market
data, or a weaponized proof that targets systems you do not own.

Include:

- affected commit and platform;
- the violated security or evidence invariant;
- a minimal synthetic reproduction;
- whether a secret, external service, or third-party system was involved;
- the first failing verifier path, stream, or sequence when applicable;
- a safe remediation suggestion, if known.

## If a credential may be exposed

Rotate or revoke it immediately. Do not wait for reproduction or triage.
Preserve only secret-free hashes and logs. Never paste the credential into an
issue, chat, command, screenshot, bundle, report, or commit.

## Security boundaries

The V1 security boundary includes:

- point-in-time observation isolation and scenario opacity;
- deny-by-default agent egress;
- non-root, read-only container execution with dropped capabilities;
- strict URL, hostname, path, and JSON parsing;
- secret-free bundle schemas;
- hash chains, seals, and three-valued bundle verification;
- raw-response redaction without altering chained evidence.

The historical simulator is not a custody or execution system. It does not
protect exchange accounts or live trading funds because it does not connect to
them.
