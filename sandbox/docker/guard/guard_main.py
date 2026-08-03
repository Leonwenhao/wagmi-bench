# SPDX-License-Identifier: Apache-2.0
"""Install namespace rules, drop privilege, and collect blocked attempts."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Mapping

from .block_collector import BlockCollector
from .firewall_runtime import FirewallPlan

_READY_PATH = Path("/run/tradeevolve/ready.json")
_COLLECTOR_UID = 65533
_COLLECTOR_GID = 65533


def _integer_env(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name, str(default))
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(f"{name} must be decimal")
    return int(raw)


def _plan_from_env(environment: Mapping[str, str]) -> FirewallPlan:
    gateway = environment.get("GATEWAY_IPV4")
    if gateway is None:
        raise ValueError("GATEWAY_IPV4 is required")
    return FirewallPlan(
        agent_uid=_integer_env(environment, "AGENT_UID", 65532),
        gateway_ipv4=gateway,
        agent_http_port=_integer_env(environment, "AGENT_HTTP_PORT", 8000),
        gateway_port=_integer_env(environment, "GATEWAY_PORT", 3128),
        collector_tcp_port=_integer_env(
            environment,
            "COLLECTOR_TCP_PORT",
            15001,
        ),
        collector_udp_port=_integer_env(
            environment,
            "COLLECTOR_UDP_PORT",
            15002,
        ),
    )


def _install(plan: FirewallPlan) -> None:
    subprocess.run(
        ["iptables-restore", "--wait", "5", "--noflush"],
        input=plan.restore_text().encode("ascii"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _verify(plan: FirewallPlan) -> None:
    for command in plan.check_commands():
        subprocess.run(
            list(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )


def _effective_capabilities_hex() -> str:
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        if line.startswith("CapEff:"):
            return line.split(":", 1)[1].strip().lower()
    raise RuntimeError("CapEff missing from /proc/self/status")


def _assert_ipv6_disabled(proc_root: Path = Path("/proc")) -> None:
    """Prove the namespace has neither IPv6 interfaces nor routes.

    The checked-in guard currently collects IPv4 TCP/UDP redirects only.  It
    must therefore refuse readiness unless Docker's namespace sysctls removed
    every non-loopback IPv6 path before the agent joins.
    """

    for relative in (
        "sys/net/ipv6/conf/all/disable_ipv6",
        "sys/net/ipv6/conf/default/disable_ipv6",
    ):
        if (proc_root / relative).read_text(encoding="ascii").strip() != "1":
            raise RuntimeError("IPv6 disable sysctl is not active")

    interface_file = proc_root / "net/if_inet6"
    if interface_file.exists():
        for line in interface_file.read_text(encoding="ascii").splitlines():
            fields = line.split()
            if fields and fields[-1] != "lo":
                raise RuntimeError("non-loopback IPv6 interface remains")

    route_file = proc_root / "net/ipv6_route"
    if route_file.exists():
        for line in route_file.read_text(encoding="ascii").splitlines():
            fields = line.split()
            if fields and fields[-1] != "lo":
                raise RuntimeError("non-loopback IPv6 route remains")


def _drop_privileges() -> None:
    os.setgroups([])
    os.setgid(_COLLECTOR_GID)
    os.setuid(_COLLECTOR_UID)
    if os.geteuid() != _COLLECTOR_UID:
        raise RuntimeError("guard failed to drop uid")
    if int(_effective_capabilities_hex(), 16) != 0:
        raise RuntimeError("guard retained effective capabilities")


def _assert_runtime_directory(
    path: Path = _READY_PATH.parent,
    *,
    uid: int = _COLLECTOR_UID,
    gid: int = _COLLECTOR_GID,
) -> None:
    """Fail closed unless Docker provisioned the readiness tmpfs exactly."""

    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    metadata = path.stat()
    if metadata.st_uid != uid or metadata.st_gid != gid:
        raise RuntimeError("guard readiness directory owner is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o755:
        raise RuntimeError("guard readiness directory mode is invalid")


def _ready_record(plan: FirewallPlan) -> dict[str, object]:
    return {
        "schema": "sandbox_guard_ready/v1",
        "ready": True,
        "firewall_sha256": plan.sha256(),
        "collector_tcp_port": plan.collector_tcp_port,
        "collector_udp_port": plan.collector_udp_port,
        "ipv6_disabled": True,
        "euid": os.geteuid(),
        "cap_eff_hex": _effective_capabilities_hex(),
    }


def _write_ready(record: dict[str, object]) -> None:
    _READY_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = json.dumps(
        record,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    temporary = _READY_PATH.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    # The record is secret-free and must be readable by Docker's root
    # healthcheck process after the main process has dropped every capability.
    os.chmod(temporary, 0o444)
    os.replace(temporary, _READY_PATH)


def _read_ready() -> dict[str, object]:
    value = json.loads(_READY_PATH.read_text(encoding="ascii"))
    if not isinstance(value, dict):
        raise ValueError("guard readiness is not an object")
    return value


def _check_ready() -> int:
    try:
        value = _read_ready()
    except (OSError, ValueError, json.JSONDecodeError):
        return 1
    if (
        value.get("schema") != "sandbox_guard_ready/v1"
        or value.get("ready") is not True
        or value.get("euid") != _COLLECTOR_UID
        or value.get("cap_eff_hex") != "0000000000000000"
        or value.get("ipv6_disabled") is not True
    ):
        return 1
    print(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _check_firewall() -> int:
    try:
        plan = _plan_from_env(os.environ)
        _verify(plan)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return 1
    print(
        json.dumps(
            {
                "schema": "sandbox_firewall_check/v1",
                "ok": True,
                "firewall_sha256": plan.sha256(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _check_gateway() -> int:
    try:
        plan = _plan_from_env(os.environ)
        connection = socket.create_connection(
            (plan.gateway_ipv4, plan.gateway_port),
            timeout=1.0,
        )
        connection.close()
    except (OSError, ValueError):
        return 1
    print(
        json.dumps(
            {
                "schema": "sandbox_gateway_check/v1",
                "ok": True,
                "gateway_ipv4": plan.gateway_ipv4,
                "gateway_port": plan.gateway_port,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _serve() -> int:
    stage = "configuration"
    try:
        plan = _plan_from_env(os.environ)
        stage = "firewall_install"
        _install(plan)
        stage = "firewall_verify"
        _verify(plan)
        stage = "ipv6_proof"
        _assert_ipv6_disabled()
        stage = "runtime_directory"
        _assert_runtime_directory()
        stage = "privilege_drop"
        _drop_privileges()
        stage = "collector_start"
        collector = BlockCollector(
            tcp_port=plan.collector_tcp_port,
            udp_port=plan.collector_udp_port,
        )
        collector.start()
        stage = "ready_record"
        _write_ready(_ready_record(plan))
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(
            json.dumps(
                {
                    "schema": "sandbox_guard_error/v1",
                    "stage": stage,
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
            flush=True,
        )
        return 1

    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        stopped.wait()
    finally:
        collector.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeEvolve namespace guard")
    parser.add_argument("--check-ready", action="store_true")
    parser.add_argument("--check-firewall", action="store_true")
    parser.add_argument("--check-gateway", action="store_true")
    args = parser.parse_args(argv)
    selected = sum(
        (args.check_ready, args.check_firewall, args.check_gateway),
    )
    if selected > 1:
        return 2
    if args.check_ready:
        return _check_ready()
    if args.check_firewall:
        return _check_firewall()
    if args.check_gateway:
        return _check_gateway()
    return _serve()


if __name__ == "__main__":
    raise SystemExit(main())
