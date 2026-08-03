# SPDX-License-Identifier: Apache-2.0
"""Guided interactive mode: one word to a first survival report.

Every action screen prints the equivalent non-interactive command before
executing it, so an operator graduates from the wizard to scripting without
reverse-engineering flags. The wizard owns no run, estimate, spend, or
manifest logic of its own: it composes argument vectors and dispatches them
through the same parser and handlers a typed command uses, so the
estimate/confirm gate and every evidence invariant are identical on both
paths.

All input and output flows through the injected ``_Streams`` object rather
than ``input()``/``sys.stdin``, which is what makes the flow testable and
keeps a piped or redirected session honest.
"""

from __future__ import annotations

import getpass
import os
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Final

from agents.evoskill import SkillFormatError, load_skills
from agents.llm import PROVIDER_BASE_URLS, provider_key_name
from cli.main import (
    _REFERENCE_PRICING,
    EXIT_INPUT,
    EXIT_USAGE,
    _dispatch,
    _Streams,
)
from cli.packs import pack_local_state
from data.catalog import available_pack_ids
from sandbox.orchestration import PreflightFailure, load_protected_credentials

_HELP_HINT: Final = (
    "Next: run `uv run wagmibench --help` for every command."
)
# Three tries is enough for a typo and short enough that a wedged or
# non-interactive stream cannot spin the prompt forever.
_MAX_ATTEMPTS: Final = 3
_MAX_FRESH_SUFFIX: Final = 999
_MAX_COMPARE_ENTRIES: Final = 64
_DEFAULT_MAX_OUTPUT_TOKENS: Final = 2000
_DEFAULT_ADAPTER_URL: Final = "http://127.0.0.1:8000"
_DEFAULT_SCAFFOLD_DIR: Final = "wagmibench-agent"
_DEFAULT_SKILLS_DIR: Final = ".claude/skills"
_ENV_FILE: Final = Path(".env")
_BUNDLES_ROOT: Final = Path("bundles")
_REPORTS_ROOT: Final = Path("reports")
_PACKS_ROOT: Final = Path("packs")
_GOLDEN_PACK: Final = Path("fixtures") / "golden-mini" / "pack"
_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
_BASELINE_AGENTS: Final[tuple[str, ...]] = (
    "buyhold",
    "shorthold",
    "flat",
    "momentum",
)
_PROVIDER_MODEL_PREFIXES: Final[Mapping[str, str]] = {
    "anthropic": "claude-",
    "fireworks": "accounts/fireworks/",
}
_MENU: Final[tuple[tuple[str, str], ...]] = (
    ("1", "Quick demo — keyless, ~30 seconds, no network"),
    ("2", "Benchmark a hosted model with my API key"),
    ("3", "Benchmark my own agent (HTTP adapter or EvoSkill skills)"),
    ("4", "Compare existing result bundles"),
    ("q", "Quit"),
)


@dataclass(frozen=True, slots=True)
class _WizardExit(Exception):
    """Leave the guided flow with an exit code and no traceback."""

    code: int


def _say(streams: _Streams, text: str = "") -> None:
    print(text, file=streams.stdout)


def _ask(streams: _Streams, prompt: str) -> str:
    """Write one prompt and read one stripped line, or leave on EOF."""

    print(prompt, end="", file=streams.stdout, flush=True)
    line = streams.stdin.readline()
    if line == "":
        _say(streams)
        _say(streams, "Input ended; leaving guided mode. Nothing further ran.")
        _say(streams, _HELP_HINT)
        raise _WizardExit(code=0)
    return line.strip()


def _give_up(streams: _Streams, reason: str) -> _WizardExit:
    _say(streams, reason)
    _say(streams, _HELP_HINT)
    return _WizardExit(code=EXIT_USAGE)


def _ask_choice(
    streams: _Streams,
    prompt: str,
    options: Sequence[str],
    *,
    hint: str,
) -> str:
    allowed = tuple(options)
    for _attempt in range(_MAX_ATTEMPTS):
        answer = _ask(streams, prompt)
        if answer in allowed:
            return answer
        _say(streams, f"{answer!r} is not a listed choice. {hint}")
    raise _give_up(streams, "Too many unrecognized answers.")


def _ask_yes_no(streams: _Streams, question: str, *, default: bool) -> bool:
    prompt = f"{question} {'[Y/n]' if default else '[y/N]'}: "
    for _attempt in range(_MAX_ATTEMPTS):
        answer = _ask(streams, prompt).lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        _say(streams, "Answer y or n.")
    raise _give_up(streams, "Too many unrecognized answers.")


def _ask_text(
    streams: _Streams,
    label: str,
    *,
    default: str | None = None,
) -> str:
    prompt = label if default is None else f"{label} [{default}]"
    for _attempt in range(_MAX_ATTEMPTS):
        answer = _ask(streams, f"{prompt}: ")
        if answer:
            return answer
        if default is not None:
            return default
        _say(streams, "A value is required.")
    raise _give_up(streams, "Too many empty answers.")


def _ask_positive_int(streams: _Streams, label: str, *, default: int) -> int:
    for _attempt in range(_MAX_ATTEMPTS):
        answer = _ask(streams, f"{label} [{default}]: ")
        if not answer:
            return default
        try:
            value = int(answer)
        except ValueError:
            value = 0
        if value >= 1:
            return value
        _say(streams, "Enter a whole number of 1 or more.")
    raise _give_up(streams, "Too many invalid numbers.")


def _ask_price(streams: _Streams, label: str) -> str:
    for _attempt in range(_MAX_ATTEMPTS):
        answer = _ask(streams, f"{label} (USD per 1M tokens): ")
        try:
            parsed = Decimal(answer)
        except InvalidOperation:
            _say(streams, "Enter a decimal number such as 3.00.")
            continue
        if parsed.is_finite() and parsed >= 0:
            return answer
        _say(streams, "Enter a finite, non-negative decimal number.")
    raise _give_up(streams, "Too many invalid prices.")


def _fresh_path(streams: _Streams, parent: Path, stem: str) -> Path:
    """Return the first non-existing ``parent/stem[-N]``; never reuse one."""

    candidate = parent / stem
    if not candidate.exists():
        return candidate
    for index in range(2, _MAX_FRESH_SUFFIX + 1):
        candidate = parent / f"{stem}-{index}"
        if not candidate.exists():
            return candidate
    _say(
        streams,
        f"error: {parent}/{stem}-* is exhausted; evidence is immutable so "
        "the wizard will not reuse a directory.",
    )
    _say(streams, f"next: archive or remove old {parent}/{stem}* directories.")
    raise _WizardExit(code=EXIT_INPUT)


def _golden_pack() -> Path:
    """Locate the committed synthetic fixture from any working directory."""

    if _GOLDEN_PACK.is_dir():
        return _GOLDEN_PACK
    return _REPO_ROOT / _GOLDEN_PACK


def _run(streams: _Streams, argv: Sequence[str]) -> int:
    """Print the equivalent command, then execute it on these streams."""

    rendered = " ".join(shlex.quote(item) for item in argv)
    _say(streams, f"Equivalent command: uv run wagmibench {rendered}")
    _say(streams)
    return _dispatch(argv, streams)


def _require(streams: _Streams, argv: Sequence[str]) -> None:
    """Run one step and leave the wizard if it did not succeed."""

    code = _run(streams, argv)
    if code != 0:
        raise _WizardExit(code=code)
    _say(streams)


def _banner(streams: _Streams) -> None:
    _say(
        streams,
        "WAGMI Bench guided mode — deterministic BTC perpetual-futures "
        "survival runs, sealed as verifiable evidence.",
    )
    _say(
        streams,
        "Claim label: survival-stress — evidence about liquidation "
        "survival, drawdown, funding drag, and rule-following under "
        "stress; it does not establish predictive ability or future "
        "performance.",
    )
    _say(streams)


def _demo(streams: _Streams) -> int:
    _say(
        streams,
        "Quick demo: the keyless reference policy on the committed synthetic "
        "golden fixture. No network request, no API key, no spending.",
    )
    _say(streams)
    bundle = _fresh_path(streams, _BUNDLES_ROOT, "wizard-demo")
    _require(
        streams,
        ["run", "--pack", str(_golden_pack()), "--output", str(bundle)],
    )
    report = _fresh_path(streams, _REPORTS_ROOT, bundle.name)
    _require(
        streams,
        ["report", "--bundle", str(bundle), "--output", str(report)],
    )
    _say(streams, "=" * 60)
    _say(streams, f"Open your report: {report / 'report.html'}")
    _say(streams, "=" * 60)
    _say(streams)
    _say(
        streams,
        "Next: run the wizard again and choose 2 to put a hosted model "
        "through the same machinery, or 4 to compare sealed bundles.",
    )
    return 0


def _provider_choice(streams: _Streams) -> str:
    providers = tuple(sorted(PROVIDER_BASE_URLS))
    _say(streams, "Which inference provider?")
    for index, provider in enumerate(providers, start=1):
        base_url = PROVIDER_BASE_URLS[provider]
        _say(streams, f"  {index}) {provider:<11} {base_url}")
    answer = _ask_choice(
        streams,
        "Provider number: ",
        tuple(str(index) for index in range(1, len(providers) + 1)),
        hint=f"Enter 1-{len(providers)}.",
    )
    return providers[int(answer) - 1]


def _pinned_models(provider: str) -> tuple[str, ...]:
    prefix = _PROVIDER_MODEL_PREFIXES.get(provider)
    if prefix is None:
        return ()
    return tuple(
        model
        for model in sorted(_REFERENCE_PRICING)
        if model.startswith(prefix)
    )


def _model_choice(streams: _Streams, provider: str) -> str:
    suggestions = _pinned_models(provider)
    _say(streams)
    if not suggestions:
        _say(
            streams,
            f"This CLI pins no reference price row for {provider}, so you "
            "will be asked for both token prices after choosing a model.",
        )
        return _ask_text(streams, "Model id")
    _say(streams, f"Models with a pinned {provider} price row:")
    for index, model in enumerate(suggestions, start=1):
        input_price, output_price = _REFERENCE_PRICING[model]
        _say(
            streams,
            f"  {index}) {model}  (${input_price} in / ${output_price} out "
            "per 1M tokens)",
        )
    _say(
        streams,
        "Any other model id is accepted; it then needs explicit prices.",
    )
    answer = _ask_text(
        streams,
        "Model number or id",
        default=suggestions[0],
    )
    if answer.isdigit() and 1 <= int(answer) <= len(suggestions):
        return suggestions[int(answer) - 1]
    return answer


def _pricing_flags(streams: _Streams, *, model: str) -> tuple[str, ...]:
    if model in _REFERENCE_PRICING:
        return ()
    _say(streams)
    _say(
        streams,
        f"No pinned price row for {model!r}. The worst-case estimate needs "
        "both prices from the provider's current official pricing page.",
    )
    input_price = _ask_price(streams, "Input price")
    output_price = _ask_price(streams, "Output price")
    return (
        "--input-usd-per-million",
        input_price,
        "--output-usd-per-million",
        output_price,
    )


def _stdin_is_real_terminal(stream: IO[str]) -> bool:
    """Report whether hidden entry via ``getpass`` can work on this stream."""

    if stream is not sys.stdin:
        return False
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


def _read_secret(streams: _Streams, name: str) -> str:
    if _stdin_is_real_terminal(streams.stdin):
        _say(streams, f"Enter your {name} (input stays hidden):")
        try:
            value = getpass.getpass("")
        except (EOFError, OSError) as exc:
            _say(streams, f"error: could not read the key privately: {exc}")
            _say(streams, f"next: export {name} yourself, then rerun.")
            raise _WizardExit(code=EXIT_INPUT) from exc
    else:
        _say(
            streams,
            f"WARNING: {name} will be ECHOED in this terminal as you type. "
            "Make sure nobody is watching the screen or recording it.",
        )
        value = _ask(streams, f"{name}: ")
    value = value.strip()
    if not value:
        _say(streams, f"error: no {name} value was entered.")
        _say(
            streams,
            f"next: export {name}, or add a `{name}=...` line to a gitignored "
            f"{_ENV_FILE}, then rerun.",
        )
        raise _WizardExit(code=EXIT_INPUT)
    return value


def _append_credential(streams: _Streams, name: str, value: str) -> None:
    """Append one ``NAME=value`` line, never echoing the value back."""

    try:
        separator = ""
        if _ENV_FILE.is_file():
            existing = _ENV_FILE.read_bytes()
            if existing and not existing.endswith(b"\n"):
                separator = "\n"
        with _ENV_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{separator}{name}={value}\n")
    except OSError as exc:
        _say(streams, f"Could not write {_ENV_FILE}: {exc}")
        _say(streams, "The key is still set for this session only.")
        return
    _say(
        streams,
        f"Appended {name} to {_ENV_FILE}. Keep that file gitignored; its "
        "value is never printed, manifested, or recorded.",
    )


def _ensure_credential(streams: _Streams, *, base_url: str) -> None:
    try:
        name = provider_key_name(base_url)
    except ValueError as exc:
        _say(streams, f"error: no canonical key name for {base_url}: {exc}")
        _say(streams, "next: choose a listed provider.")
        raise _WizardExit(code=EXIT_INPUT) from exc
    _say(streams)
    if os.environ.get(name):
        _say(streams, f"Using {name} from the environment.")
        return
    try:
        load_protected_credentials(_ENV_FILE, (name,))
    except PreflightFailure:
        pass
    else:
        _say(streams, f"Using {name} from {_ENV_FILE}.")
        return
    _say(streams, f"No {name} in the environment or in {_ENV_FILE}.")
    value = _read_secret(streams, name)
    # Kept in this process only; the run reads the environment first, and the
    # value never reaches a manifest, bundle, report, or printed message.
    os.environ[name] = value
    _say(streams, f"{name} is set for this session.")
    if _ask_yes_no(
        streams,
        f"Append {name} to {_ENV_FILE} for future runs?",
        default=False,
    ):
        _append_credential(streams, name, value)


def _pack_choice(streams: _Streams) -> tuple[str, str]:
    """Return the ``--pack`` reference and a short label for output names."""

    catalog = available_pack_ids()
    _say(streams)
    _say(streams, "Which scenario pack?")
    _say(
        streams,
        "   0) golden-mini fixture — synthetic, tiny, already local, "
        "cheapest first run",
    )
    for index, pack_id in enumerate(catalog, start=1):
        state = pack_local_state(_PACKS_ROOT / pack_id)
        _say(streams, f"  {index:>2}) {pack_id:<31} {state}")
    answer = _ask_choice(
        streams,
        "Pack number: ",
        ("0",) + tuple(str(index) for index in range(1, len(catalog) + 1)),
        hint=f"Enter 0-{len(catalog)}.",
    )
    if answer == "0":
        return str(_golden_pack()), "golden-mini"
    pack_id = catalog[int(answer) - 1]
    state = pack_local_state(_PACKS_ROOT / pack_id)
    if state != "ready":
        _say(streams)
        _say(
            streams,
            f"{pack_id} is {state}: its market series are not built locally.",
        )
        if not _ask_yes_no(
            streams,
            "Fetch and build it now? (downloads checksum-verified archives)",
            default=True,
        ):
            _say(streams, "Nothing was run.")
            _say(
                streams,
                f"Next: uv run wagmibench fetch-data --pack {pack_id}",
            )
            raise _WizardExit(code=0)
        _say(streams)
        _require(streams, ["fetch-data", "--pack", pack_id])
    return pack_id, pack_id


def _baseline_comparison(
    streams: _Streams,
    *,
    pack_ref: str,
    pack_label: str,
    candidate: Path,
) -> None:
    bundles = [candidate]
    for agent in _BASELINE_AGENTS:
        bundle = _fresh_path(
            streams,
            _BUNDLES_ROOT,
            f"wizard-{pack_label}-{agent}",
        )
        _require(
            streams,
            [
                "run",
                "--pack",
                pack_ref,
                "--agent",
                agent,
                "--output",
                str(bundle),
            ],
        )
        bundles.append(bundle)
    output = _fresh_path(
        streams,
        _REPORTS_ROOT,
        f"wizard-{pack_label}-compare",
    )
    argv = ["compare"]
    for bundle in bundles:
        argv.extend(["--bundle", str(bundle)])
    argv.extend(["--output", str(output)])
    _require(streams, argv)
    _say(streams, f"Comparison table and JSON: {output}")


def _hosted_model(streams: _Streams) -> int:
    _say(
        streams,
        "Benchmark a hosted model. This spends money at your provider: the "
        "run prints a worst-case token/cost estimate and requires a typed "
        "`yes` before the first paid request.",
    )
    _say(streams)
    provider = _provider_choice(streams)
    model = _model_choice(streams, provider)
    pricing = _pricing_flags(streams, model=model)
    _ensure_credential(streams, base_url=PROVIDER_BASE_URLS[provider])
    pack_ref, pack_label = _pack_choice(streams)
    _say(streams)
    max_output_tokens = _ask_positive_int(
        streams,
        "Maximum output tokens per decision",
        default=_DEFAULT_MAX_OUTPUT_TOKENS,
    )
    bundle = _fresh_path(streams, _BUNDLES_ROOT, f"wizard-{pack_label}")
    _say(streams)
    argv = [
        "run",
        "--pack",
        pack_ref,
        "--agent",
        "llm-local",
        "--llm-provider",
        provider,
        "--model",
        model,
        "--max-output-tokens",
        str(max_output_tokens),
        "--output",
        str(bundle),
        *pricing,
    ]
    _require(streams, argv)
    report = _fresh_path(streams, _REPORTS_ROOT, bundle.name)
    _require(
        streams,
        ["report", "--bundle", str(bundle), "--output", str(report)],
    )
    _say(streams, "=" * 60)
    _say(streams, f"Open your report: {report / 'report.html'}")
    _say(streams, "=" * 60)
    _say(streams)
    if _ask_yes_no(
        streams,
        "Compare against the keyless baselines? (runs buyhold/shorthold/"
        "flat/momentum on this pack — free, no model requests)",
        default=True,
    ):
        _say(streams)
        _baseline_comparison(
            streams,
            pack_ref=pack_ref,
            pack_label=pack_label,
            candidate=bundle,
        )
    return 0


def _scaffold_agent(streams: _Streams) -> int:
    directory = _ask_text(
        streams,
        "New scaffold directory",
        default=_DEFAULT_SCAFFOLD_DIR,
    )
    _say(streams)
    # The init handler writes its own next steps to these streams; they are
    # reproduced verbatim rather than paraphrased here.
    _require(streams, ["init", directory])
    return 0


def _http_agent(streams: _Streams) -> int:
    _say(
        streams,
        "Point the runner at an adapter that already serves the IC-6 "
        "/healthz and /decide contract.",
    )
    url = _ask_text(streams, "Adapter URL", default=_DEFAULT_ADAPTER_URL)
    name = _ask_text(
        streams,
        "Agent name for the sealed manifest",
        default="custom-http-agent",
    )
    pack_ref, pack_label = _pack_choice(streams)
    bundle = _fresh_path(streams, _BUNDLES_ROOT, f"wizard-{pack_label}-http")
    _say(streams)
    _require(
        streams,
        [
            "run",
            "--pack",
            pack_ref,
            "--agent",
            "http",
            "--agent-url",
            url,
            "--agent-name",
            name,
            "--output",
            str(bundle),
        ],
    )
    report = _fresh_path(streams, _REPORTS_ROOT, bundle.name)
    _require(
        streams,
        ["report", "--bundle", str(bundle), "--output", str(report)],
    )
    _say(streams, f"Open your report: {report / 'report.html'}")
    return 0


def _evoskill_agent(streams: _Streams) -> int:
    _say(
        streams,
        "EvoSkill skills become the policy of an adapter process. That "
        "needs two terminals, so nothing is started for you here.",
    )
    folder = _ask_text(
        streams,
        "EvoSkill skills folder",
        default=_DEFAULT_SKILLS_DIR,
    )
    try:
        skills = load_skills(folder)
    except SkillFormatError as exc:
        _say(streams)
        _say(streams, f"error: {exc}")
        _say(
            streams,
            "next: point at the `.claude/skills` folder an EvoSkill run "
            "produced; each subfolder needs a SKILL.md whose frontmatter "
            "carries `name` (matching the folder) and `description`.",
        )
        raise _WizardExit(code=EXIT_INPUT) from exc
    except OSError as exc:
        _say(streams)
        _say(streams, f"error: could not read {folder}: {exc}")
        _say(streams, "next: check the path and its read permissions.")
        raise _WizardExit(code=EXIT_INPUT) from exc
    _say(streams)
    _say(
        streams,
        f"Parsed {len(skills)} skill folder(s): "
        + ", ".join(skill.name for skill in skills),
    )
    _say(streams)
    _say(streams, "Terminal 1 — start the adapter:")
    _say(streams, "  export TRADEVOLVE_AGENT_MODE=evoskill")
    _say(
        streams,
        "  export TRADEVOLVE_EVOSKILL_SKILLS_DIR="
        + shlex.quote(folder),
    )
    _say(streams, "  export TRADEVOLVE_LLM_PROVIDER=fireworks")
    _say(
        streams,
        "  export TRADEVOLVE_LLM_MODEL=accounts/fireworks/models/kimi-k3",
    )
    _say(
        streams,
        f"  export TRADEVOLVE_LLM_MAX_TOKENS={_DEFAULT_MAX_OUTPUT_TOKENS}",
    )
    _say(streams, "  uv run python -m agents.server")
    _say(streams)
    _say(
        streams,
        "Terminal 2 — keep `FIREWORKS_API_KEY=...` in a gitignored .env, "
        "then run a pack against it:",
    )
    _say(streams, "  uv run wagmibench run \\")
    _say(streams, f"    --pack {_golden_pack()} \\")
    _say(streams, f"    --agent llm --agent-url {_DEFAULT_ADAPTER_URL} \\")
    _say(streams, "    --llm-provider fireworks \\")
    _say(streams, "    --model accounts/fireworks/models/kimi-k3 \\")
    _say(
        streams,
        f"    --max-output-tokens {_DEFAULT_MAX_OUTPUT_TOKENS} \\",
    )
    _say(streams, "    --output bundles/evoskill-run")
    _say(streams)
    _say(
        streams,
        "Then: uv run wagmibench report --bundle bundles/evoskill-run",
    )
    _say(
        streams,
        "`uv run wagmibench packs list` shows the other 13 scenario windows.",
    )
    return 0


def _own_agent(streams: _Streams) -> int:
    _say(streams, "How is your agent packaged?")
    _say(streams, "  a) Scaffold a new local HTTP adapter for me")
    _say(streams, "  b) I already have an HTTP endpoint running")
    _say(streams, "  c) I have an EvoSkill skills folder")
    choice = _ask_choice(
        streams,
        "Choose a, b, or c: ",
        ("a", "b", "c"),
        hint="Enter a, b, or c.",
    )
    _say(streams)
    if choice == "a":
        return _scaffold_agent(streams)
    if choice == "b":
        return _http_agent(streams)
    return _evoskill_agent(streams)


def _collect_bundles(streams: _Streams) -> tuple[Path, ...]:
    collected: list[Path] = []
    for _entry in range(_MAX_COMPARE_ENTRIES):
        answer = _ask(streams, f"Bundle {len(collected) + 1} (blank to end): ")
        if not answer:
            return tuple(collected)
        candidate = Path(answer)
        if not candidate.is_dir():
            _say(streams, f"No such bundle directory: {candidate}")
            continue
        collected.append(candidate)
    return tuple(collected)


def _compare_bundles(streams: _Streams) -> int:
    _say(
        streams,
        "Compare sealed bundles run on the same pack. Enter one directory "
        "per line; a blank line ends the list. At least two are required.",
    )
    bundles: tuple[Path, ...] = ()
    for _attempt in range(_MAX_ATTEMPTS):
        bundles = _collect_bundles(streams)
        if len(bundles) >= 2:
            break
        _say(
            streams,
            "Comparison needs at least two existing bundle directories "
            "(a candidate and at least one baseline).",
        )
        bundles = ()
    if len(bundles) < 2:
        raise _give_up(streams, "Too few bundles were listed.")
    argv = ["compare"]
    for bundle in bundles:
        argv.extend(["--bundle", str(bundle)])
    _say(streams)
    return _run(streams, argv)


_ACTIONS: Final[Mapping[str, Callable[[_Streams], int]]] = {
    "1": _demo,
    "2": _hosted_model,
    "3": _own_agent,
    "4": _compare_bundles,
}


def _flow(streams: _Streams) -> int:
    _banner(streams)
    _say(streams, "What do you want to do?")
    for key, label in _MENU:
        _say(streams, f"  {key}) {label}")
    choice = _ask_choice(
        streams,
        "Choose 1-4 or q: ",
        tuple(key for key, _label in _MENU),
        hint="Enter 1, 2, 3, 4, or q.",
    )
    _say(streams)
    if choice == "q":
        _say(streams, "Nothing was run.")
        _say(streams, _HELP_HINT)
        return 0
    return _ACTIONS[choice](streams)


def run_wizard(streams: _Streams) -> int:
    """Run the guided flow, returning an exit code and never a traceback."""

    try:
        return _flow(streams)
    except _WizardExit as leaving:
        return leaving.code
    except KeyboardInterrupt:
        _say(streams)
        _say(streams, "Interrupted; leaving guided mode. Nothing further ran.")
        return 130
