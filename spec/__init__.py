# SPDX-License-Identifier: Apache-2.0
"""WAGMI Bench spec package: schemas and canonical serialization (C0).

The normative interface contracts live in ``docs/contracts/`` and the JSON
Schemas in ``spec/schemas/``. This package provides the runtime library that
implements the canonicalization and hash-chain rules those contracts pin down.
"""

from spec.canonical import (
    CanonicalizationError,
    ChainBuilder,
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

__all__ = [
    "CanonicalizationError",
    "ChainBuilder",
    "ChainVerificationError",
    "FloatNotAllowedError",
    "canonical_bytes",
    "chain_genesis",
    "chain_link_value",
    "content_hash",
    "run_config_sha256",
    "seal_root",
    "sha256_prefixed",
    "verify_chain",
]
