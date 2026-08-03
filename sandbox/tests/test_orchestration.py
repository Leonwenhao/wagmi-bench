# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from harness.http import HTTPAgent
from harness.protocol import AgentReply, HarnessEvent, HarnessEventSource
from sandbox.orchestration import (
    GATEWAY_CONTEXT,
    GUARD_CONTEXT,
    ContainerSnapshot,
    DockerEgressEventSource,
    DockerSandbox,
    IsolationPlan,
    PreflightFailure,
    RuntimeSnapshot,
    SandboxHandle,
    _agent_secret_environment,
    evaluate_runtime_snapshot,
)

IMAGE_A = "sha256:" + ("a" * 64)
IMAGE_B = "sha256:" + ("b" * 64)
IMAGE_C = "sha256:" + ("c" * 64)


def _repo(tmp_path: Path, *, env: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    if env:
        (tmp_path / ".env").write_text(
            "FIREWORKS_API_KEY=test-only\n",
            encoding="utf-8",
        )
    return tmp_path


def _plan(tmp_path: Path) -> IsolationPlan:
    repo = tmp_path / "repo"
    return IsolationPlan(
        repo_root=_repo(repo),
        agent_image=IMAGE_A,
        gateway_image=IMAGE_B,
        guard_image=IMAGE_C,
        endpoint_domains=("api.fireworks.ai",),
        agent_command=("python3", "-m", "agent"),
        agent_env={
            "TRADEVOLVE_AGENT_MODE": "llm",
            "TRADEVOLVE_LLM_MODEL": "accounts/example/models/reference",
            "TRADEVOLVE_LLM_BASE_URL": "https://api.fireworks.ai/inference/v1",
            "TRADEVOLVE_LLM_MAX_TOKENS": "128",
        },
    )


def _container(
    *,
    name: str,
    image: str,
    user: str,
    read_only: bool = True,
    privileged: bool = False,
    cap_drop: tuple[str, ...] = ("ALL",),
    cap_add: tuple[str, ...] = (),
    network_mode: str = "",
    dns: tuple[str, ...] = (),
    sysctls: tuple[tuple[str, str], ...] = (),
    tmpfs: tuple[str, ...] = (),
    ports: tuple[tuple[str, str, str], ...] = (),
    networks: tuple[str, ...] = (),
) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=name,
        container_id=(name[0] * 64),
        image_id=image,
        running=True,
        health_status="healthy" if name == "tradeevolve-guard" else None,
        user=user,
        read_only=read_only,
        privileged=privileged,
        cap_drop=cap_drop,
        cap_add=cap_add,
        security_opt=("no-new-privileges:true",),
        network_mode=network_mode,
        dns=dns,
        sysctls=sysctls,
        mount_types=(),
        tmpfs_paths=tmpfs,
        port_bindings=ports,
        networks=networks,
        network_ipv4=tuple(
            (network, "172.30.240.2" if "gateway" in name and "agent-net" in network else "")
            for network in networks
        ),
        env_names=(),
        sandbox_id_label="placeholder",
        log_driver="local",
    )


def _snapshot(plan: IsolationPlan) -> RuntimeSnapshot:
    guard = _container(
        name=plan.guard_name,
        image=IMAGE_C,
        user="0:0",
        cap_add=("CAP_NET_ADMIN", "CAP_SETGID", "CAP_SETUID"),
        network_mode=plan.egress_network,
        dns=("127.0.0.1",),
        sysctls=(
            ("net.ipv6.conf.all.disable_ipv6", "1"),
            ("net.ipv6.conf.default.disable_ipv6", "1"),
        ),
        tmpfs=("/run/tradeevolve",),
        ports=(
            (
                f"{plan.agent_http_port}/tcp",
                "127.0.0.1",
                str(plan.host_http_port),
            ),
        ),
        networks=(plan.internal_network, plan.egress_network),
    )
    return RuntimeSnapshot(
        server_os="linux",
        security_options=("name=seccomp,profile=builtin",),
        agent=replace(
            _container(
            name=plan.agent_name,
            image=IMAGE_A,
            user="65532:65532",
            network_mode=f"container:{guard.container_id}",
            tmpfs=("/tmp",),
            ),
            sandbox_id_label=plan.sandbox_id,
            log_driver="none",
        ),
        guard=replace(guard, sandbox_id_label=plan.sandbox_id),
        gateway=replace(
            _container(
            name=plan.gateway_name,
            image=IMAGE_B,
            user="65532:65532",
            network_mode=plan.internal_network,
            tmpfs=("/tmp",),
            networks=(plan.internal_network, plan.egress_network),
            ),
            sandbox_id_label=plan.sandbox_id,
        ),
        internal_network_is_internal=True,
        internal_network_ipv6=False,
        internal_network_label=plan.sandbox_id,
        egress_network_is_internal=False,
        egress_network_label=plan.sandbox_id,
        guard_ready={
            "schema": "sandbox_guard_ready/v1",
            "ready": True,
            "firewall_sha256": plan.firewall_plan.sha256(),
            "euid": 65533,
            "cap_eff_hex": "0000000000000000",
            "ipv6_disabled": True,
        },
        firewall_check={
            "schema": "sandbox_firewall_check/v1",
            "ok": True,
            "firewall_sha256": plan.firewall_plan.sha256(),
        },
        gateway_check={
            "schema": "sandbox_gateway_check/v1",
            "ok": True,
            "gateway_ipv4": plan.gateway_ipv4,
            "gateway_port": plan.gateway_port,
        },
    )


def test_build_contexts_are_minimal_deny_by_default() -> None:
    for context in (GATEWAY_CONTEXT, GUARD_CONTEXT):
        transmitted = context.audit()
        assert ".env" not in transmitted
        assert not any("pack" in path or "repo" in path for path in transmitted)
        dockerfile = (context.root / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY ." not in dockerfile
        assert "ADD " not in dockerfile


def test_build_command_requires_pinned_cached_base_and_no_network() -> None:
    command = GATEWAY_CONTEXT.build_command(
        base_argument="PYTHON_BASE",
        base_image="python@sha256:" + ("d" * 64),
        output_tag="tradeevolve/gateway:local",
    )
    assert "--network=none" in command
    assert "--pull=false" in command
    with pytest.raises(ValueError, match="pinned"):
        GATEWAY_CONTEXT.build_command(
            base_argument="PYTHON_BASE",
            base_image="python:latest",
            output_tag="tradeevolve/gateway:local",
        )


def test_creation_plan_never_mounts_repo_pack_or_env_into_builders(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    commands = plan.create_commands()
    guard_command = commands[4]
    guard_connect_command = commands[5]
    agent_command = commands[6]

    assert "--cap-drop" in agent_command and "ALL" in agent_command
    assert "--read-only" in agent_command
    assert "no-new-privileges:true" in agent_command
    assert f"container:{plan.guard_name}" in agent_command
    assert "--env-file" not in agent_command
    assert str(plan.env_file) not in agent_command
    assert "FIREWORKS_API_KEY" in agent_command
    assert "test-only" not in agent_command
    assert "DAYTONA_API_KEY" not in agent_command
    assert f"TRADEVOLVE_AGENT_PORT={plan.agent_http_port}" in agent_command
    assert "TRADEVOLVE_AGENT_MODE=llm" in agent_command
    assert (
        "TRADEVOLVE_LLM_MODEL=accounts/example/models/reference"
        in agent_command
    )
    assert (
        "TRADEVOLVE_LLM_BASE_URL=https://api.fireworks.ai/inference/v1"
        in agent_command
    )
    assert "TRADEVOLVE_LLM_MAX_TOKENS=128" in agent_command
    assert "--cap-add" in guard_command
    assert {"NET_ADMIN", "SETGID", "SETUID"} <= set(guard_command)
    assert "net.ipv6.conf.all.disable_ipv6=1" in guard_command
    assert any("mode=0755" in argument for argument in guard_command)
    assert "127.0.0.1" in guard_command
    assert f"AGENT_HTTP_PORT={plan.agent_http_port}" in guard_command
    assert guard_connect_command == (
        "docker",
        "network",
        "connect",
        plan.internal_network,
        plan.guard_name,
    )
    for command in (commands[2], guard_command):
        assert "max-file=1" in command
        assert "compress=false" in command
    for command in commands[:4]:
        assert "--env-file" not in command
        assert str(plan.repo_root) not in command
    for command in commands:
        assert "--volume" not in command
        assert "--mount" not in command
        assert not any("packs/" in argument for argument in command)


def test_secret_source_must_be_plain_and_gitignored(tmp_path: Path) -> None:
    repo = tmp_path / "missing-repo"
    missing = IsolationPlan(
        repo_root=_repo(repo, env=False),
        agent_image=IMAGE_A,
        gateway_image=IMAGE_B,
        guard_image=IMAGE_C,
        endpoint_domains=(),
        agent_command=("agent",),
    )
    with pytest.raises(PreflightFailure):
        missing.require_secret_source()


def test_agent_process_env_excludes_irrelevant_repo_secrets(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan.env_file.write_text(
        "FIREWORKS_API_KEY=model-secret\n"
        "DAYTONA_API_KEY=sandbox-secret\n"
        "UNRELATED_TOKEN=other-secret\n",
        encoding="utf-8",
    )
    selected = _agent_secret_environment(plan)
    assert selected == {"FIREWORKS_API_KEY": "model-secret"}
    assert "DAYTONA_API_KEY" not in selected
    assert "UNRELATED_TOKEN" not in selected
    assert "model-secret" not in repr(plan.create_commands())


def test_reckless_public_env_injects_fixed_blocked_probe(tmp_path: Path) -> None:
    plan = IsolationPlan(
        repo_root=_repo(tmp_path / "reckless-repo", env=False),
        agent_image=IMAGE_A,
        gateway_image=IMAGE_B,
        guard_image=IMAGE_C,
        endpoint_domains=(),
        credential_env_names=(),
        agent_command=("python3", "-m", "agent"),
        agent_env={
            "TRADEVOLVE_AGENT_MODE": "reckless",
            "TRADEVOLVE_RECKLESS_EGRESS_URL": "https://data.binance.vision/",
        },
    )
    plan.require_secret_source()
    assert _agent_secret_environment(plan) == {}
    agent_command = plan.create_commands()[6]
    assert "TRADEVOLVE_AGENT_MODE=reckless" in agent_command
    assert (
        "TRADEVOLVE_RECKLESS_EGRESS_URL=https://data.binance.vision/"
        in agent_command
    )
    assert "FIREWORKS_API_KEY" not in agent_command


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FIREWORKS_API_KEY", "not-even-a-real-secret"),
        ("TRADEVOLVE_AGENT_PORT", "9999"),
        ("HTTPS_PROXY", "https://proxy.example"),
        ("TRADEVOLVE_LLM_MODEL", "sk-secret-looking-value"),
        ("TRADEVOLVE_LLM_MODEL", "line-one\nline-two"),
    ],
)
def test_public_env_rejects_secret_or_reserved_surfaces(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        IsolationPlan(
            repo_root=_repo(tmp_path / name.lower().replace("_", "-")),
            agent_image=IMAGE_A,
            gateway_image=IMAGE_B,
            guard_image=IMAGE_C,
            endpoint_domains=("api.fireworks.ai",),
            agent_command=("agent",),
            agent_env={
                "TRADEVOLVE_AGENT_MODE": "llm",
                "TRADEVOLVE_LLM_MODEL": "safe/model",
                name: value,
            },
        )


def test_llm_public_env_must_bind_exact_domain_and_key_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="endpoint allowlist"):
        IsolationPlan(
            repo_root=_repo(tmp_path / "mismatch"),
            agent_image=IMAGE_A,
            gateway_image=IMAGE_B,
            guard_image=IMAGE_C,
            endpoint_domains=("other.example.com",),
            agent_command=("agent",),
            agent_env={
                "TRADEVOLVE_AGENT_MODE": "llm",
                "TRADEVOLVE_LLM_MODEL": "safe/model",
                "TRADEVOLVE_LLM_BASE_URL": (
                    "https://api.fireworks.ai/inference/v1"
                ),
            },
        )


def test_launch_failure_cleans_only_owned_exact_resources_and_no_secret_argv(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    plan.env_file.write_text(
        "FIREWORKS_API_KEY=model-secret\n"
        "DAYTONA_API_KEY=irrelevant-secret\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []
    secret_calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(command: tuple[str, ...]) -> str:
        calls.append(command)
        if command[:4] == ("docker", "inspect", "--format", "{{.State.Running}}"):
            return "true\n"
        if (
            command[:4]
            == (
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
            )
        ):
            return "true healthy\n"
        if command[:3] == ("docker", "exec", plan.guard_name):
            flag = command[-1]
            schemas = {
                "--check-ready": "sandbox_guard_ready/v1",
                "--check-firewall": "sandbox_firewall_check/v1",
                "--check-gateway": "sandbox_gateway_check/v1",
            }
            return '{"schema":"' + schemas[flag] + '"}\n'
        if (
            command[:3] == ("docker", "inspect", "--format")
            and "tradeevolve.sandbox_id" in command[3]
        ):
            return plan.sandbox_id + "\n"
        if (
            command[:4] == ("docker", "network", "inspect", "--format")
            and "tradeevolve.sandbox_id" in command[4]
        ):
            return plan.sandbox_id + "\n"
        return ""

    def run_with_env(
        command: tuple[str, ...],
        environment: dict[str, str] | object,
    ) -> str:
        assert isinstance(environment, dict)
        secret_calls.append((command, environment))
        raise PreflightFailure("synthetic agent launch failure")

    lifecycle = DockerSandbox(
        plan,
        run_text=run,
        run_with_env=run_with_env,
        health_check=lambda _host, _port: False,
    )
    with pytest.raises(PreflightFailure, match="synthetic"):
        lifecycle.start()

    assert len(secret_calls) == 1
    agent_argv, environment = secret_calls[0]
    assert environment == {"FIREWORKS_API_KEY": "model-secret"}
    assert "model-secret" not in repr(agent_argv)
    assert "irrelevant-secret" not in repr(agent_argv)
    assert "DAYTONA_API_KEY" not in agent_argv
    assert ("docker", "rm", "--force", plan.guard_name) in calls
    assert ("docker", "rm", "--force", plan.gateway_name) in calls
    assert ("docker", "network", "rm", plan.internal_network) in calls
    assert ("docker", "network", "rm", plan.egress_network) in calls
    assert ("docker", "rm", "--force", plan.agent_name) in calls


def test_docker_log_source_uses_stable_prefix_and_typed_tuple() -> None:
    line = (
        '{"event_payload":{"count":1,'
        '"destination":"domain-sha256:'
        + ("d" * 64)
        + '","port":443,"protocol":"https"},'
        '"schema":"sandbox_egress_block/v1","witness":"proxy_connect"}'
    )
    calls: dict[str, int] = {"gateway": 0, "guard": 0}

    def run(command: tuple[str, ...]) -> str:
        assert command[:2] == ("docker", "logs")
        source = "gateway" if command[2] == "gw" else "guard"
        calls[source] += 1
        if source == "guard":
            return ""
        return line if calls[source] == 1 else line + "\n" + line + "\n"

    source = DockerEgressEventSource(
        guard_name="guard",
        gateway_name="gw",
        run_text=run,
    )
    first = source.drain_harness_events()
    second = source.drain_harness_events()
    assert isinstance(first, tuple) and isinstance(second, tuple)
    assert len(first) == 1 and len(second) == 1
    assert first[0].type == second[0].type == "EgressBlocked"


def test_docker_log_source_fails_if_log_prefix_rotates() -> None:
    line = (
        '{"event_payload":{"count":1,'
        '"destination":"domain-sha256:'
        + ("e" * 64)
        + '","port":443,"protocol":"https"},'
        '"schema":"sandbox_egress_block/v1","witness":"proxy_connect"}'
    )
    gateway_outputs = iter((line + "\n", ""))

    def run(command: tuple[str, ...]) -> str:
        if command[2] == "gw":
            return next(gateway_outputs)
        return ""

    source = DockerEgressEventSource(
        guard_name="guard",
        gateway_name="gw",
        run_text=run,
    )
    assert len(source.drain_harness_events()) == 1
    with pytest.raises(PreflightFailure, match="rotated"):
        source.drain_harness_events()


def test_sandbox_handle_is_one_decision_and_event_source(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    requests: list[dict[str, object]] = []

    class FakeHTTPAgent:
        def decide(self, request: dict[str, object]) -> AgentReply:
            requests.append(request)
            return AgentReply(body=b'{"schema":"action/v1"}', http_status=200)

    class FakeEvents:
        def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
            return (
                HarnessEvent(
                    type="EgressBlocked",
                    payload={
                        "destination": "domain-sha256:" + ("a" * 64),
                        "port": 443,
                        "protocol": "https",
                        "count": 1,
                    },
                ),
            )

    handle = SandboxHandle(
        base_url="http://127.0.0.1:18080",
        http_agent=cast(HTTPAgent, FakeHTTPAgent()),
        event_source=cast(DockerEgressEventSource, FakeEvents()),
        snapshot=_snapshot(plan),
    )
    assert isinstance(handle, HarnessEventSource)
    request: dict[str, object] = {"schema": "runner_request/v1"}
    assert handle.decide(request).http_status == 200
    assert requests == [request]
    drained = handle.drain_harness_events()
    assert len(drained) == 1
    assert drained[0].type == "EgressBlocked"


def test_good_runtime_snapshot_proves_enforcement(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    result = evaluate_runtime_snapshot(plan, _snapshot(plan))
    assert result.ready
    assert result.failures == ()


def test_preflight_fails_closed_on_ipv6_or_guard_proof_drift(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    snapshot = _snapshot(plan)
    bad = replace(
        snapshot,
        internal_network_ipv6=True,
        guard_ready={**snapshot.guard_ready, "ipv6_disabled": False},
    )
    result = evaluate_runtime_snapshot(plan, bad)
    assert not result.ready
    assert any("IPv6" in failure for failure in result.failures)
    with pytest.raises(PreflightFailure):
        result.require_ready()


def test_preflight_refuses_guard_control_channel_on_internal_network(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    snapshot = _snapshot(plan)
    bad = replace(
        snapshot,
        guard=replace(snapshot.guard, network_mode=plan.internal_network),
    )
    result = evaluate_runtime_snapshot(plan, bad)
    assert not result.ready
    assert any("control channel" in failure for failure in result.failures)


@pytest.mark.parametrize(
    "escape_surface",
    [
        "root",
        "writable",
        "privileged",
        "caps-not-dropped",
        "cap-added",
        "bind-mount",
    ],
)
def test_preflight_refuses_each_agent_escape_surface(
    tmp_path: Path,
    escape_surface: str,
) -> None:
    plan = _plan(tmp_path)
    snapshot = _snapshot(plan)
    if escape_surface == "root":
        bad_agent = replace(snapshot.agent, user="0:0")
    elif escape_surface == "writable":
        bad_agent = replace(snapshot.agent, read_only=False)
    elif escape_surface == "privileged":
        bad_agent = replace(snapshot.agent, privileged=True)
    elif escape_surface == "caps-not-dropped":
        bad_agent = replace(snapshot.agent, cap_drop=())
    elif escape_surface == "cap-added":
        bad_agent = replace(snapshot.agent, cap_add=("NET_RAW",))
    elif escape_surface == "bind-mount":
        bad_agent = replace(snapshot.agent, mount_types=("bind",))
    else:
        raise AssertionError("unknown test parameter")
    result = evaluate_runtime_snapshot(
        plan,
        replace(snapshot, agent=bad_agent),
    )
    assert not result.ready
