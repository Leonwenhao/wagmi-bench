# SPDX-License-Identifier: Apache-2.0
"""Guided-mode flows, driven end to end through scripted stdin."""

from __future__ import annotations

import importlib
import json
import os
from io import StringIO
from pathlib import Path

import pytest

from agents.llm import LLMConfig, ProviderResult
from cli.main import (
    EXIT_CONFIRMATION,
    EXIT_INPUT,
    EXIT_USAGE,
    main,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PACK = ROOT / "fixtures" / "golden-mini" / "pack"
EXAMPLE_SKILLS = ROOT / "examples" / "evoskill" / ".claude" / "skills"


class _TerminalInput(StringIO):
    """A scripted stdin that claims to be an operator's terminal."""

    def isatty(self) -> bool:
        return True


def _invoke(
    *args: str,
    stdin_text: str = "",
    tty: bool = False,
) -> tuple[int, str, str]:
    stdin = _TerminalInput(stdin_text) if tty else StringIO(stdin_text)
    stdout = StringIO()
    stderr = StringIO()
    code = main(args, stdin=stdin, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_wizard_has_its_own_help() -> None:
    code, stdout, stderr = _invoke("wizard", "--help")

    assert code == 0
    assert stderr == ""
    assert "usage: wagmibench wizard" in stdout
    assert "equivalent non-interactive command" in stdout


def test_wizard_is_listed_in_the_top_level_help() -> None:
    code, stdout, _stderr = _invoke("--help")

    assert code == 0
    assert "wizard" in stdout


def test_bare_invocation_without_a_terminal_keeps_the_usage_error() -> None:
    code, stdout, stderr = _invoke()

    assert code == EXIT_USAGE
    assert stdout == ""
    assert "error:" in stderr
    assert "next:" in stderr
    assert "WAGMI Bench guided mode" not in stdout


def test_bare_invocation_at_a_terminal_opens_the_wizard() -> None:
    code, stdout, stderr = _invoke(stdin_text="q\n", tty=True)

    assert code == 0
    assert stderr == ""
    assert "WAGMI Bench guided mode" in stdout
    assert "Claim label: survival-stress" in stdout
    assert "1) Quick demo" in stdout
    assert "Nothing was run." in stdout


def test_invalid_menu_input_reprompts_three_times_then_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    code, stdout, _stderr = _invoke("wizard", stdin_text="x\n9\nnope\n")

    assert code == EXIT_USAGE
    assert stdout.count("is not a listed choice") == 3
    assert "Too many unrecognized answers." in stdout
    assert list(tmp_path.iterdir()) == []


def test_end_of_input_leaves_the_wizard_without_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    code, stdout, stderr = _invoke("wizard", stdin_text="")

    assert code == 0
    assert stderr == ""
    assert "Input ended; leaving guided mode." in stdout
    assert "Traceback" not in stdout
    assert list(tmp_path.iterdir()) == []


def test_demo_flow_seals_a_bundle_and_report_and_shows_the_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    code, stdout, stderr = _invoke("wizard", stdin_text="1\n")

    assert code == 0, stderr
    assert stderr == ""
    assert "Equivalent command: uv run wagmibench run --pack" in stdout
    assert "Equivalent command: uv run wagmibench report --bundle" in stdout
    assert "Run complete:" in stdout
    assert "Evidence root: sha256:" in stdout
    bundle = tmp_path / "bundles" / "wizard-demo"
    report = tmp_path / "reports" / "wizard-demo"
    assert (bundle / "chain.json").is_file()
    assert (report / "report.html").is_file()
    assert "Open your report: reports/wizard-demo/report.html" in stdout


def test_demo_flow_never_reuses_an_existing_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    first, _stdout, stderr = _invoke("wizard", stdin_text="1\n")
    assert first == 0, stderr
    second, stdout, stderr = _invoke("wizard", stdin_text="1\n")

    assert second == 0, stderr
    assert (tmp_path / "bundles" / "wizard-demo-2" / "chain.json").is_file()
    assert (tmp_path / "reports" / "wizard-demo-2" / "report.html").is_file()
    assert "wizard-demo-2" in stdout


class _OfflineProvider:
    """Provider seam standing in for a paid completion; never leaves memory."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        *,
        system_prompt: str,
        observation: dict[str, object],
        retry_feedback: dict[str, object] | None,
    ) -> ProviderResult:
        assert system_prompt
        del retry_feedback
        self.calls += 1
        markets = observation["markets"]
        assert isinstance(markets, dict)
        target = json.dumps({alias: "0" for alias in markets})
        return ProviderResult(
            content=(
                '{"schema":"action/v1","intent_kind":"leverage_target",'
                f'"target":{target},"max_slippage_bps":25}}'
            ),
            input_tokens=311,
            output_tokens=29,
        )


def _fake_local_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_OfflineProvider, list[tuple[str, str]]]:
    """Replace the provider factory and capture the config it receives."""

    module = importlib.import_module("cli.main")
    provider = _OfflineProvider()
    built: list[tuple[str, str]] = []

    def build_provider(
        config: LLMConfig,
        environ: dict[str, str] | None = None,
    ) -> _OfflineProvider:
        assert environ is not None
        name = config.api_key_env_name
        built.append((name, environ[name]))
        return provider

    monkeypatch.setattr(module, "build_provider", build_provider)
    monkeypatch.delenv("TRADEVOLVE_LLM_BASE_URL", raising=False)
    return provider, built


def test_hosted_model_flow_runs_reports_and_compares_baselines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, built = _fake_local_provider(monkeypatch)
    secret = "TE_SYNTHETIC_WIZARD_CANARY_env"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.chdir(tmp_path)

    # menu, provider, pinned model, golden-mini pack, default tokens,
    # spend confirmation, then the free keyless comparison.
    code, stdout, stderr = _invoke(
        "wizard",
        stdin_text="2\n1\n2\n0\n\nyes\ny\n",
    )

    assert code == 0, stderr
    assert "Using ANTHROPIC_API_KEY from the environment." in stdout
    assert (
        "Equivalent command: uv run wagmibench run --pack" in stdout
        and "--agent llm-local --llm-provider anthropic "
        "--model claude-opus-5" in stdout
    )
    assert "Pre-run LLM cost estimate (no request sent):" in stdout
    assert "estimated maximum cost: $" in stdout
    assert built == [("ANTHROPIC_API_KEY", secret)]
    assert provider.calls > 0

    bundle = tmp_path / "bundles" / "wizard-golden-mini"
    manifest = json.loads(
        (bundle / "agent_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "llm-local"
    assert manifest["model_id"] == "claude-opus-5"
    assert manifest["inference_params"]["max_tokens"] == 2000
    assert (tmp_path / "reports" / "wizard-golden-mini" / "report.html").is_file()

    for agent in ("buyhold", "shorthold", "flat", "momentum"):
        assert (
            tmp_path / "bundles" / f"wizard-golden-mini-{agent}" / "chain.json"
        ).is_file()
    assert "WAGMI BENCH COMPARISON" in stdout
    assert "TIER" in stdout
    compare_dir = tmp_path / "reports" / "wizard-golden-mini-compare"
    assert (compare_dir / "compare.txt").is_file()
    assert secret not in stdout


def test_hosted_model_decline_at_the_estimate_writes_no_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, built = _fake_local_provider(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "TE_SYNTHETIC_WIZARD_decline")
    monkeypatch.chdir(tmp_path)

    code, stdout, stderr = _invoke(
        "wizard",
        stdin_text="2\n1\n1\n0\n\nno\n",
    )

    assert code == EXIT_CONFIRMATION
    assert "no request sent" in stdout
    assert "cancelled before any decision request or spending" in stderr
    assert provider.calls == 0
    assert built == []
    assert not (tmp_path / "bundles").exists()


def test_hosted_model_flow_asks_for_prices_when_no_row_is_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _built = _fake_local_provider(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "TE_SYNTHETIC_WIZARD_openai")
    monkeypatch.chdir(tmp_path)

    # openai is provider 3; it pins no price row, so both prices are asked
    # for before the estimate, and the run is declined at the gate.
    code, stdout, _stderr = _invoke(
        "wizard",
        stdin_text="2\n3\ngpt-example\n1.25\n10\n0\n\nno\n",
    )

    assert code == EXIT_CONFIRMATION
    assert "pins no reference price row for openai" in stdout
    assert "--input-usd-per-million 1.25 --output-usd-per-million 10" in stdout
    assert "prices per 1M tokens: input $1.25, output $10" in stdout
    assert provider.calls == 0


def test_hosted_model_flow_offers_to_persist_a_typed_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _built = _fake_local_provider(monkeypatch)
    environ = dict(os.environ)
    environ.pop("ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(os, "environ", environ)
    monkeypatch.chdir(tmp_path)
    secret = "TE_SYNTHETIC_WIZARD_typed_key"

    code, stdout, _stderr = _invoke(
        "wizard",
        stdin_text=f"2\n1\n1\n{secret}\ny\n0\n\nno\n",
    )

    assert code == EXIT_CONFIRMATION
    assert "No ANTHROPIC_API_KEY in the environment" in stdout
    assert "will be ECHOED in this terminal" in stdout
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        f"ANTHROPIC_API_KEY={secret}\n"
    )
    assert environ["ANTHROPIC_API_KEY"] == secret
    assert "Appended ANTHROPIC_API_KEY to .env" in stdout
    assert provider.calls == 0


def test_own_agent_scaffold_prints_the_handler_next_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    code, stdout, stderr = _invoke("wizard", stdin_text="3\na\nmy-agent\n")

    assert code == 0, stderr
    assert "Equivalent command: uv run wagmibench init my-agent" in stdout
    assert (tmp_path / "my-agent" / "agent_adapter.py").is_file()
    assert "--agent http" in stdout
    assert "Next:" in stdout


def test_evoskill_path_reports_a_format_error_actionably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    empty = tmp_path / "not-skills"
    empty.mkdir()

    code, stdout, stderr = _invoke(
        "wizard",
        stdin_text=f"3\nc\n{empty}\n",
    )

    assert code == EXIT_INPUT
    assert stderr == ""
    assert "error: " in stdout
    assert "contains no skill directories" in stdout
    assert "next: point at the `.claude/skills` folder" in stdout
    assert "Terminal 1" not in stdout


def test_evoskill_path_prints_two_terminal_instructions_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    code, stdout, stderr = _invoke(
        "wizard",
        stdin_text=f"3\nc\n{EXAMPLE_SKILLS}\n",
    )

    assert code == 0, stderr
    assert "perps-leverage-discipline" in stdout
    assert "Terminal 1 — start the adapter:" in stdout
    assert "export TRADEVOLVE_AGENT_MODE=evoskill" in stdout
    assert "uv run python -m agents.server" in stdout
    assert "Terminal 2" in stdout
    assert "--agent llm --agent-url http://127.0.0.1:8000" in stdout
    # Print-only: no server is started and no bundle is written.
    assert not (tmp_path / "bundles").exists()


def test_compare_flow_renders_a_table_over_two_bundles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    for agent in ("flat", "buyhold"):
        code, _stdout, stderr = _invoke(
            "run",
            "--pack",
            str(GOLDEN_PACK),
            "--agent",
            agent,
            "--output",
            f"bundles/{agent}",
        )
        assert code == 0, stderr

    code, stdout, stderr = _invoke(
        "wizard",
        stdin_text="4\nbundles/missing\nbundles/flat\nbundles/buyhold\n\n",
    )

    assert code == 0, stderr
    assert "No such bundle directory: bundles/missing" in stdout
    assert (
        "Equivalent command: uv run wagmibench compare "
        "--bundle bundles/flat --bundle bundles/buyhold" in stdout
    )
    assert "WAGMI BENCH COMPARISON" in stdout
    assert "SURVIVED — FLAT-HOLD" in stdout
    assert "claim_label:" in stdout


def test_compare_flow_reprompts_until_two_bundles_are_listed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    code, stdout, _stderr = _invoke("wizard", stdin_text="4\n\n\n\n")

    assert code == EXIT_USAGE
    assert stdout.count("needs at least two existing bundle") == 3
    assert "Too few bundles were listed." in stdout
