# SPDX-License-Identifier: Apache-2.0
"""Deterministic, deliberately small agent-adapter scaffold."""

from __future__ import annotations

import json
from pathlib import Path


class ScaffoldError(RuntimeError):
    """A scaffold cannot be created without overwriting user material."""


_AGENT_SOURCE = '''"""Edit this policy, then run it as a local IC-6 adapter."""

from __future__ import annotations

from collections.abc import Mapping

from agents.server import make_server


class Policy:
    """Example flat policy; replace only the decision logic."""

    def decide(self, request: Mapping[str, object]) -> dict[str, object]:
        observation = request.get("observation")
        if not isinstance(observation, Mapping):
            raise ValueError("request has no observation")
        markets = observation.get("markets")
        if not isinstance(markets, Mapping):
            raise ValueError("observation has no markets")
        return {
            "schema": "action/v1",
            "intent_kind": "leverage_target",
            "target": {str(alias): "0" for alias in sorted(markets)},
            "comment": "replace this flat scaffold policy",
        }


if __name__ == "__main__":
    server = make_server(Policy(), host="127.0.0.1", port=8000)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
'''

_CONFIG: dict[str, object] = {
    "schema": "tradeevolve_cli_config/v1",
    "agent": {
        "kind": "http",
        "origin": "http://127.0.0.1:8000",
    },
    "run": {
        "pack": "covid-black-thursday",
        "lookback_bars": None,
        "funding_prints": None,
    },
    "pricing": {
        "model": "",
        "input_usd_per_million": "",
        "output_usd_per_million": "",
    },
}


def create_scaffold(directory: Path) -> tuple[Path, Path]:
    """Create a fresh adapter source file and secret-free config."""

    if directory.exists():
        raise ScaffoldError(
            f"destination already exists: {directory}"
        )
    try:
        directory.mkdir(parents=True)
        source_path = directory / "agent_adapter.py"
        config_path = directory / "wagmibench.json"
        source_path.write_text(_AGENT_SOURCE, encoding="utf-8")
        config_path.write_text(
            json.dumps(
                _CONFIG,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ScaffoldError(
            f"could not create scaffold at {directory}: {exc}"
        ) from exc
    return source_path, config_path
