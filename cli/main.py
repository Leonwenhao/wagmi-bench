# SPDX-License-Identifier: Apache-2.0
"""Actionable, dependency-light command line for the V1 benchmark."""

from __future__ import annotations

import argparse
import os
import platform
import secrets
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Final, Never, cast

from agents.common import observation_json
from agents.cost import (
    CostEstimateError,
    ModelPricing,
    RunCostEstimate,
    estimate_run_cost,
)
from agents.llm import (
    PROVIDER_BASE_URLS,
    PROVIDER_REASONING_EFFORT,
    LLMBaselinePolicy,
    LLMConfig,
    ProviderError,
    build_provider,
    endpoint_domain,
    provider_key_name,
)
from agents.local import LocalLLMAgent
from agents.prompt import load_prompt, prompt_sha256
from cli.packs import (
    PackAcquisitionError,
    acquire_pack,
    pack_local_state,
)
from cli.progress import HeartbeatAgent, stream_is_interactive
from cli.reporting import (
    generate_comparison,
    generate_report,
    generate_scoreboard,
)
from cli.scaffold import ScaffoldError, create_scaffold
from core.config import EpisodeConfig
from core.engine import EngineError, EpisodeResult, run_episode
from core.pack import PackData, PackError, load_pack
from data.binance import BinanceDataError
from data.builder import PackBuildError
from data.catalog import (
    PackCatalogError,
    available_pack_ids,
    get_pack_definition,
)
from harness.http import (
    HTTPAgent,
    HTTPAgentConfigurationError,
    HTTPAgentError,
)
from harness.protocol import InProcessAgent
from harness.scripted import ConstantTargetAgent, MomentumAgent
from recorder.redaction import RedactionError, create_share_bundle
from recorder.replay import ReplayError, replay_bundle
from recorder.verify import VerificationResult, verify_bundle
from recorder.writer import (
    RecorderError,
    build_bundle_manifest,
    record_episode_bundle,
    validate_instance,
)
from sandbox.orchestration import (
    PreflightFailure,
    load_protected_credentials,
)

EXIT_USAGE: Final = 2
EXIT_INPUT: Final = 3
EXIT_ACQUISITION: Final = 4
EXIT_RUN: Final = 5
EXIT_EVIDENCE: Final = 6
EXIT_REPORT: Final = 7
EXIT_CONFIRMATION: Final = 8

_FIREWORKS_BASE_URL: Final = "https://api.fireworks.ai/inference/v1"
_DEFAULT_CREDENTIAL_ENV_NAME: Final = "FIREWORKS_API_KEY"
# The in-process lane has no sandbox to inherit a timeout from; this ceiling
# sits under the default per-attempt decision deadline so a stalled provider
# surfaces as one recorded missed decision rather than a hung run.
_LOCAL_PROVIDER_TIMEOUT_SECONDS: Final = 100
_REFERENCE_PRICING: Final[Mapping[str, tuple[str, str]]] = {
    # Pinned CLI estimate table (input, output USD per million tokens).
    # Operators can override both prices together. Sticker prices are pinned
    # deliberately — worst-case estimates ignore intro discounts.
    "accounts/fireworks/models/qwen3p7-plus": ("0.40", "1.60"),
    "accounts/fireworks/models/kimi-k3": ("3.00", "15.00"),
    "accounts/fireworks/models/glm-5p2": ("1.40", "4.40"),
    "accounts/fireworks/models/deepseek-v4-pro": ("1.74", "3.48"),
    "accounts/fireworks/models/inkling": ("1.00", "4.05"),
    "claude-opus-5": ("5.00", "25.00"),
    "claude-sonnet-5": ("3.00", "15.00"),
    "claude-haiku-4-5": ("1.00", "5.00"),
}

# Keyless constant-target baselines. Restating one leverage target every
# turn is idempotent (the engine fills only the delta), so these hold one
# position for the whole episode. They exist so a headline verdict is always
# read against do-nothing and hold-one-way comparators on the same pack.
_BASELINE_TARGETS: Final[Mapping[str, int]] = {
    "buyhold": 10_000,
    "shorthold": -10_000,
    "flat": 0,
}

# Adapters that can spend money and therefore require the estimate/confirm
# gate: the sandboxed two-process lane and the in-process one-command lane.
_PRICED_AGENTS: Final[frozenset[str]] = frozenset({"llm", "llm-local"})


def _baseline_manifest(kind: str, target_lev_1e4: int) -> dict[str, object]:
    return {
        "schema": "agent_manifest/v1",
        "name": f"{kind}-baseline",
        "adapter": "in_process",
        "model_id": "none",
        "endpoint_domains": [],
        "inference_params": {
            "policy": "constant-target",
            "target_lev_1e4": target_lev_1e4,
        },
        "prompt_sha256": None,
        "agent_version": "m5-cli-v1",
        "image_sha256": None,
    }


_SCRIPTED_AGENT_MANIFEST: Final[dict[str, object]] = {
    "schema": "agent_manifest/v1",
    "name": "momentum-reference",
    "adapter": "in_process",
    "model_id": "none",
    "endpoint_domains": [],
    "inference_params": {
        "policy": "two-bar-momentum",
        "leverage_lev_1e4": 10_000,
    },
    "prompt_sha256": None,
    "agent_version": "m5-cli-v1",
    "image_sha256": None,
}

# The LLM lane's structured-output control is owned by agents/llm.py and sealed
# by agents/manifest.py. The CLI cannot call build_llm_manifest directly: it
# never holds an LLMConfig (the sandboxed adapter process owns it) and it seals
# an optional image digest, which that builder requires. The values below are
# therefore mirrored and pinned to the adapter by
# cli/tests/test_llm_manifest_sync.py, which fails if the two builders diverge.
_LLM_RESPONSE_FORMAT: Final = "json_schema"


@dataclass(frozen=True, slots=True)
class _CommandError(Exception):
    message: str
    next_step: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class _ParserExit(Exception):
    status: int
    message: str


class _ArgumentParser(argparse.ArgumentParser):
    def exit(self, status: int = 0, message: str | None = None) -> Never:
        raise _ParserExit(status=status, message=message or "")

    def error(self, message: str) -> Never:
        raise _ParserExit(
            status=EXIT_USAGE,
            message=self.format_usage() + f"{self.prog}: error: {message}\n",
        )


@dataclass(frozen=True, slots=True)
class _Streams:
    stdin: IO[str]
    stdout: IO[str]
    stderr: IO[str]


def _add_pack_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--packs-root",
        type=Path,
        default=Path("packs"),
        help="directory containing built packs (default: packs)",
    )


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="wagmibench",
        description=(
            "Run and audit deterministic perpetual-futures survival scenarios."
        ),
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
    )

    wizard_parser = commands.add_parser(
        "wizard",
        help="guided interactive setup for a first run",
        description=(
            "Walk through a first run interactively: quick keyless demo, a "
            "hosted model, your own agent, or a comparison of existing "
            "bundles. Every screen prints the equivalent non-interactive "
            "command before it runs anything. Typing `wagmibench` with no "
            "arguments in a terminal opens this same flow."
        ),
    )
    wizard_parser.set_defaults(handler=_cmd_wizard)

    init_parser = commands.add_parser(
        "init",
        help="create a fresh HTTP agent adapter scaffold and config",
        description=(
            "Create a secret-free local HTTP adapter scaffold. Existing "
            "destinations are never overwritten."
        ),
    )
    init_parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("wagmibench-agent"),
        help="new scaffold directory (default: wagmibench-agent)",
    )
    init_parser.set_defaults(handler=_cmd_init)

    fetch_parser = commands.add_parser(
        "fetch-data",
        help="download checksummed Binance bulk data and build one pack",
        description=(
            "Fetch only from data.binance.vision, verify sibling checksums, "
            "and build one deterministic local pack."
        ),
    )
    fetch_parser.add_argument(
        "--pack",
        required=True,
        help="catalog pack id (run `wagmibench packs list`)",
    )
    fetch_parser.add_argument(
        "--raw-root",
        type=Path,
        default=Path("data/raw"),
        help="ignored raw archive cache (default: data/raw)",
    )
    _add_pack_roots(fetch_parser)
    fetch_parser.set_defaults(handler=_cmd_fetch_data)

    packs_parser = commands.add_parser(
        "packs",
        help="inspect the scenario-pack catalog",
    )
    pack_commands = packs_parser.add_subparsers(
        dest="packs_command",
        required=True,
        metavar="COMMAND",
    )
    list_parser = pack_commands.add_parser(
        "list",
        help="list catalog ids, intervals, and local availability",
    )
    _add_pack_roots(list_parser)
    list_parser.set_defaults(handler=_cmd_packs_list)

    run_parser = commands.add_parser(
        "run",
        help="run a pack and seal an evidence bundle",
        description=(
            "Run the keyless MomentumAgent by default. Both LLM paths (a "
            "sandboxed adapter with --agent llm, or this process with --agent "
            "llm-local) estimate worst-case tokens and cost, then require "
            "explicit confirmation before the first paid decision request."
        ),
    )
    run_parser.add_argument(
        "--pack",
        required=True,
        help="catalog id or path to a built pack directory",
    )
    _add_pack_roots(run_parser)
    run_parser.add_argument(
        "--agent",
        choices=(
            "momentum",
            "buyhold",
            "shorthold",
            "flat",
            "http",
            "llm",
            "llm-local",
        ),
        default="momentum",
        help=(
            "decision adapter (default: momentum, no key required); "
            "buyhold/shorthold/flat are keyless constant-target baselines; "
            "llm-local calls a hosted model from this process instead of a "
            "separate sandboxed adapter"
        ),
    )
    run_parser.add_argument(
        "--output",
        type=Path,
        help="fresh bundle directory (default: bundles/<pack>-<agent>)",
    )
    run_parser.add_argument(
        "--lookback-bars",
        type=_positive_int,
        help="override the pack's visible bar lookback",
    )
    run_parser.add_argument(
        "--funding-prints",
        type=_nonnegative_int,
        help="override the pack's visible funding-print lookback",
    )
    run_parser.add_argument(
        "--response-deadline-ms",
        type=_positive_int,
        default=120_000,
        help="per-attempt decision deadline (default: 120000)",
    )
    run_parser.add_argument(
        "--agent-url",
        help="HTTP adapter's sandbox-internal origin",
    )
    run_parser.add_argument(
        "--agent-name",
        default="custom-http-agent",
        help="manifest name for --agent http (default: custom-http-agent)",
    )
    run_parser.add_argument(
        "--agent-version",
        help="optional version label for --agent http",
    )
    run_parser.add_argument(
        "--endpoint-domain",
        action="append",
        default=[],
        help=(
            "declared external hostname for --agent http; repeat for each "
            "allowed domain"
        ),
    )
    run_parser.add_argument(
        "--prompt-sha256",
        help="optional exact prompt commitment for --agent http",
    )
    run_parser.add_argument(
        "--model",
        help="billed model id (or TRADEVOLVE_LLM_MODEL)",
    )
    run_parser.add_argument(
        "--provider-domain",
        help=(
            "exact external provider hostname for the agent manifest "
            "(inferred from --llm-provider, TRADEVOLVE_LLM_BASE_URL, or "
            "the Fireworks default)"
        ),
    )
    run_parser.add_argument(
        "--llm-provider",
        choices=tuple(sorted(PROVIDER_BASE_URLS)),
        help=(
            "known inference provider preset; sets the manifest endpoint "
            "domain (--provider-domain overrides)"
        ),
    )
    run_parser.add_argument(
        "--input-usd-per-million",
        help="input-token price; must be paired with output price",
    )
    run_parser.add_argument(
        "--output-usd-per-million",
        help="output-token price; must be paired with input price",
    )
    run_parser.add_argument(
        "--max-output-tokens",
        type=_positive_int,
        default=128,
        help="maximum tokens per LLM attempt (default: 128)",
    )
    run_parser.add_argument(
        "--temperature",
        default="0",
        help="manifested decimal-string LLM temperature (default: 0)",
    )
    run_parser.add_argument(
        "--image-digest",
        help="optional pinned agent-container sha256 digest",
    )
    run_parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help=(
            "explicitly accept the displayed estimate without an interactive "
            "prompt; valid only with --agent llm or --agent llm-local"
        ),
    )
    run_parser.add_argument(
        "--credential-file",
        type=Path,
        default=Path(".env"),
        help=(
            "plain dotenv source: fingerprinted protection material for "
            "--agent llm, and the key fallback for --agent llm-local when the "
            "name is absent from the environment (default: .env)"
        ),
    )
    run_parser.add_argument(
        "--credential-env-name",
        default=_DEFAULT_CREDENTIAL_ENV_NAME,
        help=(
            "declared agent-container key name to protect at HTTP ingress; "
            "--agent llm-local ignores this unless the origin is custom, "
            "because each known provider binds one canonical name "
            f"(default: {_DEFAULT_CREDENTIAL_ENV_NAME})"
        ),
    )
    run_parser.set_defaults(handler=_cmd_run)

    report_parser = commands.add_parser(
        "report",
        help="render terminal, HTML, and labeled share-card artifacts",
        description=(
            "Generate a survival report only from a COMPLETE sealed bundle; "
            "live engine state is never consulted."
        ),
    )
    report_parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help="sealed evidence bundle",
    )
    report_parser.add_argument(
        "--output",
        type=Path,
        help="fresh report directory (default: reports/<bundle-name>)",
    )
    report_parser.set_defaults(handler=_cmd_report)

    compare_parser = commands.add_parser(
        "compare",
        help="render one pack-grouped table over two or more sealed bundles",
        description=(
            "Compare candidates against baselines run on the same packs. "
            "Rows derive only from COMPLETE sealed bundles; the table is "
            "survival-first and both cost profiles are always shown."
        ),
    )
    compare_parser.add_argument(
        "--bundle",
        action="append",
        required=True,
        type=Path,
        dest="bundles",
        help="sealed evidence bundle (repeat for each row; at least two)",
    )
    compare_parser.add_argument(
        "--output",
        type=Path,
        help="fresh directory for compare.txt and compare.json (optional)",
    )
    compare_parser.set_defaults(handler=_cmd_compare)

    score_parser = commands.add_parser(
        "score",
        help="aggregate sealed bundles into WAGMI Scores per agent",
        description=(
            "Compute the survival-dominant WAGMI Score for every candidate "
            "agent across the given sealed bundles. Include the keyless "
            "baseline bundles for each pack so the GMI rung is decidable."
        ),
    )
    score_parser.add_argument(
        "--bundle",
        action="append",
        required=True,
        type=Path,
        dest="bundles",
        help="sealed evidence bundle (repeat; candidates and baselines)",
    )
    score_parser.add_argument(
        "--output",
        type=Path,
        help="fresh directory for score.txt and score.json (optional)",
    )
    score_parser.set_defaults(handler=_cmd_score)

    replay_parser = commands.add_parser(
        "replay",
        help="prove stored economics reproduce byte-for-byte offline",
    )
    replay_parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help="sealed evidence bundle",
    )
    replay_parser.add_argument(
        "--pack",
        required=True,
        help="catalog id or path to the exact built pack",
    )
    _add_pack_roots(replay_parser)
    replay_parser.set_defaults(handler=_cmd_replay)

    share_parser = commands.add_parser(
        "share",
        help="create a verifiable redacted share sub-bundle",
        description=(
            "Copy a COMPLETE sealed bundle, remove only committed raw model "
            "response blobs, and disclose every removal in redaction.json. "
            "Use `report` for the static SVG card."
        ),
    )
    share_parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help="sealed evidence bundle",
    )
    share_parser.add_argument(
        "--output",
        type=Path,
        help="fresh share-bundle directory (default: shares/<bundle-name>)",
    )
    share_parser.set_defaults(handler=_cmd_share)
    return parser


def _positive_int(raw: str) -> int:
    value = _integer(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be an integer >= 1")
    return value


def _nonnegative_int(raw: str) -> int:
    value = _integer(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be an integer >= 0")
    return value


def _integer(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc


def _arg(args: argparse.Namespace, name: str) -> object:
    return vars(args)[name]


def _string_arg(args: argparse.Namespace, name: str) -> str:
    value = _arg(args, name)
    if not isinstance(value, str):
        raise _CommandError(
            message=f"internal CLI argument {name!r} is invalid",
            next_step="Run the command with --help and report this as a bug.",
            exit_code=EXIT_USAGE,
        )
    return value


def _optional_string_arg(
    args: argparse.Namespace,
    name: str,
) -> str | None:
    value = _arg(args, name)
    if value is None or isinstance(value, str):
        return value
    raise _CommandError(
        message=f"internal CLI argument {name!r} is invalid",
        next_step="Run the command with --help and report this as a bug.",
        exit_code=EXIT_USAGE,
    )


def _string_list_arg(
    args: argparse.Namespace,
    name: str,
) -> tuple[str, ...]:
    value = _arg(args, name)
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise _CommandError(
            message=f"internal CLI list argument {name!r} is invalid",
            next_step="Run the command with --help and report this as a bug.",
            exit_code=EXIT_USAGE,
        )
    return tuple(value)


def _path_arg(args: argparse.Namespace, name: str) -> Path:
    value = _arg(args, name)
    if not isinstance(value, Path):
        raise _CommandError(
            message=f"internal CLI path argument {name!r} is invalid",
            next_step="Run the command with --help and report this as a bug.",
            exit_code=EXIT_USAGE,
        )
    return value


def _optional_path_arg(
    args: argparse.Namespace,
    name: str,
) -> Path | None:
    value = _arg(args, name)
    if value is None or isinstance(value, Path):
        return value
    raise _CommandError(
        message=f"internal CLI path argument {name!r} is invalid",
        next_step="Run the command with --help and report this as a bug.",
        exit_code=EXIT_USAGE,
    )


def _optional_int_arg(
    args: argparse.Namespace,
    name: str,
) -> int | None:
    value = _arg(args, name)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise _CommandError(
        message=f"internal CLI integer argument {name!r} is invalid",
        next_step="Run the command with --help and report this as a bug.",
        exit_code=EXIT_USAGE,
    )


def _bool_arg(args: argparse.Namespace, name: str) -> bool:
    value = _arg(args, name)
    if isinstance(value, bool):
        return value
    raise _CommandError(
        message=f"internal CLI flag {name!r} is invalid",
        next_step="Run the command with --help and report this as a bug.",
        exit_code=EXIT_USAGE,
    )


def _resolve_pack(pack_ref: str, packs_root: Path) -> Path:
    candidate = Path(pack_ref)
    if candidate.is_dir():
        return candidate
    catalog_path = packs_root / pack_ref
    if catalog_path.is_dir():
        return catalog_path
    try:
        get_pack_definition(pack_ref)
    except PackCatalogError as exc:
        raise _CommandError(
            message=f"pack {pack_ref!r} is neither a directory nor a catalog id",
            next_step="Run `wagmibench packs list` and choose an available id.",
            exit_code=EXIT_INPUT,
        ) from exc
    raise _CommandError(
        message=f"pack {pack_ref!r} has not been built at {catalog_path}",
        next_step=(
            f"Run `wagmibench fetch-data --pack {pack_ref}` first, "
            "or pass the built pack directory."
        ),
        exit_code=EXIT_INPUT,
    )


def _require_fresh(path: Path, *, label: str, next_step: str) -> None:
    if path.exists():
        raise _CommandError(
            message=f"{label} already exists: {path}",
            next_step=next_step,
            exit_code=EXIT_INPUT,
        )


def _cmd_wizard(args: argparse.Namespace, streams: _Streams) -> int:
    """Delegate the guided flow, which owns no run or spend logic itself."""

    del args
    # Imported here so cli.wizard can import this module's dispatch seam at
    # module scope without a cycle.
    from cli.wizard import run_wizard

    return run_wizard(streams)


def _cmd_init(args: argparse.Namespace, streams: _Streams) -> int:
    directory = _path_arg(args, "directory")
    try:
        source, config = create_scaffold(directory)
    except ScaffoldError as exc:
        raise _CommandError(
            message=str(exc),
            next_step=(
                "Choose a new directory, or move the existing material "
                "before retrying."
            ),
            exit_code=EXIT_INPUT,
        ) from exc
    print(f"Created adapter: {source}", file=streams.stdout)
    print(f"Created config:  {config}", file=streams.stdout)
    print(
        f"Next: edit {source}, run it with your Python interpreter, then use "
        "`wagmibench run --agent http --agent-url "
        "http://127.0.0.1:8000 --pack <PACK>`.",
        file=streams.stdout,
    )
    return 0


def _cmd_fetch_data(args: argparse.Namespace, streams: _Streams) -> int:
    pack_id = _string_arg(args, "pack")
    raw_root = _path_arg(args, "raw_root")
    packs_root = _path_arg(args, "packs_root")
    try:
        get_pack_definition(pack_id)
    except PackCatalogError as exc:
        raise _CommandError(
            message=str(exc),
            next_step="Run `wagmibench packs list` and choose a catalog id.",
            exit_code=EXIT_INPUT,
        ) from exc
    try:
        local = acquire_pack(
            pack_id,
            raw_root=raw_root,
            packs_root=packs_root,
        )
    except (
        BinanceDataError,
        PackBuildError,
        PackAcquisitionError,
        OSError,
    ) as exc:
        raise _CommandError(
            message=f"could not fetch and build {pack_id!r}: {exc}",
            next_step=(
                "Check network access and free disk space, then retry; "
                "verified partial downloads in the raw cache can resume."
            ),
            exit_code=EXIT_ACQUISITION,
        ) from exc
    print(f"Local state: {local.state}", file=streams.stdout)
    print(f"Ready pack: {local.directory}", file=streams.stdout)
    print(f"Content hash: {local.content_hash}", file=streams.stdout)
    print(
        f"Next: wagmibench run --pack {pack_id}",
        file=streams.stdout,
    )
    return 0


def _cmd_packs_list(args: argparse.Namespace, streams: _Streams) -> int:
    packs_root = _path_arg(args, "packs_root")
    print("PACK ID                         BAR   LOCAL", file=streams.stdout)
    for pack_id in available_pack_ids():
        definition = get_pack_definition(pack_id)
        interval_hours = definition.config.decision_bar_ms // 3_600_000
        state = pack_local_state(packs_root / pack_id)
        print(
            f"{pack_id:<31} {interval_hours:>2}h   {state}",
            file=streams.stdout,
        )
    print(
        "Next: wagmibench fetch-data --pack <PACK-ID>",
        file=streams.stdout,
    )
    return 0


def _episode_config(args: argparse.Namespace) -> EpisodeConfig:
    deadline = _optional_int_arg(args, "response_deadline_ms")
    if deadline is None:
        raise _CommandError(
            message="response deadline is missing",
            next_step="Pass --response-deadline-ms with a positive integer.",
            exit_code=EXIT_USAGE,
        )
    return EpisodeConfig(
        lookback_bars=_optional_int_arg(args, "lookback_bars"),
        funding_prints=_optional_int_arg(args, "funding_prints"),
        response_deadline_ms=deadline,
    )


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(8)


def _host_manifest() -> dict[str, object]:
    return {
        "os": platform.system().lower() or "unknown",
        "arch": platform.machine().lower() or "unknown",
        "python": platform.python_version(),
    }


def _reference_pricing(
    args: argparse.Namespace,
    *,
    model: str,
) -> ModelPricing:
    input_price = _optional_string_arg(args, "input_usd_per_million")
    output_price = _optional_string_arg(args, "output_usd_per_million")
    if (input_price is None) != (output_price is None):
        raise _CommandError(
            message="LLM input and output prices must be supplied together",
            next_step=(
                "Pass both --input-usd-per-million and "
                "--output-usd-per-million."
            ),
            exit_code=EXIT_USAGE,
        )
    if input_price is None or output_price is None:
        pinned = _REFERENCE_PRICING.get(model)
        if pinned is None:
            raise _CommandError(
                message=f"there is no pinned CLI pricing row for model {model!r}",
                next_step=(
                    "Pass both token prices from the provider's current "
                    "official pricing page."
                ),
                exit_code=EXIT_INPUT,
            )
        input_price, output_price = pinned
    try:
        return ModelPricing.from_strings(
            input_usd_per_million=input_price,
            output_usd_per_million=output_price,
        )
    except CostEstimateError as exc:
        raise _CommandError(
            message=f"invalid LLM pricing: {exc}",
            next_step="Pass finite, non-negative decimal-string prices.",
            exit_code=EXIT_USAGE,
        ) from exc


def _provider_domain(args: argparse.Namespace) -> str:
    explicit = _optional_string_arg(args, "provider_domain")
    preset = _optional_string_arg(args, "llm_provider")
    if explicit is not None:
        origin = f"https://{explicit}"
    elif preset is not None:
        origin = PROVIDER_BASE_URLS[preset]
    else:
        origin = os.environ.get(
            "TRADEVOLVE_LLM_BASE_URL",
            _FIREWORKS_BASE_URL,
        )
    try:
        domain = endpoint_domain(origin)
    except ValueError as exc:
        raise _CommandError(
            message=f"invalid LLM provider origin/domain: {exc}",
            next_step=(
                "Pass --provider-domain as a bare hostname such as "
                "api.fireworks.ai."
            ),
            exit_code=EXIT_USAGE,
        ) from exc
    if "." not in domain:
        raise _CommandError(
            message=f"provider domain {domain!r} is not manifest-safe",
            next_step="Pass an exact DNS hostname with at least one dot.",
            exit_code=EXIT_USAGE,
        )
    return domain


def _provider_origin(args: argparse.Namespace) -> str:
    """Return the exact API origin the in-process lane will call.

    ``--provider-domain`` is deliberately absent here: a bare hostname cannot
    carry the provider's API path prefix, so the in-process lane resolves its
    origin only from a known preset or an explicit base URL and then checks
    the two agree.
    """

    preset = _optional_string_arg(args, "llm_provider")
    if preset is not None:
        return PROVIDER_BASE_URLS[preset]
    return os.environ.get("TRADEVOLVE_LLM_BASE_URL", _FIREWORKS_BASE_URL)


def _model(args: argparse.Namespace) -> str:
    model = _optional_string_arg(args, "model")
    if model is None:
        model = os.environ.get("TRADEVOLVE_LLM_MODEL", "")
    if not model or any(character.isspace() for character in model):
        raise _CommandError(
            message="the LLM model id is missing or invalid",
            next_step="Pass --model or set TRADEVOLVE_LLM_MODEL.",
            exit_code=EXIT_INPUT,
        )
    return model


def _temperature(args: argparse.Namespace) -> str:
    value = _string_arg(args, "temperature")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise _CommandError(
            message="LLM temperature is not a decimal string",
            next_step="Pass --temperature between 0 and 2, such as 0 or 0.2.",
            exit_code=EXIT_USAGE,
        ) from exc
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("2"):
        raise _CommandError(
            message="LLM temperature must be between 0 and 2",
            next_step="Pass --temperature between 0 and 2, such as 0 or 0.2.",
            exit_code=EXIT_USAGE,
        )
    return value


def _llm_inference_params(
    *,
    temperature: str,
    max_output_tokens: int,
    provider_domain: str = "api.fireworks.ai",
) -> dict[str, object]:
    """Return the generation controls the LLM adapter actually transmits."""

    if provider_domain == "api.anthropic.com":
        return {
            "max_tokens": max_output_tokens,
            "response_format": _LLM_RESPONSE_FORMAT,
            "provider_api": "anthropic-messages",
        }
    return {
        "temperature": temperature,
        "max_tokens": max_output_tokens,
        "reasoning_effort": PROVIDER_REASONING_EFFORT,
        "response_format": _LLM_RESPONSE_FORMAT,
        "provider_api": "openai-chat-completions",
    }


def _estimate_llm_run(
    *,
    pack_path: Path,
    pack: PackData,
    config: EpisodeConfig,
    max_output_tokens: int,
    pricing: ModelPricing,
) -> RunCostEstimate:
    dry_result = run_episode(
        pack_dir=pack_path,
        agent=MomentumAgent(),
        config=config,
        run_id="run_0000000000000000",
        episode_id="ep_0000000000000000",
    )
    if not dry_result.observations:
        raise _CommandError(
            message="the pack produced no sample observation for estimation",
            next_step="Validate the pack and its warmup/window configuration.",
            exit_code=EXIT_RUN,
        )
    sample = max(
        dry_result.observations,
        key=lambda observation: len(observation_json(observation)),
    )
    try:
        return estimate_run_cost(
            system_prompt=load_prompt(),
            sample_observation_json=observation_json(sample),
            turns=pack.bars_total,
            max_output_tokens=max_output_tokens,
            attempts_per_turn=1 + config.parse_failure_retries,
            pricing=pricing,
        )
    except (CostEstimateError, OSError, UnicodeError) as exc:
        raise _CommandError(
            message=f"could not calculate the pre-run estimate: {exc}",
            next_step="Verify the local prompt and numeric run configuration.",
            exit_code=EXIT_RUN,
        ) from exc


def _print_estimate(
    estimate: RunCostEstimate,
    *,
    model: str,
    pricing: ModelPricing,
    streams: _Streams,
) -> None:
    cost = estimate.estimated_cost_usd
    if cost is None:
        raise _CommandError(
            message="the LLM cost estimate has no price",
            next_step="Pass valid model token prices.",
            exit_code=EXIT_RUN,
        )
    print("Pre-run LLM cost estimate (no request sent):", file=streams.stdout)
    print(f"  model: {model}", file=streams.stdout)
    print(f"  pack turns: {estimate.turns}", file=streams.stdout)
    print(
        f"  maximum attempts per turn: {estimate.attempts_per_turn}",
        file=streams.stdout,
    )
    print(f"  estimated input tokens: {estimate.input_tokens}", file=streams.stdout)
    print(
        f"  maximum output tokens: {estimate.output_tokens}",
        file=streams.stdout,
    )
    print(
        "  prices per 1M tokens: "
        f"input ${pricing.input_usd_per_million}, "
        f"output ${pricing.output_usd_per_million}",
        file=streams.stdout,
    )
    print(f"  estimated maximum cost: ${format(cost, 'f')}", file=streams.stdout)
    print(
        "  method: largest observed lookback payload × every pack turn × "
        "the IC-6 retry ceiling",
        file=streams.stdout,
    )


def _confirm_llm_spend(
    *,
    estimate: RunCostEstimate,
    confirmed_by_flag: bool,
    streams: _Streams,
) -> None:
    if estimate.estimated_cost_usd is None:
        raise _CommandError(
            message="cannot confirm an unpriced LLM run",
            next_step="Supply a complete pricing row.",
            exit_code=EXIT_RUN,
        )
    if confirmed_by_flag:
        print(
            "Explicit spend confirmation accepted via --confirm-spend.",
            file=streams.stdout,
        )
        return
    print(
        "Type `yes` to authorize the estimated paid LLM requests; "
        "anything else cancels: ",
        end="",
        file=streams.stdout,
        flush=True,
    )
    response = streams.stdin.readline().strip()
    if response != "yes":
        raise _CommandError(
            message="LLM run cancelled before any decision request or spending",
            next_step=(
                "Review the estimate, then rerun and type `yes`, or pass "
                "--confirm-spend as an explicit non-interactive authorization."
            ),
            exit_code=EXIT_CONFIRMATION,
        )
    print("Explicit interactive spend confirmation accepted.", file=streams.stdout)


def _http_agent(
    origin: str,
    *,
    protected_response_values: Sequence[str | bytes] = (),
) -> HTTPAgent:
    try:
        if protected_response_values:
            return HTTPAgent(
                origin,
                protected_response_values=protected_response_values,
            )
        return HTTPAgent(origin)
    except HTTPAgentConfigurationError as exc:
        raise _CommandError(
            message="the HTTP adapter URL is invalid",
            next_step=(
                "Pass a credential-free, sandbox-internal plain-HTTP origin "
                "such as http://127.0.0.1:8000."
            ),
            exit_code=EXIT_INPUT,
        ) from exc


def _validated_agent_manifest(
    manifest: dict[str, object],
    *,
    label: str,
) -> dict[str, object]:
    try:
        validate_instance(
            "agent_manifest",
            manifest,
            context=label,
        )
    except RecorderError as exc:
        raise _CommandError(
            message=f"{label} is invalid: {exc}",
            next_step=(
                "Fix the agent attribution, endpoint domains, prompt hash, "
                "or optional image digest before running."
            ),
            exit_code=EXIT_INPUT,
        ) from exc
    return manifest


def _http_agent_and_manifest(
    args: argparse.Namespace,
) -> tuple[InProcessAgent, dict[str, object]]:
    origin = _optional_string_arg(args, "agent_url")
    if origin is None:
        raise _CommandError(
            message="the HTTP adapter URL is missing",
            next_step=(
                "Start the scaffolded adapter and pass "
                "--agent-url http://127.0.0.1:8000."
            ),
            exit_code=EXIT_INPUT,
        )
    model = _optional_string_arg(args, "model")
    if model not in {None, "none"}:
        raise _CommandError(
            message=(
                "--agent http accepts only model_id 'none'; priced model "
                "adapters must use --agent llm"
            ),
            next_step=(
                "Use --agent llm so WAGMI Bench estimates cost and requires "
                "explicit spend confirmation."
            ),
            exit_code=EXIT_USAGE,
        )
    domains = _string_list_arg(args, "endpoint_domain")
    for domain in domains:
        try:
            parsed = endpoint_domain(f"https://{domain}")
        except ValueError as exc:
            raise _CommandError(
                message="an HTTP agent endpoint domain is invalid",
                next_step=(
                    "Pass each --endpoint-domain as a bare DNS hostname "
                    "without scheme, port, path, or credentials."
                ),
                exit_code=EXIT_USAGE,
            ) from exc
        if parsed != domain or "." not in parsed:
            raise _CommandError(
                message=f"endpoint domain {domain!r} is not manifest-safe",
                next_step=(
                    "Pass a lowercase bare DNS hostname with at least one dot."
                ),
                exit_code=EXIT_USAGE,
            )
    manifest: dict[str, object] = {
        "schema": "agent_manifest/v1",
        "name": _string_arg(args, "agent_name"),
        "adapter": "http",
        "model_id": "none",
        "endpoint_domains": list(domains),
        "inference_params": {},
        "prompt_sha256": _optional_string_arg(args, "prompt_sha256"),
        "agent_version": _optional_string_arg(args, "agent_version"),
        "image_sha256": _optional_string_arg(args, "image_digest"),
    }
    return _http_agent(origin), _validated_agent_manifest(
        manifest,
        label="HTTP agent manifest",
    )


def _llm_agent_and_manifest(
    args: argparse.Namespace,
    *,
    streams: _Streams,
    pack_path: Path,
    pack: PackData,
    config: EpisodeConfig,
) -> tuple[InProcessAgent, dict[str, object]]:
    origin = _optional_string_arg(args, "agent_url")
    if origin is None:
        raise _CommandError(
            message="the LLM adapter URL is missing",
            next_step=(
                "Start the sandboxed HTTP agent and pass "
                "--agent-url http://<host>:<port>."
            ),
            exit_code=EXIT_INPUT,
        )
    model = _model(args)
    domain = _provider_domain(args)
    pricing = _reference_pricing(args, model=model)
    max_output_tokens = _optional_int_arg(args, "max_output_tokens")
    if max_output_tokens is None:
        raise _CommandError(
            message="the maximum output token count is missing",
            next_step="Pass --max-output-tokens with a positive integer.",
            exit_code=EXIT_USAGE,
        )
    image_digest = _optional_string_arg(args, "image_digest")
    manifest: dict[str, object] = {
        "schema": "agent_manifest/v1",
        "name": "llm-baseline",
        "adapter": "http",
        "model_id": model,
        "endpoint_domains": [domain],
        "inference_params": _llm_inference_params(
            temperature=_temperature(args),
            max_output_tokens=max_output_tokens,
            provider_domain=domain,
        ),
        "prompt_sha256": prompt_sha256(),
        "agent_version": "m5-cli-v1",
        "image_sha256": image_digest,
    }
    manifest = _validated_agent_manifest(
        manifest,
        label="LLM agent manifest",
    )
    estimate = _estimate_llm_run(
        pack_path=pack_path,
        pack=pack,
        config=config,
        max_output_tokens=max_output_tokens,
        pricing=pricing,
    )
    _print_estimate(
        estimate,
        model=model,
        pricing=pricing,
        streams=streams,
    )
    _confirm_llm_spend(
        estimate=estimate,
        confirmed_by_flag=_bool_arg(args, "confirm_spend"),
        streams=streams,
    )
    credential_file = _path_arg(args, "credential_file")
    credential_name = _string_arg(args, "credential_env_name")
    try:
        protected_values = load_protected_credentials(
            credential_file,
            (credential_name,),
        )
    except PreflightFailure as exc:
        raise _CommandError(
            message=f"LLM credential protection is not ready: {exc}",
            next_step=(
                "Put the declared key in a plain gitignored .env file, pass "
                "--credential-file and --credential-env-name if needed, then "
                "retry. The value is fingerprinted and never recorded."
            ),
            exit_code=EXIT_INPUT,
        ) from exc
    agent = _http_agent(
        origin,
        protected_response_values=protected_values,
    )
    heartbeat = HeartbeatAgent(
        inner=agent,
        stream=streams.stderr,
        total_turns=estimate.turns,
        interactive=stream_is_interactive(streams.stderr),
    )
    return heartbeat, manifest


def _local_llm_credential(
    args: argparse.Namespace,
    *,
    base_url: str,
) -> tuple[str, str]:
    """Resolve the provider's credential name and value, echoing neither.

    Each known provider origin binds exactly one canonical environment name,
    so the declared ``--credential-env-name`` is honoured only when the
    operator moved it off the default (which a custom origin requires).
    """

    declared = _string_arg(args, "credential_env_name")
    configured = None if declared == _DEFAULT_CREDENTIAL_ENV_NAME else declared
    try:
        name = provider_key_name(base_url, configured=configured)
    except ValueError as exc:
        raise _CommandError(
            message=f"the LLM credential name is not usable: {exc}",
            next_step=(
                "Leave --credential-env-name unset for a known provider, or "
                "pass a provider-specific name for a custom origin."
            ),
            exit_code=EXIT_USAGE,
        ) from exc
    from_environment = os.environ.get(name)
    if from_environment:
        return name, from_environment
    credential_file = _path_arg(args, "credential_file")
    try:
        loaded = load_protected_credentials(credential_file, (name,))
    except PreflightFailure as exc:
        raise _CommandError(
            message=f"the {name} credential is not available: {exc}",
            next_step=(
                f"Export {name}, or add a `{name}=...` line to "
                f"{credential_file} (keep it gitignored). The value is never "
                "printed, manifested, or recorded."
            ),
            exit_code=EXIT_INPUT,
        ) from exc
    return name, loaded[0]


def _local_llm_agent(
    *,
    base_url: str,
    model: str,
    temperature: str,
    max_output_tokens: int,
    credential_name: str,
    credential_value: str,
) -> InProcessAgent:
    """Build the in-process policy without touching the process environment."""

    try:
        config = LLMConfig(
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=max_output_tokens,
            timeout_seconds=_LOCAL_PROVIDER_TIMEOUT_SECONDS,
            api_key_env_name=credential_name,
        )
        provider = build_provider(config, {credential_name: credential_value})
    except ValueError as exc:
        raise _CommandError(
            message=f"the in-process LLM configuration is invalid: {exc}",
            next_step=(
                "Check --model, --temperature, and --max-output-tokens, then "
                "retry."
            ),
            exit_code=EXIT_USAGE,
        ) from exc
    except ProviderError as exc:
        raise _CommandError(
            message=f"the in-process LLM provider is not usable: {exc}",
            next_step=(
                "Current Anthropic models reject sampling parameters; leave "
                "--temperature at its default 0 for that provider."
            ),
            exit_code=EXIT_USAGE,
        ) from exc
    return LocalLLMAgent(
        policy=LLMBaselinePolicy(
            provider=provider,
            config=config,
            system_prompt=load_prompt(),
        )
    )


def _local_llm_agent_and_manifest(
    args: argparse.Namespace,
    *,
    streams: _Streams,
    pack_path: Path,
    pack: PackData,
    config: EpisodeConfig,
) -> tuple[InProcessAgent, dict[str, object]]:
    """Run the priced model lane inside this process, sealing what it calls."""

    if _optional_string_arg(args, "agent_url") is not None:
        raise _CommandError(
            message="--agent-url is not valid with --agent llm-local",
            next_step=(
                "Drop --agent-url: llm-local runs the model policy in-process. "
                "Use --agent llm for the sandboxed two-process lane."
            ),
            exit_code=EXIT_USAGE,
        )
    if _optional_string_arg(args, "image_digest") is not None:
        raise _CommandError(
            message="--image-digest is not valid with --agent llm-local",
            next_step=(
                "Drop --image-digest: no agent container runs in-process. Use "
                "--agent llm to seal a pinned adapter image."
            ),
            exit_code=EXIT_USAGE,
        )
    model = _model(args)
    origin = _provider_origin(args)
    domain = _provider_domain(args)
    try:
        origin_domain = endpoint_domain(origin)
    except ValueError as exc:
        raise _CommandError(
            message=f"invalid in-process LLM provider origin: {exc}",
            next_step=(
                "Select the origin with --llm-provider, or set "
                "TRADEVOLVE_LLM_BASE_URL to a credential-free HTTPS origin."
            ),
            exit_code=EXIT_USAGE,
        ) from exc
    if origin_domain != domain:
        raise _CommandError(
            message=(
                f"--provider-domain {domain!r} does not match the in-process "
                f"provider origin ({origin_domain})"
            ),
            next_step=(
                "Drop --provider-domain and select the provider with "
                "--llm-provider or TRADEVOLVE_LLM_BASE_URL; llm-local seals "
                "exactly the origin it calls."
            ),
            exit_code=EXIT_USAGE,
        )
    pricing = _reference_pricing(args, model=model)
    max_output_tokens = _optional_int_arg(args, "max_output_tokens")
    if max_output_tokens is None:
        raise _CommandError(
            message="the maximum output token count is missing",
            next_step="Pass --max-output-tokens with a positive integer.",
            exit_code=EXIT_USAGE,
        )
    temperature = _temperature(args)
    manifest: dict[str, object] = {
        "schema": "agent_manifest/v1",
        "name": "llm-local",
        "adapter": "in_process",
        "model_id": model,
        "endpoint_domains": [domain],
        "inference_params": _llm_inference_params(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            provider_domain=domain,
        ),
        "prompt_sha256": prompt_sha256(),
        "agent_version": "m5-cli-v1",
        "image_sha256": None,
    }
    manifest = _validated_agent_manifest(
        manifest,
        label="in-process LLM agent manifest",
    )
    # Resolved before the estimate so a missing key cannot waste an operator's
    # spend confirmation; the value stays in this frame and never reaches the
    # process environment, the manifest, stdout, or the bundle.
    credential_name, credential_value = _local_llm_credential(
        args,
        base_url=origin,
    )
    estimate = _estimate_llm_run(
        pack_path=pack_path,
        pack=pack,
        config=config,
        max_output_tokens=max_output_tokens,
        pricing=pricing,
    )
    _print_estimate(
        estimate,
        model=model,
        pricing=pricing,
        streams=streams,
    )
    _confirm_llm_spend(
        estimate=estimate,
        confirmed_by_flag=_bool_arg(args, "confirm_spend"),
        streams=streams,
    )
    agent = _local_llm_agent(
        base_url=origin,
        model=model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        credential_name=credential_name,
        credential_value=credential_value,
    )
    heartbeat = HeartbeatAgent(
        inner=agent,
        stream=streams.stderr,
        total_turns=estimate.turns,
        interactive=stream_is_interactive(streams.stderr),
    )
    return heartbeat, manifest


def _record_run(
    *,
    pack_path: Path,
    bundle_path: Path,
    agent: InProcessAgent,
    agent_manifest: Mapping[str, object],
    config: EpisodeConfig,
) -> tuple[EpisodeResult, VerificationResult]:
    result = run_episode(
        pack_dir=pack_path,
        agent=agent,
        config=config,
        run_id=_new_id("run_"),
        episode_id=_new_id("ep_"),
    )
    manifest = build_bundle_manifest(
        result,
        agent_manifest,
        created_at_ms=time.time_ns() // 1_000_000,
        host=_host_manifest(),
    )
    record_episode_bundle(
        bundle_path,
        result=result,
        manifest=manifest,
        agent_manifest=agent_manifest,
    )
    verification = verify_bundle(bundle_path)
    return result, verification


def _cmd_run(args: argparse.Namespace, streams: _Streams) -> int:
    pack_ref = _string_arg(args, "pack")
    packs_root = _path_arg(args, "packs_root")
    pack_path = _resolve_pack(pack_ref, packs_root)
    agent_kind = _string_arg(args, "agent")
    output = _optional_path_arg(args, "output")
    if output is None:
        output = Path("bundles") / f"{pack_path.name}-{agent_kind}"
    _require_fresh(
        output,
        label="bundle output directory",
        next_step="Choose a fresh --output path; evidence bundles are immutable.",
    )
    if agent_kind not in _PRICED_AGENTS and _bool_arg(args, "confirm_spend"):
        raise _CommandError(
            message=(
                "--confirm-spend is valid only with --agent llm or "
                "--agent llm-local"
            ),
            next_step=(
                "Remove the flag for a keyless adapter, or use --agent llm "
                "or --agent llm-local for the priced-model confirmation flow."
            ),
            exit_code=EXIT_USAGE,
        )
    try:
        pack = load_pack(pack_path)
        config = _episode_config(args)
        if agent_kind == "momentum":
            agent: InProcessAgent = MomentumAgent()
            agent_manifest: Mapping[str, object] = _SCRIPTED_AGENT_MANIFEST
        elif agent_kind in _BASELINE_TARGETS:
            target_lev_1e4 = _BASELINE_TARGETS[agent_kind]
            agent = ConstantTargetAgent(target_lev_1e4=target_lev_1e4)
            agent_manifest = _baseline_manifest(agent_kind, target_lev_1e4)
        elif agent_kind == "http":
            agent, agent_manifest = _http_agent_and_manifest(args)
        elif agent_kind == "llm-local":
            agent, agent_manifest = _local_llm_agent_and_manifest(
                args,
                streams=streams,
                pack_path=pack_path,
                pack=pack,
                config=config,
            )
        else:
            agent, agent_manifest = _llm_agent_and_manifest(
                args,
                streams=streams,
                pack_path=pack_path,
                pack=pack,
                config=config,
            )
        result, verification = _record_run(
            pack_path=pack_path,
            bundle_path=output,
            agent=agent,
            agent_manifest=agent_manifest,
            config=config,
        )
    except _CommandError:
        raise
    except (PackError, EngineError, HTTPAgentError, RecorderError, OSError) as exc:
        raise _CommandError(
            message=f"episode run failed: {exc}",
            next_step=(
                "Validate the pack and adapter health, choose a fresh output "
                "path, then retry."
            ),
            exit_code=EXIT_RUN,
        ) from exc
    if not verification.is_complete:
        raise _CommandError(
            message=(
                f"bundle verification returned {verification.verdict}: "
                f"{verification.message}"
            ),
            next_step=(
                "Preserve the bundle as evidence, inspect the named path/stream, "
                "and do not report or share it."
            ),
            exit_code=EXIT_EVIDENCE,
        )
    invariant = result.metrics.get("profile_invariant")
    verdict = "unknown"
    if isinstance(invariant, Mapping):
        value = invariant.get("survival_verdict")
        if isinstance(value, str):
            verdict = value
    print(f"Run complete: {result.run_id}", file=streams.stdout)
    print(f"Survival verdict: {verdict}", file=streams.stdout)
    print(f"Bundle: {output}", file=streams.stdout)
    print(f"Evidence root: {verification.root}", file=streams.stdout)
    print(
        f"Next: wagmibench report --bundle {output}",
        file=streams.stdout,
    )
    return 0


def _complete_bundle(bundle: Path) -> VerificationResult:
    verification = verify_bundle(bundle)
    if not verification.is_complete:
        raise _CommandError(
            message=(
                f"bundle is {verification.verdict}, not COMPLETE: "
                f"{verification.message}"
            ),
            next_step=(
                "Inspect the verifier's named path/stream; only a COMPLETE "
                "bundle can be reported, replayed, or shared."
            ),
            exit_code=EXIT_EVIDENCE,
        )
    return verification


def _cmd_report(args: argparse.Namespace, streams: _Streams) -> int:
    bundle = _path_arg(args, "bundle")
    output = _optional_path_arg(args, "output")
    if output is None:
        output = Path("reports") / bundle.name
    _require_fresh(
        output,
        label="report output directory",
        next_step="Choose a fresh --output directory to avoid overwriting a report.",
    )
    _complete_bundle(bundle)
    try:
        files = generate_report(bundle, output)
    except (RuntimeError, OSError, ValueError) as exc:
        raise _CommandError(
            message=f"report generation failed: {exc}",
            next_step=(
                "Verify the report component is installed and the output "
                "parent is writable, then retry."
            ),
            exit_code=EXIT_REPORT,
        ) from exc
    print(f"Report directory: {output}", file=streams.stdout)
    for path in files:
        print(f"  {path}", file=streams.stdout)
    print(
        f"Next: open {output / 'report.html'} or share "
        f"{output / 'share-card.svg'}.",
        file=streams.stdout,
    )
    return 0


def _cmd_score(args: argparse.Namespace, streams: _Streams) -> int:
    bundles = tuple(Path(bundle) for bundle in args.bundles)
    if len(bundles) < 2:
        raise _CommandError(
            message="score needs at least two --bundle arguments",
            next_step=(
                "Pass candidate bundles plus the keyless baseline bundles "
                "for each pack so the GMI rung is decidable."
            ),
            exit_code=EXIT_USAGE,
        )
    output = _optional_path_arg(args, "output")
    if output is not None:
        _require_fresh(
            output,
            label="score output directory",
            next_step=(
                "Choose a fresh --output directory to avoid overwriting a "
                "scoreboard."
            ),
        )
    # As with compare, the report loader verifies every bundle and refuses
    # anything not COMPLETE; no second CLI verification pass at this scale.
    try:
        table, files = generate_scoreboard(bundles, output)
    except (RuntimeError, OSError, ValueError) as exc:
        raise _CommandError(
            message=f"score generation failed: {exc}",
            next_step=(
                "Verify the report component is installed and every bundle "
                "is COMPLETE, then retry."
            ),
            exit_code=EXIT_REPORT,
        ) from exc
    print(table, end="", file=streams.stdout)
    for path in files:
        print(f"  {path}", file=streams.stdout)
    return 0


def _cmd_compare(args: argparse.Namespace, streams: _Streams) -> int:
    bundles = tuple(Path(bundle) for bundle in args.bundles)
    if len(bundles) < 2:
        raise _CommandError(
            message="compare needs at least two --bundle arguments",
            next_step=(
                "Pass a candidate bundle and at least one baseline bundle "
                "run on the same pack."
            ),
            exit_code=EXIT_USAGE,
        )
    output = _optional_path_arg(args, "output")
    if output is not None:
        _require_fresh(
            output,
            label="compare output directory",
            next_step=(
                "Choose a fresh --output directory to avoid overwriting a "
                "comparison."
            ),
        )
    # No CLI pre-verification pass: the report loader verifies each bundle
    # and refuses anything not COMPLETE, and a second full verification per
    # bundle is material at comparison scale (dozens of bundles).
    try:
        table, files = generate_comparison(bundles, output)
    except (RuntimeError, OSError, ValueError) as exc:
        raise _CommandError(
            message=f"comparison generation failed: {exc}",
            next_step=(
                "Verify the report component is installed and every bundle "
                "is COMPLETE, then retry."
            ),
            exit_code=EXIT_REPORT,
        ) from exc
    print(table, end="", file=streams.stdout)
    for path in files:
        print(f"  {path}", file=streams.stdout)
    return 0


def _cmd_replay(args: argparse.Namespace, streams: _Streams) -> int:
    bundle = _path_arg(args, "bundle")
    pack = _resolve_pack(
        _string_arg(args, "pack"),
        _path_arg(args, "packs_root"),
    )
    _complete_bundle(bundle)
    try:
        receipt = replay_bundle(bundle, pack_dir=pack)
    except (ReplayError, OSError) as exc:
        raise _CommandError(
            message=f"offline replay failed: {exc}",
            next_step=(
                "Use the exact pack referenced by the bundle and leave all "
                "sealed bundle bytes unchanged."
            ),
            exit_code=EXIT_EVIDENCE,
        ) from exc
    print(f"Replay matched: {receipt.run_id}", file=streams.stdout)
    print(f"Evidence root: {receipt.bundle_root}", file=streams.stdout)
    print(
        "Compared files: " + ", ".join(receipt.files_compared),
        file=streams.stdout,
    )
    print(
        f"Decision records compared: {receipt.decisions_compared}",
        file=streams.stdout,
    )
    return 0


def _cmd_share(args: argparse.Namespace, streams: _Streams) -> int:
    bundle = _path_arg(args, "bundle")
    output = _optional_path_arg(args, "output")
    if output is None:
        output = Path("shares") / bundle.name
    _require_fresh(
        output,
        label="share-bundle output directory",
        next_step=(
            "Choose a fresh --output directory to avoid overwriting evidence."
        ),
    )
    _complete_bundle(bundle)
    try:
        result = create_share_bundle(bundle, output)
    except (RedactionError, OSError) as exc:
        raise _CommandError(
            message=f"share-bundle generation failed: {exc}",
            next_step=(
                "Leave the parent bundle unchanged, choose a fresh destination, "
                "and verify that committed raw response blobs are present."
            ),
            exit_code=EXIT_EVIDENCE,
        ) from exc
    print(f"Share bundle: {result.path}", file=streams.stdout)
    print(f"Parent evidence root: {result.parent_root}", file=streams.stdout)
    print(f"Raw response blobs removed: {result.removals}", file=streams.stdout)
    print("Verifier verdict: COMPLETE (with disclosed absences)", file=streams.stdout)
    print(
        "Claim label: survival-stress; this is survival/stress evidence only "
        "and does not establish predictive capability or future performance.",
        file=streams.stdout,
    )
    return 0


def _write_error(error: _CommandError, streams: _Streams) -> int:
    print(f"error: {error.message}", file=streams.stderr)
    print(f"next: {error.next_step}", file=streams.stderr)
    return error.exit_code


def _dispatch(argv: Sequence[str], streams: _Streams) -> int:
    """Parse one argument vector and run its handler on these streams.

    The guided wizard composes argument vectors and calls this, so an
    interactive run reaches exactly the same parser, handlers, estimate and
    spend-confirmation gates as the typed command it prints.
    """

    parser = _parser()
    try:
        with redirect_stdout(streams.stdout), redirect_stderr(streams.stderr):
            args = parser.parse_args(list(argv))
    except _ParserExit as exc:
        destination = streams.stdout if exc.status == 0 else streams.stderr
        if exc.message:
            print(exc.message, end="", file=destination)
        if exc.status != 0:
            print(
                "next: run `wagmibench --help` or "
                "`wagmibench COMMAND --help`.",
                file=streams.stderr,
            )
        return exc.status
    handler_value = vars(args).get("handler")
    if not callable(handler_value):
        return _write_error(
            _CommandError(
                message="no command handler was selected",
                next_step="Run `wagmibench --help` and choose a command.",
                exit_code=EXIT_USAGE,
            ),
            streams,
        )
    handler = cast(
        Callable[[argparse.Namespace, _Streams], int],
        handler_value,
    )
    try:
        return handler(args, streams)
    except _CommandError as exc:
        return _write_error(exc, streams)
    except KeyboardInterrupt:
        return _write_error(
            _CommandError(
                message="command interrupted by the operator",
                next_step=(
                    "Inspect any partial output before retrying; immutable "
                    "bundle directories must not be reused."
                ),
                exit_code=130,
            ),
            streams,
        )
    except Exception as exc:
        return _write_error(
            _CommandError(
                message=f"unexpected {type(exc).__name__}",
                next_step=(
                    "Preserve any output, rerun with COMMAND --help to verify "
                    "inputs, and report this as a bug if it persists."
                ),
                exit_code=1,
            ),
            streams,
        )


def _stdin_is_interactive(stream: IO[str]) -> bool:
    """Report whether this exact stream is an operator's terminal.

    The check reads the injected stream rather than ``sys.stdin`` so tests
    and pipelines stay on the non-interactive path deterministically.
    """

    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run one CLI command without leaking a traceback for operator errors."""

    streams = _Streams(
        stdin=sys.stdin if stdin is None else stdin,
        stdout=sys.stdout if stdout is None else stdout,
        stderr=sys.stderr if stderr is None else stderr,
    )
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments and _stdin_is_interactive(streams.stdin):
        # A newcomer typing one word at a terminal gets guided; a script or
        # pipeline with no arguments still gets today's usage error.
        arguments = ["wizard"]
    return _dispatch(arguments, streams)


if __name__ == "__main__":  # pragma: no cover - module execution entry
    raise SystemExit(main())
