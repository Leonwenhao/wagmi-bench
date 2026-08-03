# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from harness.protocol import HarnessEvent, HarnessEventSource
from sandbox.egress_probe import (
    ALLOWED_FIREWORKS_DOMAIN,
    PROBE_SCHEMA,
    PROBE_SCRIPT,
    DockerEgressProbeRunner,
    EgressProbeError,
    expected_blocks,
    parse_probe_receipt,
    verify_egress_proof,
)
from sandbox.gateway import destination_token
from sandbox.orchestration import IsolationPlan

IMAGE = "sha256:" + ("a" * 64)


def _receipt(*, allowed: str = "reachable") -> str:
    outcomes = (
        allowed,
        "refused",
        "refused",
        "attempted",
        "attempted",
        "attempted",
        "refused",
    )
    ids = (
        "allowed_fireworks_https",
        "blocked_hostname_https",
        "blocked_redirect_https",
        "dns_exfil_udp",
        "raw_ipv4_tcp",
        "raw_udp",
        "websocket_connect",
    )
    return json.dumps(
        {
            "schema": PROBE_SCHEMA,
            "cases": [
                {"id": case_id, "outcome": outcome}
                for case_id, outcome in zip(ids, outcomes, strict=True)
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _events() -> tuple[HarnessEvent, ...]:
    return tuple(
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


def _plan(tmp_path: Path) -> IsolationPlan:
    return IsolationPlan(
        repo_root=tmp_path,
        agent_image=IMAGE,
        gateway_image=IMAGE,
        guard_image=IMAGE,
        endpoint_domains=(ALLOWED_FIREWORKS_DOMAIN,),
        agent_command=("python3", "-m", "agents.server"),
        agent_env={
            "TRADEVOLVE_AGENT_MODE": "llm",
            "TRADEVOLVE_LLM_MODEL": "accounts/example/models/reference",
        },
    )


def test_probe_script_is_destination_fixed_and_secret_free() -> None:
    compile(PROBE_SCRIPT, "<egress-probe>", "exec")
    assert "https://api.fireworks.ai/" in PROBE_SCRIPT
    assert "https://data.binance.vision/" in PROBE_SCRIPT
    assert "https://redirect-proof.example/" in PROBE_SCRIPT
    assert "7365637265742d70726f6265.iso2-proof.invalid" in PROBE_SCRIPT
    assert "198.51.100.10" in PROBE_SCRIPT
    assert "198.51.100.11" in PROBE_SCRIPT
    assert "websocket-proof.example" in PROBE_SCRIPT
    for forbidden in (
        "FIREWORKS_API_KEY",
        "Authorization",
        "open('.env",
        'open(".env',
        "pack_manifest",
        "scenario",
    ):
        assert forbidden not in PROBE_SCRIPT


def test_receipt_and_events_form_complete_secret_safe_proof() -> None:
    receipt = parse_probe_receipt(_receipt())
    proof = verify_egress_proof(receipt, _events())

    assert len(proof.receipt.cases) == 7
    assert len(proof.matched_events) == 6
    encoded = repr(proof)
    assert "data.binance.vision" not in encoded
    assert "redirect-proof.example" not in encoded
    assert "7365637265742d70726f6265.iso2-proof.invalid" not in encoded
    assert "198.51.100.10" not in encoded


def test_receipt_rejects_raw_destination_or_extra_output() -> None:
    value = json.loads(_receipt())
    value["destination"] = "secret.exfil.example"
    with pytest.raises(EgressProbeError, match="unexpected fields"):
        parse_probe_receipt(json.dumps(value))
    with pytest.raises(EgressProbeError, match="multiline"):
        parse_probe_receipt(_receipt() + "\ntraceback")
    with pytest.raises(EgressProbeError, match="multiline"):
        parse_probe_receipt(_receipt() + "\n\n")


def test_proof_refuses_unreachable_allowed_endpoint_or_missing_block() -> None:
    with pytest.raises(EgressProbeError, match="not reachable"):
        verify_egress_proof(parse_probe_receipt(_receipt(allowed="unreachable")), _events())
    with pytest.raises(EgressProbeError, match="raw_udp"):
        verify_egress_proof(parse_probe_receipt(_receipt()), _events()[:-2] + _events()[-1:])


def test_proof_refuses_block_event_for_allowed_fireworks() -> None:
    allowed_block = HarnessEvent(
        type="EgressBlocked",
        payload={
            "destination": destination_token(
                ALLOWED_FIREWORKS_DOMAIN,
                kind="domain",
            ),
            "port": 443,
            "protocol": "https",
            "count": 1,
        },
    )
    with pytest.raises(EgressProbeError, match="allowed Fireworks"):
        verify_egress_proof(
            parse_probe_receipt(_receipt()),
            _events() + (allowed_block,),
        )


def test_proof_rejects_unexpected_duplicate_or_coalesced_events() -> None:
    extra = HarnessEvent(
        type="EgressBlocked",
        payload={
            "destination": destination_token("extra.example", kind="domain"),
            "port": 443,
            "protocol": "https",
            "count": 1,
        },
    )
    with pytest.raises(EgressProbeError, match="unexpected"):
        verify_egress_proof(parse_probe_receipt(_receipt()), _events() + (extra,))
    with pytest.raises(EgressProbeError, match="duplicate"):
        verify_egress_proof(
            parse_probe_receipt(_receipt()),
            _events() + (_events()[0],),
        )
    events = list(_events())
    events[0] = HarnessEvent(
        type="EgressBlocked",
        payload={**events[0].payload, "count": 2},
    )
    with pytest.raises(EgressProbeError, match="non-unit"):
        verify_egress_proof(parse_probe_receipt(_receipt()), events)


def test_runner_baselines_old_events_and_uses_stdin_not_argv(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], bytes]] = []

    class Events:
        def __init__(self) -> None:
            self.drains = 0

        def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
            self.drains += 1
            return _events() if self.drains == 2 else ()

    def run_input(command: tuple[str, ...], program: bytes) -> str:
        calls.append((command, program))
        return _receipt()

    event_source = Events()
    runner = DockerEgressProbeRunner(
        plan=_plan(tmp_path),
        event_source=cast(HarnessEventSource, event_source),
        run_input=run_input,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    proof = runner.run()

    assert len(proof.matched_events) == 6
    assert event_source.drains == 2
    assert len(calls) == 1
    command, program = calls[0]
    assert command[-2:] == ("python3", "-")
    assert "data.binance.vision" not in repr(command)
    assert program == PROBE_SCRIPT.encode("ascii")
