# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sandbox.docker.guard.guard_main import _assert_runtime_directory


def test_runtime_directory_accepts_exact_owner_and_mode(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)

    _assert_runtime_directory(
        runtime,
        uid=os.getuid(),
        gid=os.getgid(),
    )


def test_runtime_directory_rejects_wrong_owner(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)

    with pytest.raises(
        RuntimeError,
        match="guard readiness directory owner is invalid",
    ):
        _assert_runtime_directory(
            runtime,
            uid=os.getuid() + 1,
            gid=os.getgid(),
        )


def test_runtime_directory_rejects_wrong_mode(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)

    with pytest.raises(
        RuntimeError,
        match="guard readiness directory mode is invalid",
    ):
        _assert_runtime_directory(
            runtime,
            uid=os.getuid(),
            gid=os.getgid(),
        )
