# SPDX-License-Identifier: Apache-2.0
"""Fail-closed Docker isolation primitives for HTTP agents.

The engine, recorder, and market packs intentionally do not live in this
package.  ``sandbox`` owns only the untrusted-agent boundary and emits
secret-safe egress-block facts for the trusted harness to sequence as frozen
``EgressBlocked`` events.
"""

from sandbox.egress_probe import (
    DockerEgressProbeRunner,
    EgressProbeError,
    EgressProof,
)
from sandbox.orchestration import (
    DockerContext,
    DockerEgressEventSource,
    DockerSandbox,
    IsolationPlan,
    PreflightFailure,
    RuntimeSnapshot,
    SandboxHandle,
    evaluate_runtime_snapshot,
)

__all__ = [
    "DockerContext",
    "DockerEgressEventSource",
    "DockerEgressProbeRunner",
    "DockerSandbox",
    "IsolationPlan",
    "EgressProbeError",
    "EgressProof",
    "PreflightFailure",
    "RuntimeSnapshot",
    "SandboxHandle",
    "evaluate_runtime_snapshot",
]
