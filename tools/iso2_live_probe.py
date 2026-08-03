# SPDX-License-Identifier: Apache-2.0
"""Run the fixed ISO-2 attack matrix inside the real Docker sandbox.

This release-gate tool is intentionally keyless. It proves TLS reachability to
the exact Fireworks hostname but never calls an inference endpoint and never
loads ``.env``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from sandbox.egress_probe import (
    ALLOWED_FIREWORKS_DOMAIN,
    DockerEgressProbeRunner,
    EgressProbeError,
)
from sandbox.orchestration import (
    DockerSandbox,
    IsolationPlan,
    PreflightFailure,
    RuntimeSnapshot,
)


@dataclass(frozen=True, slots=True)
class LiveProbeConfig:
    repo_root: Path
    agent_image: str
    gateway_image: str
    guard_image: str
    host_port: int
    subnet: str
    gateway_ipv4: str


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _runtime_receipt(snapshot: RuntimeSnapshot) -> dict[str, object]:
    return {
        "server_os": snapshot.server_os,
        "security_options": list(snapshot.security_options),
        "agent": {
            "image_id": snapshot.agent.image_id,
            "user": snapshot.agent.user,
            "read_only": snapshot.agent.read_only,
            "privileged": snapshot.agent.privileged,
            "cap_drop": list(snapshot.agent.cap_drop),
            "cap_add": list(snapshot.agent.cap_add),
            "mount_types": list(snapshot.agent.mount_types),
            "tmpfs_paths": list(snapshot.agent.tmpfs_paths),
            "log_driver": snapshot.agent.log_driver,
        },
        "guard": {
            "image_id": snapshot.guard.image_id,
            "user": snapshot.guard.user,
            "read_only": snapshot.guard.read_only,
            "privileged": snapshot.guard.privileged,
            "cap_eff_hex": snapshot.guard_ready.get("cap_eff_hex"),
            "euid": snapshot.guard_ready.get("euid"),
            "ipv6_disabled": snapshot.guard_ready.get("ipv6_disabled"),
        },
        "gateway": {
            "image_id": snapshot.gateway.image_id,
            "user": snapshot.gateway.user,
            "read_only": snapshot.gateway.read_only,
            "privileged": snapshot.gateway.privileged,
            "cap_drop": list(snapshot.gateway.cap_drop),
            "cap_add": list(snapshot.gateway.cap_add),
        },
        "internal_network_is_internal": snapshot.internal_network_is_internal,
        "internal_network_ipv6": snapshot.internal_network_ipv6,
    }


def run_live_probe(config: LiveProbeConfig) -> dict[str, object]:
    sandbox_id = secrets.token_hex(16)
    suffix = sandbox_id[:10]
    plan = IsolationPlan(
        repo_root=config.repo_root,
        agent_image=config.agent_image,
        gateway_image=config.gateway_image,
        guard_image=config.guard_image,
        endpoint_domains=(ALLOWED_FIREWORKS_DOMAIN,),
        agent_command=("python3", "-m", "agents.server"),
        # Reachability testing is keyless. An empty credential allowlist also
        # guarantees DockerSandbox never reads the repository's .env.
        credential_env_names=(),
        sandbox_id=sandbox_id,
        agent_name=f"te-iso2-agent-{suffix}",
        gateway_name=f"te-iso2-gateway-{suffix}",
        guard_name=f"te-iso2-guard-{suffix}",
        internal_network=f"te-iso2-internal-{suffix}",
        egress_network=f"te-iso2-egress-{suffix}",
        subnet=config.subnet,
        gateway_ipv4=config.gateway_ipv4,
        host_http_port=config.host_port,
    )
    snapshot: RuntimeSnapshot
    proof_cases: list[dict[str, str]]
    matched_events: list[dict[str, object]]
    with DockerSandbox(plan) as handle:
        snapshot = handle.snapshot
        proof = DockerEgressProbeRunner(
            plan=plan,
            event_source=handle.event_source,
        ).run()
        proof_cases = [
            {"id": case.case_id, "outcome": case.outcome}
            for case in proof.receipt.cases
        ]
        matched_events = [
            {"type": event.type, "payload": dict(event.payload)}
            for event in proof.matched_events
        ]

    # This object is emitted only after context-manager teardown succeeds.
    return {
        "schema": "tradeevolve_iso2_live/v1",
        "verdict": "PASS",
        "keyless": True,
        "inference_requests": 0,
        "cleanup": "complete",
        "cases": proof_cases,
        "matched_events": matched_events,
        "runtime": _runtime_receipt(snapshot),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the keyless ISO-2 attack matrix inside the real Docker "
            "sandbox and emit a secret-safe JSON receipt."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--agent-image", required=True)
    parser.add_argument("--gateway-image", required=True)
    parser.add_argument("--guard-image", required=True)
    parser.add_argument(
        "--host-port",
        type=int,
        default=None,
        help="loopback port for the temporary agent API (default: free port)",
    )
    parser.add_argument("--subnet", default="172.30.240.0/24")
    parser.add_argument("--gateway-ipv4", default="172.30.240.2")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    host_port = (
        _available_loopback_port()
        if args.host_port is None
        else args.host_port
    )
    try:
        receipt = run_live_probe(
            LiveProbeConfig(
                repo_root=args.repo_root.resolve(strict=True),
                agent_image=args.agent_image,
                gateway_image=args.gateway_image,
                guard_image=args.guard_image,
                host_port=host_port,
                subnet=args.subnet,
                gateway_ipv4=args.gateway_ipv4,
            )
        )
    except (EgressProbeError, PreflightFailure, OSError, ValueError) as exc:
        print(f"ISO-2 live probe failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            receipt,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
