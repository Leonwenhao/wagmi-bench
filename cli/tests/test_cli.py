# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import importlib
import json
import shutil
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
from data.builder import BuiltPack
from harness.protocol import AgentReply
from recorder.replay import replay_bundle
from recorder.verify import verify_bundle

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PACK = ROOT / "fixtures" / "golden-mini" / "pack"


def _invoke(*args: str, stdin_text: str = "") -> tuple[int, str, str]:
    stdin = StringIO(stdin_text)
    stdout = StringIO()
    stderr = StringIO()
    code = main(args, stdin=stdin, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    "arguments",
    [
        ("--help",),
        ("wizard", "--help"),
        ("init", "--help"),
        ("fetch-data", "--help"),
        ("packs", "--help"),
        ("packs", "list", "--help"),
        ("run", "--help"),
        ("report", "--help"),
        ("compare", "--help"),
        ("score", "--help"),
        ("replay", "--help"),
        ("share", "--help"),
    ],
)
def test_every_command_has_help(arguments: tuple[str, ...]) -> None:
    code, stdout, stderr = _invoke(*arguments)

    assert code == 0
    assert "usage:" in stdout
    assert stderr == ""


def test_help_uses_the_wagmibench_console_script_name() -> None:
    """Usage lines name the installed console script, not the working name."""
    code, stdout, stderr = _invoke("--help")

    assert code == 0
    assert stderr == ""
    assert stdout.startswith("usage: wagmibench ")
    assert "tradeevolve" not in stdout.lower()


def test_usage_errors_are_nonzero_and_actionable() -> None:
    code, _, stderr = _invoke("run")

    assert code == EXIT_USAGE
    assert "error:" in stderr
    assert "--pack" in stderr
    assert "next:" in stderr
    assert "--help" in stderr
    assert "tradeevolve" not in stderr.lower()


def test_packs_list_is_deterministic_and_requires_no_network(
    tmp_path: Path,
) -> None:
    first = _invoke("packs", "list", "--packs-root", str(tmp_path))
    second = _invoke("packs", "list", "--packs-root", str(tmp_path))

    assert first == second
    code, stdout, stderr = first
    assert code == 0
    assert stderr == ""
    assert "covid-black-thursday" in stdout
    assert "10-10-cascade" in stdout
    assert stdout.count("not-fetched") == 13
    assert "Next:" in stdout


def test_init_creates_a_fresh_scaffold_and_never_overwrites(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "my-agent"

    code, stdout, stderr = _invoke("init", str(destination))

    assert code == 0
    assert stderr == ""
    assert (destination / "agent_adapter.py").is_file()
    assert (destination / "wagmibench.json").is_file()
    original = (destination / "agent_adapter.py").read_bytes()
    compile(original, str(destination / "agent_adapter.py"), "exec")
    assert "--agent http" in stdout
    assert "Next:" in stdout

    code, _, stderr = _invoke("init", str(destination))
    assert code == EXIT_INPUT
    assert "already exists" in stderr
    assert "next:" in stderr
    assert (destination / "agent_adapter.py").read_bytes() == original


def test_fetch_unknown_pack_fails_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cli.main")

    def forbidden_fetch(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("network-facing fetch must not run")

    monkeypatch.setattr(module, "acquire_pack", forbidden_fetch)
    code, _, stderr = _invoke(
        "fetch-data",
        "--pack",
        "not-a-real-pack",
        "--raw-root",
        str(tmp_path / "raw"),
        "--packs-root",
        str(tmp_path / "packs"),
    )

    assert code == EXIT_INPUT
    assert "unknown pack" in stderr
    assert "packs list" in stderr


def _stage_golden_pack(packs_root: Path, pack_id: str) -> BuiltPack:
    directory = packs_root / pack_id
    shutil.copytree(GOLDEN_PACK, directory)
    series = tuple(sorted(directory.glob("*.jsonl")))
    return BuiltPack(
        directory=directory,
        manifest_path=directory / "manifest.json",
        content_hash="sha256:fixture",
        series_paths=series,
    )


def test_fetch_hydrates_a_committed_manifest_without_overwriting_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    packs_module = importlib.import_module("cli.packs")
    pack_id = "covid-black-thursday"
    packs_root = tmp_path / "packs"
    target = packs_root / pack_id
    target.mkdir(parents=True)
    committed_bytes = (GOLDEN_PACK / "manifest.json").read_bytes()
    (target / "manifest.json").write_bytes(committed_bytes)

    def fake_fetch(
        requested: str,
        *,
        raw_root: Path,
        packs_root: Path,
    ) -> BuiltPack:
        del raw_root
        return _stage_golden_pack(packs_root, requested)

    monkeypatch.setattr(packs_module, "fetch_and_build_pack", fake_fetch)
    code, stdout, stderr = _invoke(
        "fetch-data",
        "--pack",
        pack_id,
        "--raw-root",
        str(tmp_path / "raw"),
        "--packs-root",
        str(packs_root),
    )

    assert code == 0, stderr
    assert "Local state: hydrated" in stdout
    assert (target / "manifest.json").read_bytes() == committed_bytes
    assert {path.name for path in target.glob("*.jsonl")} == {
        "bars_1h.jsonl",
        "funding.jsonl",
        "index_1h.jsonl",
        "mark_1h.jsonl",
    }

    code, stdout, stderr = _invoke(
        "packs",
        "list",
        "--packs-root",
        str(packs_root),
    )
    assert code == 0, stderr
    covid_row = next(
        line for line in stdout.splitlines() if line.startswith(pack_id)
    )
    assert covid_row.endswith("ready")


def test_fetch_refuses_manifest_drift_before_installing_series(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    packs_module = importlib.import_module("cli.packs")
    pack_id = "covid-black-thursday"
    packs_root = tmp_path / "packs"
    target = packs_root / pack_id
    target.mkdir(parents=True)
    committed = (GOLDEN_PACK / "manifest.json").read_bytes()
    (target / "manifest.json").write_bytes(committed)

    def drifting_fetch(
        requested: str,
        *,
        raw_root: Path,
        packs_root: Path,
    ) -> BuiltPack:
        del raw_root
        built = _stage_golden_pack(packs_root, requested)
        built.manifest_path.write_bytes(b'{"drift":true}')
        return built

    monkeypatch.setattr(packs_module, "fetch_and_build_pack", drifting_fetch)
    code, _, stderr = _invoke(
        "fetch-data",
        "--pack",
        pack_id,
        "--raw-root",
        str(tmp_path / "raw"),
        "--packs-root",
        str(packs_root),
    )

    assert code != 0
    assert "not byte-identical" in stderr
    assert (target / "manifest.json").read_bytes() == committed
    assert tuple(target.glob("*.jsonl")) == ()


@pytest.fixture
def complete_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--output",
        str(bundle),
    )
    assert code == 0, stderr
    assert "Survival verdict:" in stdout
    assert "Evidence root: sha256:" in stdout
    return bundle


def test_keyless_quickstart_run_records_and_replays_offline(
    complete_bundle: Path,
) -> None:
    code, stdout, stderr = _invoke(
        "replay",
        "--bundle",
        str(complete_bundle),
        "--pack",
        str(GOLDEN_PACK),
    )

    assert code == 0
    assert stderr == ""
    assert "Replay matched:" in stdout
    assert "Evidence root: sha256:" in stdout
    assert "ledger_stress_2x.jsonl" in stdout


@pytest.mark.parametrize(
    ("agent_kind", "expected_target"),
    [("buyhold", 10_000), ("shorthold", -10_000), ("flat", 0)],
)
def test_baseline_agents_run_keyless_and_seal_their_policy(
    tmp_path: Path,
    agent_kind: str,
    expected_target: int,
) -> None:
    bundle = tmp_path / f"bundle-{agent_kind}"
    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        agent_kind,
        "--output",
        str(bundle),
    )

    assert code == 0
    assert stderr == ""
    assert "Evidence root: sha256:" in stdout
    manifest = json.loads((bundle / "agent_manifest.json").read_text("utf-8"))
    assert manifest["name"] == f"{agent_kind}-baseline"
    assert manifest["model_id"] == "none"
    assert manifest["inference_params"] == {
        "policy": "constant-target",
        "target_lev_1e4": expected_target,
    }


def test_compare_renders_baseline_table_and_requires_two_bundles(
    tmp_path: Path,
) -> None:
    flat_bundle = tmp_path / "flat"
    hold_bundle = tmp_path / "hold"
    for agent_kind, bundle in (("flat", flat_bundle), ("buyhold", hold_bundle)):
        code, _stdout, stderr = _invoke(
            "run",
            "--pack",
            str(GOLDEN_PACK),
            "--agent",
            agent_kind,
            "--output",
            str(bundle),
        )
        assert code == 0, stderr

    code, stdout, stderr = _invoke(
        "compare",
        "--bundle",
        str(hold_bundle),
        "--bundle",
        str(flat_bundle),
    )
    assert code == 0
    assert stderr == ""
    assert "WAGMI BENCH COMPARISON" in stdout
    assert "SURVIVED — FLAT-HOLD" in stdout
    assert "claim_label:" in stdout

    code, _stdout, stderr = _invoke("compare", "--bundle", str(flat_bundle))
    assert code != 0
    assert "at least two" in stderr


def test_scaffold_compatible_http_adapter_runs_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cli.main")

    class OfflineHTTPAgent:
        def __init__(self, origin: str) -> None:
            assert origin == "http://127.0.0.1:8000"

        def decide(self, request: dict[str, object]) -> AgentReply:
            del request
            return AgentReply(
                b'{"schema":"action/v1","intent_kind":"leverage_target",'
                b'"target":{"BTC":"0"},"comment":"offline HTTP test policy"}'
            )

    monkeypatch.setattr(module, "HTTPAgent", OfflineHTTPAgent)
    bundle = tmp_path / "http-bundle"
    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "http",
        "--agent-url",
        "http://127.0.0.1:8000",
        "--response-deadline-ms",
        "5000",
        "--output",
        str(bundle),
    )

    assert code == 0, stderr
    assert "Run complete:" in stdout
    assert "Evidence root: sha256:" in stdout
    assert (bundle / "chain.json").is_file()


def test_run_refuses_to_overwrite_an_evidence_bundle(
    complete_bundle: Path,
) -> None:
    code, _, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--output",
        str(complete_bundle),
    )

    assert code == EXIT_INPUT
    assert "already exists" in stderr
    assert "immutable" in stderr


def test_llm_decline_gate_prevents_adapter_contact_and_bundle_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cli.main")
    decisions = 0

    class ForbiddenHTTPAgent:
        def __init__(self, origin: str) -> None:
            del origin

        def decide(self, request: dict[str, object]) -> object:
            nonlocal decisions
            del request
            decisions += 1
            raise AssertionError("HTTP decision requested before confirmation")

    monkeypatch.setattr(module, "HTTPAgent", ForbiddenHTTPAgent)
    bundle = tmp_path / "llm-bundle"
    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm",
        "--agent-url",
        "http://127.0.0.1:8000",
        "--model",
        "accounts/fireworks/models/qwen3p7-plus",
        "--output",
        str(bundle),
        stdin_text="no\n",
    )

    assert code == EXIT_CONFIRMATION
    assert "no request sent" in stdout
    assert "estimated maximum cost: $" in stdout
    assert "cancelled before any decision request or spending" in stderr
    assert "next:" in stderr
    assert decisions == 0
    assert not bundle.exists()


def test_confirmed_llm_path_refuses_missing_credential_guard_before_contact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cli.main")
    constructions = 0

    class ForbiddenHTTPAgent:
        def __init__(self, origin: str, **kwargs: object) -> None:
            nonlocal constructions
            del origin, kwargs
            constructions += 1

    monkeypatch.setattr(module, "HTTPAgent", ForbiddenHTTPAgent)
    bundle = tmp_path / "unguarded-llm"
    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm",
        "--agent-url",
        "http://127.0.0.1:8000",
        "--model",
        "accounts/fireworks/models/qwen3p7-plus",
        "--credential-file",
        str(tmp_path / "missing.env"),
        "--output",
        str(bundle),
        "--confirm-spend",
    )

    assert code == EXIT_INPUT
    assert "Explicit spend confirmation accepted via --confirm-spend." in stdout
    assert "credential protection is not ready" in stderr
    assert "value is fingerprinted and never recorded" in stderr
    assert constructions == 0
    assert not bundle.exists()


def test_confirmed_llm_path_fingerprints_declared_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cli.main")
    secret = "TE_SYNTHETIC_SECRET_CANARY_cli_91c2"
    credential_file = tmp_path / ".env"
    credential_file.write_text(
        f"CUSTOM_PROVIDER_KEY={secret}\n",
        encoding="utf-8",
    )
    protected: list[tuple[str | bytes, ...]] = []

    class OfflineProtectedHTTPAgent:
        def __init__(
            self,
            origin: str,
            *,
            protected_response_values: tuple[str | bytes, ...],
        ) -> None:
            assert origin == "http://127.0.0.1:8000"
            protected.append(protected_response_values)

        def decide(self, request: dict[str, object]) -> AgentReply:
            del request
            return AgentReply(
                b'{"schema":"action/v1","intent_kind":"leverage_target",'
                b'"target":{"BTC":"0"},"comment":"safe offline response"}'
            )

    monkeypatch.setattr(module, "HTTPAgent", OfflineProtectedHTTPAgent)
    bundle = tmp_path / "protected-llm"
    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm",
        "--agent-url",
        "http://127.0.0.1:8000",
        "--model",
        "accounts/fireworks/models/qwen3p7-plus",
        "--credential-file",
        str(credential_file),
        "--credential-env-name",
        "CUSTOM_PROVIDER_KEY",
        "--output",
        str(bundle),
        "--confirm-spend",
    )

    assert code == 0, stderr
    assert "Run complete:" in stdout
    assert protected == [(secret,)]
    assert secret.encode("utf-8") not in b"".join(
        path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    )


def test_unknown_llm_pricing_fails_before_adapter_contact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cli.main")
    decisions = 0

    class ForbiddenHTTPAgent:
        def __init__(self, origin: str) -> None:
            del origin

        def decide(self, request: dict[str, object]) -> AgentReply:
            nonlocal decisions
            del request
            decisions += 1
            raise AssertionError("HTTP adapter must not be contacted")

    monkeypatch.setattr(module, "HTTPAgent", ForbiddenHTTPAgent)
    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm",
        "--agent-url",
        "http://127.0.0.1:8000",
        "--model",
        "unknown/model",
        "--output",
        str(tmp_path / "never-created"),
    )

    assert code == EXIT_INPUT
    assert stdout == ""
    assert "no pinned CLI pricing row" in stderr
    assert "both token prices" in stderr
    assert decisions == 0


def test_generic_http_mode_cannot_bypass_priced_model_gate(
    tmp_path: Path,
) -> None:
    code, _, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "http",
        "--agent-url",
        "http://127.0.0.1:8000",
        "--model",
        "paid/model",
        "--output",
        str(tmp_path / "never-created"),
    )

    assert code == EXIT_USAGE
    assert "priced model adapters must use --agent llm" in stderr
    assert "estimates cost" in stderr


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


def test_local_llm_decline_gate_prevents_any_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, built = _fake_local_provider(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "TE_SYNTHETIC_LOCAL_CANARY_decline")
    bundle = tmp_path / "declined-local"

    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm-local",
        "--llm-provider",
        "anthropic",
        "--model",
        "claude-opus-5",
        "--max-output-tokens",
        "2000",
        "--output",
        str(bundle),
        stdin_text="no\n",
    )

    assert code == EXIT_CONFIRMATION
    assert "no request sent" in stdout
    assert "estimated maximum cost: $" in stdout
    assert "cancelled before any decision request or spending" in stderr
    assert provider.calls == 0
    assert built == []
    assert not bundle.exists()


def test_local_llm_run_seals_an_in_process_manifest_without_the_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, built = _fake_local_provider(monkeypatch)
    secret = "TE_SYNTHETIC_LOCAL_CANARY_7f31"
    credential_file = tmp_path / ".env"
    credential_file.write_text(
        f"ANTHROPIC_API_KEY={secret}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bundle = tmp_path / "local-llm"

    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm-local",
        "--llm-provider",
        "anthropic",
        "--model",
        "claude-opus-5",
        "--max-output-tokens",
        "2000",
        "--credential-file",
        str(credential_file),
        "--output",
        str(bundle),
        "--confirm-spend",
    )

    assert code == 0, stderr
    assert "Explicit spend confirmation accepted via --confirm-spend." in stdout
    assert "Run complete:" in stdout
    assert built == [("ANTHROPIC_API_KEY", secret)]
    assert provider.calls > 0

    manifest = json.loads(
        (bundle / "agent_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "llm-local"
    assert manifest["adapter"] == "in_process"
    assert manifest["model_id"] == "claude-opus-5"
    assert manifest["endpoint_domains"] == ["api.anthropic.com"]
    assert manifest["inference_params"] == {
        "max_tokens": 2000,
        "response_format": "json_schema",
        "provider_api": "anthropic-messages",
    }
    assert manifest["prompt_sha256"].startswith("sha256:")
    assert manifest["image_sha256"] is None
    assert secret.encode("utf-8") not in b"".join(
        path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    )
    assert verify_bundle(bundle).is_complete


def test_local_llm_reads_the_key_from_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _provider, built = _fake_local_provider(monkeypatch)
    secret = "TE_SYNTHETIC_LOCAL_CANARY_env"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    bundle = tmp_path / "env-local-llm"

    code, _stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm-local",
        "--llm-provider",
        "anthropic",
        "--model",
        "claude-opus-5",
        "--max-output-tokens",
        "2000",
        "--credential-file",
        str(tmp_path / "absent.env"),
        "--output",
        str(bundle),
        "--confirm-spend",
    )

    assert code == 0, stderr
    assert built == [("ANTHROPIC_API_KEY", secret)]


def test_local_llm_missing_key_fails_before_the_estimate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, built = _fake_local_provider(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bundle = tmp_path / "keyless-local"

    code, stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm-local",
        "--llm-provider",
        "anthropic",
        "--model",
        "claude-opus-5",
        "--max-output-tokens",
        "2000",
        "--credential-file",
        str(tmp_path / "absent.env"),
        "--output",
        str(bundle),
        "--confirm-spend",
    )

    assert code == EXIT_INPUT
    assert stdout == ""
    assert "ANTHROPIC_API_KEY credential is not available" in stderr
    assert "never printed, manifested, or recorded" in stderr
    assert provider.calls == 0
    assert built == []
    assert not bundle.exists()


def test_local_llm_rejects_an_adapter_url(tmp_path: Path) -> None:
    code, _stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm-local",
        "--agent-url",
        "http://127.0.0.1:8000",
        "--model",
        "claude-opus-5",
        "--output",
        str(tmp_path / "never-created"),
    )

    assert code == EXIT_USAGE
    assert "--agent-url is not valid with --agent llm-local" in stderr
    assert "runs the model policy in-process" in stderr


def test_local_llm_refuses_a_provider_domain_it_would_not_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("TRADEVOLVE_LLM_BASE_URL", raising=False)
    code, _stdout, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--agent",
        "llm-local",
        "--llm-provider",
        "anthropic",
        "--provider-domain",
        "api.openai.com",
        "--model",
        "claude-opus-5",
        "--output",
        str(tmp_path / "never-created"),
    )

    assert code == EXIT_USAGE
    assert "does not match the in-process provider origin" in stderr
    assert "seals exactly the origin it calls" in stderr


def test_report_and_share_sub_bundle_use_only_the_complete_bundle(
    complete_bundle: Path,
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "report"
    code, stdout, stderr = _invoke(
        "report",
        "--bundle",
        str(complete_bundle),
        "--output",
        str(report_dir),
    )

    assert code == 0, stderr
    assert "Report directory:" in stdout
    assert {path.name for path in report_dir.iterdir()} == {
        "report.txt",
        "report.html",
        "share-card.svg",
    }
    assert "claim_label: survival-stress" in (
        report_dir / "report.txt"
    ).read_text(encoding="utf-8")

    parent_receipt = verify_bundle(complete_bundle)
    parent_raw = sorted((complete_bundle / "raw").glob("*.txt"))
    assert parent_receipt.is_complete
    assert parent_raw
    share_path = tmp_path / "share-bundle"
    code, stdout, stderr = _invoke(
        "share",
        "--bundle",
        str(complete_bundle),
        "--output",
        str(share_path),
    )

    assert code == 0, stderr
    assert "Verifier verdict: COMPLETE" in stdout
    assert "Claim label: survival-stress" in stdout
    assert (share_path / "redaction.json").is_file()
    assert list((share_path / "raw").glob("*.txt")) == []
    redaction = json.loads(
        (share_path / "redaction.json").read_text(encoding="utf-8")
    )
    assert redaction["parent_root"] == parent_receipt.root
    assert len(redaction["removals"]) == len(parent_raw)
    share_receipt = verify_bundle(share_path)
    assert share_receipt.is_complete
    assert share_receipt.root == parent_receipt.root
    replay = replay_bundle(share_path, pack_dir=GOLDEN_PACK)
    assert replay.bundle_root == parent_receipt.root


def test_report_refuses_an_unsealed_directory(
    tmp_path: Path,
) -> None:
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()

    code, _, stderr = _invoke(
        "report",
        "--bundle",
        str(incomplete),
        "--output",
        str(tmp_path / "report"),
    )

    assert code != 0
    assert "not COMPLETE" in stderr
    assert "next:" in stderr


def test_momentum_path_rejects_spend_confirmation_flag(
    tmp_path: Path,
) -> None:
    code, _, stderr = _invoke(
        "run",
        "--pack",
        str(GOLDEN_PACK),
        "--output",
        str(tmp_path / "bundle"),
        "--confirm-spend",
    )

    assert code == EXIT_USAGE
    assert "keyless adapter" in stderr
