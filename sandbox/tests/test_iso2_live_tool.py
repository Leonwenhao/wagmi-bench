# SPDX-License-Identifier: Apache-2.0
"""Unit boundary for the live ISO-2 release-gate tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from harness.protocol import HarnessEvent
from sandbox.egress_probe import (
    EgressProof,
    ProbeCaseResult,
    ProbeReceipt,
    expected_blocks,
)
from sandbox.orchestration import (
    ContainerSnapshot,
    RuntimeSnapshot,
    SandboxHandle,
)
from tools import iso2_live_probe

IMAGE = "sha256:" + "a" * 64


def _container(
    name: str,
    *,
    user: str,
    log_driver: str,
) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=name,
        container_id="b" * 64,
        image_id=IMAGE,
        running=True,
        health_status="healthy",
        user=user,
        read_only=True,
        privileged=False,
        cap_drop=("ALL",),
        cap_add=(),
        security_opt=("no-new-privileges:true",),
        network_mode="test",
        dns=(),
        sysctls=(),
        mount_types=(),
        tmpfs_paths=("/tmp",),
        port_bindings=(),
        networks=(),
        network_ipv4=(),
        env_names=(),
        sandbox_id_label="c" * 32,
        log_driver=log_driver,
    )


def _snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        server_os="linux",
        security_options=("name=seccomp,profile=default",),
        agent=_container("agent", user="65532:65532", log_driver="none"),
        guard=_container("guard", user="65533:65533", log_driver="local"),
        gateway=_container("gateway", user="65532:65532", log_driver="local"),
        internal_network_is_internal=True,
        internal_network_ipv6=False,
        internal_network_label="c" * 32,
        egress_network_is_internal=False,
        egress_network_label="c" * 32,
        guard_ready={
            "cap_eff_hex": "0000000000000000",
            "euid": 65533,
            "ipv6_disabled": True,
        },
        firewall_check={},
        gateway_check={},
    )


def test_live_tool_emits_receipt_only_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    snapshot = _snapshot()

    class FakeSandbox:
        def __init__(self, plan: object) -> None:
            del plan

        def __enter__(self) -> SandboxHandle:
            lifecycle.append("enter")
            return SandboxHandle(
                base_url="http://127.0.0.1:18080",
                http_agent=object(),  # type: ignore[arg-type]
                event_source=object(),  # type: ignore[arg-type]
                snapshot=snapshot,
            )

        def __exit__(self, *args: object) -> None:
            del args
            lifecycle.append("cleanup")

    class FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def run(self) -> EgressProof:
            lifecycle.append("probe")
            cases = (
                ProbeCaseResult("allowed_fireworks_https", "reachable"),
                *(
                    ProbeCaseResult(expectation.case_id, "refused")
                    for expectation in expected_blocks()
                ),
            )
            events = tuple(
                HarnessEvent(
                    type="EgressBlocked",
                    payload={
                        "destination": expectation.destination,
                        "port": expectation.port,
                        "protocol": expectation.protocol,
                        "count": 1,
                    },
                )
                for expectation in expected_blocks()
            )
            return EgressProof(
                receipt=ProbeReceipt(cases=cases),
                matched_events=events,
            )

    monkeypatch.setattr(iso2_live_probe, "DockerSandbox", FakeSandbox)
    monkeypatch.setattr(
        iso2_live_probe,
        "DockerEgressProbeRunner",
        FakeRunner,
    )
    receipt = iso2_live_probe.run_live_probe(
        iso2_live_probe.LiveProbeConfig(
            repo_root=tmp_path,
            agent_image=IMAGE,
            gateway_image=IMAGE,
            guard_image=IMAGE,
            host_port=18080,
            subnet="172.30.240.0/24",
            gateway_ipv4="172.30.240.2",
        )
    )

    assert lifecycle == ["enter", "probe", "cleanup"]
    assert receipt["verdict"] == "PASS"
    assert receipt["cleanup"] == "complete"
    assert receipt["keyless"] is True
    assert receipt["inference_requests"] == 0
    assert len(cast(list[object], receipt["matched_events"])) == 6
    json.dumps(receipt, allow_nan=False)


def test_live_tool_refuses_unpinned_images(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pinned by sha256 digest"):
        iso2_live_probe.run_live_probe(
            iso2_live_probe.LiveProbeConfig(
                repo_root=tmp_path,
                agent_image="tradeevolve-agent:latest",
                gateway_image=IMAGE,
                guard_image=IMAGE,
                host_port=18080,
                subnet="172.30.240.0/24",
                gateway_ipv4="172.30.240.2",
            )
        )
