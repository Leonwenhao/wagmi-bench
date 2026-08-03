# SPDX-License-Identifier: Apache-2.0
"""Shell-free Docker isolation planning and fail-closed runtime preflight."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence, cast

from agents.llm import LLMConfig, endpoint_domain
from harness.http import HTTPAgent
from harness.protocol import AgentReply, HarnessEvent
from sandbox.docker.guard.firewall_runtime import FirewallPlan
from sandbox.events import BlockEventBuffer, BlockRecordError
from sandbox.gateway import EndpointPolicy

_IMAGE_DIGEST_RE = re.compile(
    r"^(?:[a-zA-Z0-9][a-zA-Z0-9._/:+-]*@)?sha256:[0-9a-f]{64}$"
)
_NAMED_IMAGE_DIGEST_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._/:+-]*@sha256:[0-9a-f]{64}$"
)
_DOCKER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
_LOCAL_TAG_RE = re.compile(
    r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[a-zA-Z0-9_.-]+)?$"
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SANDBOX_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SECRETISH_VALUE_RE = re.compile(
    r"(?i)(?:"
    r"^(?:sk-|ghp_|github_pat_|xox[baprs]-|AIza)"
    r"|(?:api[_-]?key|access[_-]?token|secret|password|bearer)\s*[:=]"
    r")"
)
_PUBLIC_AGENT_ENV_NAMES = frozenset(
    {
        "TRADEVOLVE_AGENT_MODE",
        "TRADEVOLVE_LLM_MODEL",
        "TRADEVOLVE_LLM_BASE_URL",
        "TRADEVOLVE_LLM_TEMPERATURE",
        "TRADEVOLVE_LLM_MAX_TOKENS",
        "TRADEVOLVE_LLM_TIMEOUT_SECONDS",
        "TRADEVOLVE_LLM_API_KEY_ENV",
        "TRADEVOLVE_RECKLESS_EGRESS_URL",
    }
)
_RESERVED_AGENT_ENV_NAMES = frozenset(
    {
        "TRADEVOLVE_AGENT_HOST",
        "TRADEVOLVE_AGENT_PORT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)
_RECKLESS_EGRESS_URL = "https://data.binance.vision/"


class PreflightFailure(RuntimeError):
    """Isolation was not proved; no observation may be served."""


def _require_image_digest(value: str, *, named: bool = False) -> str:
    pattern = _NAMED_IMAGE_DIGEST_RE if named else _IMAGE_DIGEST_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        kind = "named base image" if named else "image"
        raise ValueError(f"{kind} must be pinned by sha256 digest")
    return value


def _docker_name(value: str, field: str) -> str:
    if not _DOCKER_NAME_RE.fullmatch(value):
        raise ValueError(f"{field} is not a safe Docker identifier")
    return value


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse the strict dotenv subset accepted for secret injection."""

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreflightFailure("credential source could not be read") from exc
    values: dict[str, str] = {}
    for number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise PreflightFailure(f"credential source line {number} has no equals sign")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _ENV_NAME_RE.fullmatch(name):
            raise PreflightFailure(f"credential source line {number} has invalid name")
        if name in values:
            raise PreflightFailure(f"credential source repeats {name}")
        if len(value) >= 2 and value[:1] == value[-1:] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "\x00" in value or "\r" in value or "\n" in value:
            raise PreflightFailure(f"credential source {name} has invalid bytes")
        values[name] = value
    return values


def load_protected_credentials(
    env_file: Path,
    credential_env_names: Sequence[str],
) -> tuple[str, ...]:
    """Load declared credentials for response-ingress fingerprinting.

    Callers pass these values directly to ``HTTPAgent``. The adapter reduces
    them to length/digest fingerprints during construction and retains no
    credential plaintext.
    """

    names = tuple(credential_env_names)
    if (
        len(set(names)) != len(names)
        or any(not _ENV_NAME_RE.fullmatch(name) for name in names)
    ):
        raise PreflightFailure("credential environment allowlist is invalid")
    if env_file.is_symlink() or not env_file.is_file():
        raise PreflightFailure(
            "credential source is missing or not a plain file"
        )
    source = _parse_env_file(env_file)
    selected: list[str] = []
    for name in names:
        value = source.get(name)
        if not value:
            raise PreflightFailure(
                "required agent credential is missing from the credential source"
            )
        selected.append(value)
    return tuple(selected)


def _validated_public_agent_env(
    raw: Mapping[str, str],
    *,
    endpoint_domains: tuple[str, ...],
    credential_env_names: tuple[str, ...],
) -> Mapping[str, str]:
    """Freeze public agent settings and bind them to egress/credential policy."""

    values: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("agent_env must map strings to strings")
        if name in _RESERVED_AGENT_ENV_NAMES:
            raise ValueError(f"agent_env cannot override reserved runtime field {name}")
        if name not in _PUBLIC_AGENT_ENV_NAMES:
            raise ValueError(f"agent_env field {name} is not public-allowlisted")
        if name in credential_env_names:
            raise ValueError("agent_env cannot duplicate a credential environment name")
        if not value or len(value) > 2048 or any(
            character in value for character in ("\x00", "\r", "\n")
        ):
            raise ValueError(f"agent_env value for {name} is empty, oversized, or multiline")
        if _SECRETISH_VALUE_RE.search(value):
            raise ValueError(f"agent_env value for {name} resembles a credential")
        values[name] = value

    mode = values.get("TRADEVOLVE_AGENT_MODE")
    llm_names = {
        name for name in values if name.startswith("TRADEVOLVE_LLM_")
    }
    reckless_url = values.get("TRADEVOLVE_RECKLESS_EGRESS_URL")
    if mode is None:
        if llm_names or reckless_url is not None:
            raise ValueError("agent_env policy settings require TRADEVOLVE_AGENT_MODE")
        return MappingProxyType(values)
    if mode == "llm":
        if reckless_url is not None:
            raise ValueError("LLM mode cannot carry reckless egress configuration")
        try:
            config = LLMConfig.from_env(values)
        except ValueError as exc:
            raise ValueError("agent_env LLM configuration is invalid") from exc
        expected_domain = endpoint_domain(config.base_url)
        if endpoint_domains != (expected_domain,):
            raise ValueError(
                "LLM base URL must match the exact sandbox endpoint allowlist"
            )
        if credential_env_names != (config.api_key_env_name,):
            raise ValueError(
                "LLM provider key name must match the exact credential allowlist"
            )
    elif mode == "reckless":
        if llm_names:
            raise ValueError("reckless mode cannot carry LLM configuration")
        if reckless_url != _RECKLESS_EGRESS_URL:
            raise ValueError(
                "reckless mode requires the fixed blocked Binance egress probe"
            )
        if endpoint_domains:
            raise ValueError("reckless mode must use a deny-all endpoint allowlist")
        if credential_env_names:
            raise ValueError("reckless mode must not receive credentials")
    else:
        raise ValueError("TRADEVOLVE_AGENT_MODE must be 'llm' or 'reckless'")
    return MappingProxyType(values)


@dataclass(frozen=True)
class DockerContext:
    """A deny-by-default build context with an exact transmitted file set."""

    root: Path
    expected_files: tuple[str, ...]
    dockerignore_text: str

    def audit(self) -> tuple[str, ...]:
        root = self.root.resolve(strict=True)
        if not root.is_dir():
            raise PreflightFailure("Docker build context is not a directory")
        for candidate in self.root.rglob("*"):
            if candidate.is_symlink():
                raise PreflightFailure("Docker build context contains a symlink")
        ignore_path = root / ".dockerignore"
        if ignore_path.read_text(encoding="utf-8") != self.dockerignore_text:
            raise PreflightFailure("Docker build context denylist drifted")
        for relative in self.expected_files:
            candidate = root / relative
            if not candidate.is_file() or candidate.is_symlink():
                raise PreflightFailure(
                    f"Docker build context is missing required file {relative}"
                )
        return self.expected_files

    def build_command(
        self,
        *,
        base_argument: str,
        base_image: str,
        output_tag: str,
    ) -> tuple[str, ...]:
        self.audit()
        if base_argument not in {"PYTHON_BASE", "GUARD_BASE"}:
            raise ValueError("unsupported Dockerfile base argument")
        _require_image_digest(base_image, named=True)
        if not _LOCAL_TAG_RE.fullmatch(output_tag):
            raise ValueError("invalid local Docker output tag")
        return (
            "docker",
            "build",
            "--pull=false",
            "--network=none",
            "--build-arg",
            f"{base_argument}={base_image}",
            "--tag",
            output_tag,
            str(self.root.resolve()),
        )


_DOCKER_ROOT = Path(__file__).resolve().parent / "docker"
GATEWAY_CONTEXT = DockerContext(
    root=_DOCKER_ROOT / "gateway",
    expected_files=(".dockerignore", "Dockerfile", "gateway_server.py"),
    dockerignore_text=(
        "**\n"
        "!Dockerfile\n"
        "!.dockerignore\n"
        "!gateway_server.py\n"
    ),
)
GUARD_CONTEXT = DockerContext(
    root=_DOCKER_ROOT / "guard",
    expected_files=(
        ".dockerignore",
        "Dockerfile",
        "__init__.py",
        "block_collector.py",
        "firewall_runtime.py",
        "guard_main.py",
    ),
    dockerignore_text=(
        "**\n"
        "!Dockerfile\n"
        "!.dockerignore\n"
        "!__init__.py\n"
        "!block_collector.py\n"
        "!firewall_runtime.py\n"
        "!guard_main.py\n"
    ),
)


@dataclass(frozen=True)
class IsolationPlan:
    """Exact Docker objects required before the HTTP runner may start."""

    repo_root: Path
    agent_image: str
    gateway_image: str
    guard_image: str
    endpoint_domains: tuple[str, ...]
    agent_command: tuple[str, ...]
    credential_env_names: tuple[str, ...] = ("FIREWORKS_API_KEY",)
    agent_env: Mapping[str, str] = field(default_factory=dict)
    sandbox_id: str = field(default_factory=lambda: secrets.token_hex(16))
    agent_name: str = "tradeevolve-agent"
    gateway_name: str = "tradeevolve-gateway"
    guard_name: str = "tradeevolve-guard"
    internal_network: str = "tradeevolve-agent-net"
    egress_network: str = "tradeevolve-gateway-egress"
    subnet: str = "172.30.240.0/24"
    gateway_ipv4: str = "172.30.240.2"
    agent_uid: int = 65532
    agent_http_port: int = 8000
    host_http_port: int = 18080
    gateway_port: int = 3128
    collector_tcp_port: int = 15001
    collector_udp_port: int = 15002

    def __post_init__(self) -> None:
        _require_image_digest(self.agent_image)
        _require_image_digest(self.gateway_image)
        _require_image_digest(self.guard_image)
        EndpointPolicy.from_domains(self.endpoint_domains)
        if (
            len(set(self.credential_env_names)) != len(self.credential_env_names)
            or any(
                not _ENV_NAME_RE.fullmatch(name)
                for name in self.credential_env_names
            )
        ):
            raise ValueError("credential environment allowlist is invalid")
        object.__setattr__(
            self,
            "agent_env",
            _validated_public_agent_env(
                self.agent_env,
                endpoint_domains=self.endpoint_domains,
                credential_env_names=self.credential_env_names,
            ),
        )
        if not _SANDBOX_ID_RE.fullmatch(self.sandbox_id):
            raise ValueError("sandbox_id must be 16 random bytes in lowercase hex")
        if not self.agent_command or any(not item for item in self.agent_command):
            raise ValueError("agent command must be a non-empty argv tuple")
        for value, field_name in (
            (self.agent_name, "agent_name"),
            (self.gateway_name, "gateway_name"),
            (self.guard_name, "guard_name"),
            (self.internal_network, "internal_network"),
            (self.egress_network, "egress_network"),
        ):
            _docker_name(value, field_name)
        if len({self.agent_name, self.gateway_name, self.guard_name}) != 3:
            raise ValueError("container names must be distinct")
        if self.internal_network == self.egress_network:
            raise ValueError("internal and egress networks must differ")
        try:
            network = ipaddress.ip_network(self.subnet)
            gateway = ipaddress.ip_address(self.gateway_ipv4)
        except ValueError as exc:
            raise ValueError("invalid sandbox IPv4 network") from exc
        if (
            network.version != 4
            or gateway.version != 4
            or gateway not in network
            or gateway in {network.network_address, network.broadcast_address}
        ):
            raise ValueError("gateway IPv4 must be a host in the sandbox subnet")
        FirewallPlan(
            agent_uid=self.agent_uid,
            gateway_ipv4=self.gateway_ipv4,
            agent_http_port=self.agent_http_port,
            gateway_port=self.gateway_port,
            collector_tcp_port=self.collector_tcp_port,
            collector_udp_port=self.collector_udp_port,
        )
        for port_value, port_name in (
            (self.agent_http_port, "agent_http_port"),
            (self.host_http_port, "host_http_port"),
        ):
            if not 1024 <= port_value <= 65_535:
                raise ValueError(f"{port_name} must be an unprivileged port")
        env_file = self.repo_root.resolve() / ".env"
        if self.credential_env_names and env_file.is_symlink():
            raise ValueError(".env may not be a symlink")

    @property
    def firewall_plan(self) -> FirewallPlan:
        return FirewallPlan(
            agent_uid=self.agent_uid,
            gateway_ipv4=self.gateway_ipv4,
            agent_http_port=self.agent_http_port,
            gateway_port=self.gateway_port,
            collector_tcp_port=self.collector_tcp_port,
            collector_udp_port=self.collector_udp_port,
        )

    @property
    def env_file(self) -> Path:
        return self.repo_root.resolve() / ".env"

    def require_secret_source(self) -> None:
        """Refuse launch unless the gitignored root ``.env`` is a plain file."""

        if not self.credential_env_names:
            return
        env_file = self.env_file
        if not env_file.is_file() or env_file.is_symlink():
            raise PreflightFailure("repo-root .env is missing or not a plain file")
        gitignore = self.repo_root.resolve() / ".gitignore"
        if not gitignore.is_file():
            raise PreflightFailure(".gitignore is missing")
        ignored = {
            line.strip()
            for line in gitignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if ".env" not in ignored:
            raise PreflightFailure("repo-root .env is not explicitly gitignored")
        values = _parse_env_file(env_file)
        for name in self.credential_env_names:
            if not values.get(name):
                raise PreflightFailure(f"required agent credential {name} is missing")

    def create_commands(self) -> tuple[tuple[str, ...], ...]:
        """Return ordered, shell-free Docker creation commands.

        The guard owns the namespace before the agent joins it, closing the
        usual start-up race.  The caller must run guard preflight between the
        guard and agent commands, then full runtime preflight before turn 0.
        """

        allowlist = json.dumps(
            list(self.endpoint_domains),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        sandbox_label = f"tradeevolve.sandbox_id={self.sandbox_id}"
        proxy = f"http://{self.gateway_ipv4}:{self.gateway_port}"
        public_agent_arguments = tuple(
            argument
            for name, value in sorted(self.agent_env.items())
            for argument in ("--env", f"{name}={value}")
        )
        return (
            (
                "docker",
                "network",
                "create",
                "--internal",
                "--ipv6=false",
                "--subnet",
                self.subnet,
                "--label",
                sandbox_label,
                self.internal_network,
            ),
            (
                "docker",
                "network",
                "create",
                "--ipv6=false",
                "--label",
                sandbox_label,
                self.egress_network,
            ),
            (
                "docker",
                "run",
                "--detach",
                "--name",
                self.gateway_name,
                "--label",
                sandbox_label,
                "--network",
                self.internal_network,
                "--ip",
                self.gateway_ipv4,
                "--user",
                "65532:65532",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=10m",
                "--log-opt",
                "max-file=1",
                "--log-opt",
                "compress=false",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m",
                self.gateway_image,
                "--endpoint-domains-json",
                allowlist,
                "--port",
                str(self.gateway_port),
            ),
            (
                "docker",
                "network",
                "connect",
                self.egress_network,
                self.gateway_name,
            ),
            (
                "docker",
                "run",
                "--detach",
                "--name",
                self.guard_name,
                "--label",
                sandbox_label,
                "--network",
                self.egress_network,
                "--dns",
                "127.0.0.1",
                "--sysctl",
                "net.ipv6.conf.all.disable_ipv6=1",
                "--sysctl",
                "net.ipv6.conf.default.disable_ipv6=1",
                "--publish",
                f"127.0.0.1:{self.host_http_port}:{self.agent_http_port}/tcp",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_ADMIN",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "SETUID",
                "--security-opt",
                "no-new-privileges:true",
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=10m",
                "--log-opt",
                "max-file=1",
                "--log-opt",
                "compress=false",
                "--pids-limit",
                "128",
                "--tmpfs",
                (
                    "/run/tradeevolve:rw,noexec,nosuid,nodev,size=1m,"
                    "uid=65533,gid=65533,mode=0755"
                ),
                "--env",
                f"AGENT_UID={self.agent_uid}",
                "--env",
                f"AGENT_HTTP_PORT={self.agent_http_port}",
                "--env",
                f"GATEWAY_IPV4={self.gateway_ipv4}",
                "--env",
                f"GATEWAY_PORT={self.gateway_port}",
                "--env",
                f"COLLECTOR_TCP_PORT={self.collector_tcp_port}",
                "--env",
                f"COLLECTOR_UDP_PORT={self.collector_udp_port}",
                self.guard_image,
            ),
            (
                "docker",
                "network",
                "connect",
                self.internal_network,
                self.guard_name,
            ),
            (
                "docker",
                "run",
                "--detach",
                "--name",
                self.agent_name,
                "--label",
                sandbox_label,
                "--network",
                f"container:{self.guard_name}",
                "--user",
                f"{self.agent_uid}:{self.agent_uid}",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--log-driver",
                "none",
                "--pids-limit",
                "256",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=64m",
                *tuple(
                    argument
                    for name in self.credential_env_names
                    for argument in ("--env", name)
                ),
                *public_agent_arguments,
                "--env",
                f"TRADEVOLVE_AGENT_PORT={self.agent_http_port}",
                "--env",
                "TRADEVOLVE_AGENT_HOST=0.0.0.0",
                "--env",
                f"HTTPS_PROXY={proxy}",
                "--env",
                f"https_proxy={proxy}",
                "--env",
                f"HTTP_PROXY={proxy}",
                "--env",
                f"http_proxy={proxy}",
                "--env",
                "NO_PROXY=127.0.0.1,localhost",
                "--env",
                "no_proxy=127.0.0.1,localhost",
                self.agent_image,
                *self.agent_command,
            ),
        )


@dataclass(frozen=True)
class ContainerSnapshot:
    name: str
    container_id: str
    image_id: str
    running: bool
    health_status: str | None
    user: str
    read_only: bool
    privileged: bool
    cap_drop: tuple[str, ...]
    cap_add: tuple[str, ...]
    security_opt: tuple[str, ...]
    network_mode: str
    dns: tuple[str, ...]
    sysctls: tuple[tuple[str, str], ...]
    mount_types: tuple[str, ...]
    tmpfs_paths: tuple[str, ...]
    port_bindings: tuple[tuple[str, str, str], ...]
    networks: tuple[str, ...]
    network_ipv4: tuple[tuple[str, str], ...]
    env_names: tuple[str, ...]
    sandbox_id_label: str | None
    log_driver: str


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Sanitized Docker evidence; container environment values are discarded."""

    server_os: str
    security_options: tuple[str, ...]
    agent: ContainerSnapshot
    guard: ContainerSnapshot
    gateway: ContainerSnapshot
    internal_network_is_internal: bool
    internal_network_ipv6: bool
    internal_network_label: str | None
    egress_network_is_internal: bool
    egress_network_label: str | None
    guard_ready: Mapping[str, object]
    firewall_check: Mapping[str, object]
    gateway_check: Mapping[str, object]


@dataclass(frozen=True)
class PreflightResult:
    ready: bool
    failures: tuple[str, ...]

    def require_ready(self) -> None:
        if not self.ready:
            raise PreflightFailure("; ".join(self.failures))


def _has_nnp(options: Sequence[str]) -> bool:
    return any(option.lower().startswith("no-new-privileges") for option in options)


def _capability_names(values: Sequence[str]) -> set[str]:
    """Normalize Docker's version-dependent optional ``CAP_`` prefix."""

    names: set[str] = set()
    for value in values:
        lowered = value.lower()
        names.add(lowered[4:] if lowered.startswith("cap_") else lowered)
    return names


def _network_mode_matches_guard(mode: str, guard: ContainerSnapshot) -> bool:
    if not mode.startswith("container:"):
        return False
    target = mode.split(":", 1)[1]
    return target in {guard.name, guard.container_id} or guard.container_id.startswith(
        target
    )


def evaluate_runtime_snapshot(
    plan: IsolationPlan,
    snapshot: RuntimeSnapshot,
) -> PreflightResult:
    """Evaluate every enforcement claim needed before serving turn zero."""

    failures: list[str] = []

    if snapshot.server_os.lower() != "linux":
        failures.append("Docker server is not Linux")
    if not any("seccomp" in item.lower() for item in snapshot.security_options):
        failures.append("Docker seccomp is not active")
    if not snapshot.internal_network_is_internal:
        failures.append("agent network is not Docker-internal")
    if snapshot.internal_network_ipv6:
        failures.append("agent network has Docker IPv6 enabled")
    if snapshot.egress_network_is_internal:
        failures.append("gateway egress network is unexpectedly internal")

    agent = snapshot.agent
    if not agent.running:
        failures.append("agent container is not running")
    if agent.user != f"{plan.agent_uid}:{plan.agent_uid}":
        failures.append("agent does not run as the fixed non-root uid")
    if not agent.read_only:
        failures.append("agent root filesystem is writable")
    if agent.privileged:
        failures.append("agent container is privileged")
    if "all" not in {item.lower() for item in agent.cap_drop} or agent.cap_add:
        failures.append("agent capabilities are not fully dropped")
    if not _has_nnp(agent.security_opt):
        failures.append("agent no-new-privileges is missing")
    if not _network_mode_matches_guard(agent.network_mode, snapshot.guard):
        failures.append("agent does not share the pre-armed guard namespace")
    if agent.mount_types:
        failures.append("agent has a host/volume mount")
    if set(agent.tmpfs_paths) - {"/tmp"}:
        failures.append("agent has an unexpected tmpfs")
    if agent.port_bindings:
        failures.append("agent publishes a port directly")
    if not _SHA256_RE.fullmatch(agent.image_id):
        failures.append("agent runtime image id is not content-addressed")
    if agent.sandbox_id_label != plan.sandbox_id:
        failures.append("agent ownership label is missing")
    if agent.log_driver != "none":
        failures.append("agent Docker logging is not disabled")

    guard = snapshot.guard
    if not guard.running or guard.health_status != "healthy":
        failures.append("guard container is not running and healthy")
    if not guard.read_only or guard.privileged:
        failures.append("guard is writable or Docker-privileged")
    if "all" not in {item.lower() for item in guard.cap_drop}:
        failures.append("guard did not drop the default capability set")
    if _capability_names(guard.cap_add) != {
        "net_admin",
        "setgid",
        "setuid",
    }:
        failures.append(
            "guard bootstrap capabilities are not exactly "
            "NET_ADMIN, SETGID, and SETUID"
        )
    if not _has_nnp(guard.security_opt):
        failures.append("guard no-new-privileges is missing")
    if guard.network_mode != plan.egress_network:
        failures.append("guard loopback control channel is not egress-network bound")
    if guard.dns != ("127.0.0.1",):
        failures.append("guard namespace DNS is not loopback-only")
    expected_sysctls = {
        "net.ipv6.conf.all.disable_ipv6": "1",
        "net.ipv6.conf.default.disable_ipv6": "1",
    }
    if dict(guard.sysctls) != expected_sysctls:
        failures.append("guard namespace IPv6-disable sysctls are missing")
    if guard.mount_types:
        failures.append("guard has a host/volume mount")
    if set(guard.tmpfs_paths) != {"/run/tradeevolve"}:
        failures.append("guard readiness tmpfs is missing or unexpected")
    expected_binding = (
        f"{plan.agent_http_port}/tcp",
        "127.0.0.1",
        str(plan.host_http_port),
    )
    if guard.port_bindings != (expected_binding,):
        failures.append("agent API is not the sole loopback-published port")
    if set(guard.networks) != {plan.internal_network, plan.egress_network}:
        failures.append("guard is not attached to exactly two planned networks")
    if not _SHA256_RE.fullmatch(guard.image_id):
        failures.append("guard runtime image id is not content-addressed")
    if guard.sandbox_id_label != plan.sandbox_id:
        failures.append("guard ownership label is missing")
    if guard.log_driver != "local":
        failures.append("guard block logging is not local/durable")

    gateway = snapshot.gateway
    if not gateway.running:
        failures.append("gateway container is not running")
    if gateway.user != "65532:65532":
        failures.append("gateway does not run as its fixed non-root uid")
    if not gateway.read_only or gateway.privileged:
        failures.append("gateway is writable or privileged")
    if "all" not in {item.lower() for item in gateway.cap_drop} or gateway.cap_add:
        failures.append("gateway capabilities are not fully dropped")
    if not _has_nnp(gateway.security_opt):
        failures.append("gateway no-new-privileges is missing")
    if gateway.mount_types or gateway.port_bindings:
        failures.append("gateway has a mount or published port")
    if set(gateway.tmpfs_paths) - {"/tmp"}:
        failures.append("gateway has an unexpected tmpfs")
    if set(gateway.networks) != {plan.internal_network, plan.egress_network}:
        failures.append("gateway is not attached to exactly two planned networks")
    if dict(gateway.network_ipv4).get(plan.internal_network) != plan.gateway_ipv4:
        failures.append("gateway internal IPv4 does not match firewall policy")
    if not _SHA256_RE.fullmatch(gateway.image_id):
        failures.append("gateway runtime image id is not content-addressed")
    if gateway.sandbox_id_label != plan.sandbox_id:
        failures.append("gateway ownership label is missing")
    if gateway.log_driver != "local":
        failures.append("gateway block logging is not local/durable")
    credential_names = {"FIREWORKS_API_KEY", "DAYTONA_API_KEY"}
    if credential_names.intersection(guard.env_names):
        failures.append("guard received a credential environment variable")
    if credential_names.intersection(gateway.env_names):
        failures.append("gateway received a credential environment variable")
    if snapshot.internal_network_label != plan.sandbox_id:
        failures.append("internal network ownership label is missing")
    if snapshot.egress_network_label != plan.sandbox_id:
        failures.append("egress network ownership label is missing")

    expected_firewall = plan.firewall_plan.sha256()
    ready = snapshot.guard_ready
    if (
        ready.get("schema") != "sandbox_guard_ready/v1"
        or ready.get("ready") is not True
        or ready.get("firewall_sha256") != expected_firewall
        or ready.get("euid") != 65533
        or ready.get("cap_eff_hex") != "0000000000000000"
        or ready.get("ipv6_disabled") is not True
    ):
        failures.append("guard readiness/privilege/IPv6 proof is invalid")
    firewall = snapshot.firewall_check
    if (
        firewall.get("schema") != "sandbox_firewall_check/v1"
        or firewall.get("ok") is not True
        or firewall.get("firewall_sha256") != expected_firewall
    ):
        failures.append("live firewall rule proof is invalid")
    gateway_check = snapshot.gateway_check
    if (
        gateway_check.get("schema") != "sandbox_gateway_check/v1"
        or gateway_check.get("ok") is not True
        or gateway_check.get("gateway_ipv4") != plan.gateway_ipv4
        or gateway_check.get("gateway_port") != plan.gateway_port
    ):
        failures.append("gateway listener proof is invalid")

    return PreflightResult(ready=not failures, failures=tuple(failures))


def _dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PreflightFailure(f"Docker probe field {field} is not an object")
    return cast(dict[str, object], value)


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PreflightFailure("Docker probe string-array field is invalid")
    return tuple(cast(list[str], value))


def _container_snapshot(document: object) -> ContainerSnapshot:
    root = _dict(document, "container")
    config = _dict(root.get("Config"), "Config")
    host = _dict(root.get("HostConfig"), "HostConfig")
    network_settings = _dict(root.get("NetworkSettings"), "NetworkSettings")
    networks = _dict(network_settings.get("Networks", {}), "Networks")
    state = _dict(root.get("State"), "State")
    mounts_raw = root.get("Mounts", [])
    if not isinstance(mounts_raw, list):
        raise PreflightFailure("Docker probe Mounts is invalid")
    mount_types: list[str] = []
    for item in mounts_raw:
        mount = _dict(item, "Mounts[]")
        kind = mount.get("Type")
        if isinstance(kind, str):
            mount_types.append(kind)

    tmpfs = _dict(host.get("Tmpfs", {}), "Tmpfs")
    port_map = _dict(host.get("PortBindings", {}), "PortBindings")
    port_bindings: list[tuple[str, str, str]] = []
    for container_port, bindings in sorted(port_map.items()):
        if bindings is None:
            continue
        if not isinstance(bindings, list):
            raise PreflightFailure("Docker probe PortBindings is invalid")
        for item in bindings:
            binding = _dict(item, "PortBindings[]")
            host_ip = binding.get("HostIp")
            host_port = binding.get("HostPort")
            if not isinstance(host_ip, str) or not isinstance(host_port, str):
                raise PreflightFailure("Docker port binding is invalid")
            port_bindings.append((container_port, host_ip, host_port))

    sysctls_raw = host.get("Sysctls", {})
    if sysctls_raw is None:
        sysctls_raw = {}
    sysctls_map = _dict(sysctls_raw, "Sysctls")
    sysctls: list[tuple[str, str]] = []
    for key, value in sorted(sysctls_map.items()):
        if not isinstance(value, str):
            raise PreflightFailure("Docker sysctl value is invalid")
        sysctls.append((key, value))

    network_ipv4: list[tuple[str, str]] = []
    for network_name, network_value in sorted(networks.items()):
        network_document = _dict(network_value, f"Networks.{network_name}")
        address = network_document.get("IPAddress", "")
        if not isinstance(address, str):
            raise PreflightFailure("Docker network IPv4 field is invalid")
        network_ipv4.append((network_name, address))

    environment = config.get("Env", [])
    if environment is None:
        environment = []
    if not isinstance(environment, list) or not all(
        isinstance(item, str) for item in environment
    ):
        raise PreflightFailure("Docker environment field is invalid")
    env_names = tuple(
        sorted(
            {
                cast(str, item).split("=", 1)[0]
                for item in environment
                if "=" in cast(str, item)
            }
        )
    )
    labels_value = config.get("Labels", {})
    if labels_value is None:
        labels_value = {}
    labels = _dict(labels_value, "Config.Labels")
    sandbox_label = labels.get("tradeevolve.sandbox_id")
    if sandbox_label is not None and not isinstance(sandbox_label, str):
        raise PreflightFailure("Docker sandbox label is invalid")
    log_config = _dict(host.get("LogConfig", {}), "LogConfig")
    log_driver = log_config.get("Type", "")
    if not isinstance(log_driver, str):
        raise PreflightFailure("Docker log driver is invalid")

    health_status: str | None = None
    health_value = state.get("Health")
    if health_value is not None:
        health = _dict(health_value, "State.Health")
        status = health.get("Status")
        if not isinstance(status, str):
            raise PreflightFailure("Docker health status is invalid")
        health_status = status

    name = root.get("Name")
    container_id = root.get("Id")
    image_id = root.get("Image")
    user = config.get("User", "")
    network_mode = host.get("NetworkMode", "")
    if not all(
        isinstance(item, str)
        for item in (name, container_id, image_id, user, network_mode)
    ):
        raise PreflightFailure("Docker container identity fields are invalid")
    return ContainerSnapshot(
        name=cast(str, name).lstrip("/"),
        container_id=cast(str, container_id),
        image_id=cast(str, image_id),
        running=state.get("Running") is True,
        health_status=health_status,
        user=cast(str, user),
        read_only=host.get("ReadonlyRootfs") is True,
        privileged=host.get("Privileged") is True,
        cap_drop=_string_tuple(host.get("CapDrop")),
        cap_add=_string_tuple(host.get("CapAdd")),
        security_opt=_string_tuple(host.get("SecurityOpt")),
        network_mode=cast(str, network_mode),
        dns=_string_tuple(host.get("Dns")),
        sysctls=tuple(sysctls),
        mount_types=tuple(sorted(mount_types)),
        tmpfs_paths=tuple(sorted(tmpfs)),
        port_bindings=tuple(port_bindings),
        networks=tuple(sorted(networks)),
        network_ipv4=tuple(network_ipv4),
        env_names=env_names,
        sandbox_id_label=sandbox_label,
        log_driver=log_driver,
    )


RunText = Callable[[tuple[str, ...]], str]
RunWithEnv = Callable[[tuple[str, ...], Mapping[str, str]], str]


def _run_text(command: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        operation = " ".join(command[:2])
        raise PreflightFailure(f"Docker isolation probe failed: {operation}") from exc
    return completed.stdout


def _run_text_with_env(
    command: tuple[str, ...],
    secret_environment: Mapping[str, str],
) -> str:
    """Run a command with transient secrets absent from argv and diagnostics."""

    environment = os.environ.copy()
    environment.update(secret_environment)
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        operation = " ".join(command[:2])
        raise PreflightFailure(f"Docker isolation launch failed: {operation}") from exc
    return completed.stdout


class DockerRuntimePreflight:
    """Collect sanitized Docker state and refuse unproved isolation."""

    def __init__(self, run_text: RunText = _run_text) -> None:
        self._run_text = run_text

    def collect(self, plan: IsolationPlan) -> RuntimeSnapshot:
        GATEWAY_CONTEXT.audit()
        GUARD_CONTEXT.audit()
        try:
            server_os = self._run_text(
                ("docker", "version", "--format", "{{.Server.Os}}")
            ).strip()
            security_options_value = json.loads(
                self._run_text(
                    ("docker", "info", "--format", "{{json .SecurityOptions}}")
                )
            )
            containers_value = json.loads(
                self._run_text(
                    (
                        "docker",
                        "inspect",
                        plan.agent_name,
                        plan.guard_name,
                        plan.gateway_name,
                    )
                )
            )
            networks_value = json.loads(
                self._run_text(
                    (
                        "docker",
                        "network",
                        "inspect",
                        plan.internal_network,
                        plan.egress_network,
                    )
                )
            )
            ready = json.loads(
                self._run_text(
                    (
                        "docker",
                        "exec",
                        plan.guard_name,
                        "python3",
                        "-m",
                        "tradeevolve_guard.guard_main",
                        "--check-ready",
                    )
                )
            )
            firewall = json.loads(
                self._run_text(
                    (
                        "docker",
                        "exec",
                        plan.guard_name,
                        "python3",
                        "-m",
                        "tradeevolve_guard.guard_main",
                        "--check-firewall",
                    )
                )
            )
            gateway_check = json.loads(
                self._run_text(
                    (
                        "docker",
                        "exec",
                        plan.guard_name,
                        "python3",
                        "-m",
                        "tradeevolve_guard.guard_main",
                        "--check-gateway",
                    )
                )
            )
        except (json.JSONDecodeError, TypeError) as exc:
            raise PreflightFailure("Docker isolation probe returned invalid JSON") from exc
        if (
            not isinstance(security_options_value, list)
            or not all(isinstance(item, str) for item in security_options_value)
            or not isinstance(containers_value, list)
            or len(containers_value) != 3
            or not isinstance(networks_value, list)
            or len(networks_value) != 2
            or not isinstance(ready, dict)
            or not isinstance(firewall, dict)
            or not isinstance(gateway_check, dict)
        ):
            raise PreflightFailure("Docker isolation probe returned an invalid shape")
        internal = _dict(networks_value[0], "internal network")
        egress = _dict(networks_value[1], "egress network")
        internal_labels_value = internal.get("Labels", {})
        egress_labels_value = egress.get("Labels", {})
        if internal_labels_value is None:
            internal_labels_value = {}
        if egress_labels_value is None:
            egress_labels_value = {}
        internal_labels = _dict(internal_labels_value, "internal network labels")
        egress_labels = _dict(egress_labels_value, "egress network labels")
        internal_sandbox_label = internal_labels.get("tradeevolve.sandbox_id")
        egress_sandbox_label = egress_labels.get("tradeevolve.sandbox_id")
        if (
            internal_sandbox_label is not None
            and not isinstance(internal_sandbox_label, str)
        ) or (
            egress_sandbox_label is not None
            and not isinstance(egress_sandbox_label, str)
        ):
            raise PreflightFailure("Docker network sandbox label is invalid")
        return RuntimeSnapshot(
            server_os=server_os,
            security_options=tuple(cast(list[str], security_options_value)),
            agent=_container_snapshot(containers_value[0]),
            guard=_container_snapshot(containers_value[1]),
            gateway=_container_snapshot(containers_value[2]),
            internal_network_is_internal=internal.get("Internal") is True,
            internal_network_ipv6=internal.get("EnableIPv6") is True,
            internal_network_label=internal_sandbox_label,
            egress_network_is_internal=egress.get("Internal") is True,
            egress_network_label=egress_sandbox_label,
            guard_ready=cast(dict[str, object], ready),
            firewall_check=cast(dict[str, object], firewall),
            gateway_check=cast(dict[str, object], gateway_check),
        )

    def require_ready(self, plan: IsolationPlan) -> RuntimeSnapshot:
        plan.require_secret_source()
        snapshot = self.collect(plan)
        evaluate_runtime_snapshot(plan, snapshot).require_ready()
        return snapshot


def _agent_secret_environment(plan: IsolationPlan) -> dict[str, str]:
    """Select only declared agent keys from root ``.env`` into process memory."""

    if not plan.credential_env_names:
        return {}
    values = load_protected_credentials(
        plan.env_file,
        plan.credential_env_names,
    )
    return dict(zip(plan.credential_env_names, values, strict=True))


class DockerEgressEventSource:
    """Snapshot Docker block logs and expose the typed harness event source."""

    def __init__(
        self,
        *,
        guard_name: str,
        gateway_name: str,
        run_text: RunText = _run_text,
    ) -> None:
        self._containers = (
            ("gateway", gateway_name),
            ("guard", guard_name),
        )
        self._run_text = run_text
        self._prior_lines: dict[str, tuple[str, ...]] = {
            source: () for source, _name in self._containers
        }
        self._buffer = BlockEventBuffer()
        self._lock = threading.Lock()

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        """Read a complete log snapshot, reject loss/drift, then drain facts."""

        with self._lock:
            for source, container in self._containers:
                output = self._run_text(("docker", "logs", container))
                current = tuple(output.splitlines())
                prior = self._prior_lines[source]
                if len(current) < len(prior) or current[: len(prior)] != prior:
                    raise PreflightFailure(
                        f"{source} block log rotated or changed before ingestion"
                    )
                for index, line in enumerate(
                    current[len(prior) :],
                    start=len(prior),
                ):
                    try:
                        self._buffer.append(
                            source=source,
                            cursor=str(index),
                            line=line,
                        )
                    except BlockRecordError as exc:
                        raise PreflightFailure(
                            f"{source} emitted an invalid block record"
                        ) from exc
                self._prior_lines[source] = current
            return self._buffer.drain_harness_events()


@dataclass(frozen=True)
class SandboxHandle:
    """One runnable agent combining IC-6 decisions and sandbox evidence."""

    base_url: str
    http_agent: HTTPAgent
    event_source: DockerEgressEventSource
    snapshot: RuntimeSnapshot

    def decide(self, request: dict[str, object]) -> AgentReply:
        """Delegate the exact IC-6 attempt to the bounded HTTP adapter."""

        return self.http_agent.decide(request)

    def drain_harness_events(self) -> tuple[HarnessEvent, ...]:
        """Drain egress facts from the same object passed to ``run_episode``."""

        return self.event_source.drain_harness_events()


HealthCheck = Callable[[str, int], bool]
Monotonic = Callable[[], float]
Sleep = Callable[[float], None]


def _health_check(host: str, port: int) -> bool:
    connection = http.client.HTTPConnection(host, port, timeout=1.0)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        response.read(4096)
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()


class DockerSandbox:
    """Create, prove, expose, and safely clean one exact sandbox lifecycle.

    This class deliberately stops at a ready base URL and
    ``HarnessEventSource``.  ``harness.http`` owns IC-6 request/retry behavior;
    keeping it outside this module prevents the engine/recorder from entering
    the agent container.
    """

    def __init__(
        self,
        plan: IsolationPlan,
        *,
        run_text: RunText = _run_text,
        run_with_env: RunWithEnv = _run_text_with_env,
        health_check: HealthCheck = _health_check,
        monotonic: Monotonic = time.monotonic,
        sleep: Sleep = time.sleep,
        startup_timeout_s: float = 30.0,
    ) -> None:
        if startup_timeout_s <= 0:
            raise ValueError("startup timeout must be positive")
        self.plan = plan
        self._run_text = run_text
        self._run_with_env = run_with_env
        self._health_check = health_check
        self._monotonic = monotonic
        self._sleep = sleep
        self._startup_timeout_s = startup_timeout_s
        self._created_containers: list[str] = []
        self._created_networks: list[str] = []
        self._handle: SandboxHandle | None = None

    def _run_create(self, command: tuple[str, ...], *, resource: str) -> None:
        if command[1:3] == ("network", "create"):
            self._created_networks.append(resource)
        elif command[1] == "run":
            self._created_containers.append(resource)
        self._run_text(command)

    def _run_agent_create(
        self,
        command: tuple[str, ...],
        *,
        secret_environment: Mapping[str, str],
    ) -> None:
        self._created_containers.append(self.plan.agent_name)
        self._run_with_env(command, secret_environment)

    def _guard_ready(self) -> bool:
        status = self._run_text(
            (
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                self.plan.guard_name,
            )
        ).strip()
        if status == "true healthy":
            checks = (
                ("--check-ready", "sandbox_guard_ready/v1"),
                ("--check-firewall", "sandbox_firewall_check/v1"),
                ("--check-gateway", "sandbox_gateway_check/v1"),
            )
            for flag, schema in checks:
                raw = self._run_text(
                    (
                        "docker",
                        "exec",
                        self.plan.guard_name,
                        "python3",
                        "-m",
                        "tradeevolve_guard.guard_main",
                        flag,
                    )
                )
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise PreflightFailure("guard preflight emitted invalid JSON") from exc
                if not isinstance(value, dict) or value.get("schema") != schema:
                    raise PreflightFailure("guard preflight proof is invalid")
            return True
        if status.startswith("false") or status.endswith("unhealthy"):
            diagnostic = "unknown"
            try:
                for line in reversed(
                    self._run_text(
                        ("docker", "logs", self.plan.guard_name)
                    ).splitlines()
                ):
                    value = json.loads(line)
                    if (
                        isinstance(value, dict)
                        and value.get("schema") == "sandbox_guard_error/v1"
                        and isinstance(value.get("stage"), str)
                    ):
                        diagnostic = cast(str, value["stage"])
                        break
            except (PreflightFailure, json.JSONDecodeError):
                pass
            raise PreflightFailure(
                "guard exited or became unhealthy during "
                f"{diagnostic} before agent start"
            )
        return False

    def _wait_until(self, predicate: Callable[[], bool], failure: str) -> None:
        deadline = self._monotonic() + self._startup_timeout_s
        while True:
            if predicate():
                return
            if self._monotonic() >= deadline:
                raise PreflightFailure(failure)
            self._sleep(0.1)

    def _agent_healthy(self) -> bool:
        if self._health_check("127.0.0.1", self.plan.host_http_port):
            return True
        running = self._run_text(
            (
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                self.plan.agent_name,
            )
        ).strip()
        if running != "true":
            raise PreflightFailure("agent exited before /healthz became ready")
        return False

    def start(self) -> SandboxHandle:
        if self._handle is not None or self._created_containers or self._created_networks:
            raise RuntimeError("sandbox lifecycle already started")
        self.plan.require_secret_source()
        GATEWAY_CONTEXT.audit()
        GUARD_CONTEXT.audit()
        secret_environment = _agent_secret_environment(self.plan)
        try:
            commands = self.plan.create_commands()
            self._run_create(commands[0], resource=self.plan.internal_network)
            self._run_create(commands[1], resource=self.plan.egress_network)
            self._run_create(commands[2], resource=self.plan.gateway_name)
            self._run_text(commands[3])
            self._run_create(commands[4], resource=self.plan.guard_name)
            self._run_text(commands[5])
            self._wait_until(
                self._guard_ready,
                "guard isolation did not become ready before timeout",
            )
            self._run_agent_create(
                commands[6],
                secret_environment=secret_environment,
            )
            self._wait_until(
                self._agent_healthy,
                "agent /healthz did not become ready before timeout",
            )
            snapshot = DockerRuntimePreflight(self._run_text).require_ready(self.plan)
            event_source = DockerEgressEventSource(
                guard_name=self.plan.guard_name,
                gateway_name=self.plan.gateway_name,
                run_text=self._run_text,
            )
            self._handle = SandboxHandle(
                base_url=f"http://127.0.0.1:{self.plan.host_http_port}",
                http_agent=HTTPAgent(
                    f"http://127.0.0.1:{self.plan.host_http_port}",
                    protected_response_values=tuple(
                        secret_environment.values()
                    ),
                ),
                event_source=event_source,
                snapshot=snapshot,
            )
            return self._handle
        except BaseException:
            self._cleanup_best_effort()
            raise

    def _owned_container(self, name: str) -> bool:
        try:
            label = self._run_text(
                (
                    "docker",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "tradeevolve.sandbox_id"}}',
                    name,
                )
            ).strip()
        except PreflightFailure:
            return False
        return label == self.plan.sandbox_id

    def _owned_network(self, name: str) -> bool:
        try:
            label = self._run_text(
                (
                    "docker",
                    "network",
                    "inspect",
                    "--format",
                    '{{index .Labels "tradeevolve.sandbox_id"}}',
                    name,
                )
            ).strip()
        except PreflightFailure:
            return False
        return label == self.plan.sandbox_id

    def _cleanup_best_effort(self) -> None:
        for name in reversed(self._created_containers):
            try:
                if self._owned_container(name):
                    self._run_text(("docker", "rm", "--force", name))
            except PreflightFailure:
                pass
        for name in reversed(self._created_networks):
            try:
                if self._owned_network(name):
                    self._run_text(("docker", "network", "rm", name))
            except PreflightFailure:
                pass
        self._created_containers.clear()
        self._created_networks.clear()
        self._handle = None

    def close(self) -> None:
        failures: list[str] = []
        for name in reversed(self._created_containers):
            try:
                if not self._owned_container(name):
                    failures.append(f"refused cleanup of unowned container {name}")
                    continue
                self._run_text(("docker", "rm", "--force", name))
            except PreflightFailure:
                failures.append(f"failed cleanup of container {name}")
        for name in reversed(self._created_networks):
            try:
                if not self._owned_network(name):
                    failures.append(f"refused cleanup of unowned network {name}")
                    continue
                self._run_text(("docker", "network", "rm", name))
            except PreflightFailure:
                failures.append(f"failed cleanup of network {name}")
        self._created_containers.clear()
        self._created_networks.clear()
        self._handle = None
        if failures:
            raise PreflightFailure("; ".join(failures))

    def __enter__(self) -> SandboxHandle:
        return self.start()

    def __exit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        self.close()
