# SPDX-License-Identifier: Apache-2.0
"""EvoSkill-format skill parsing and policy compilation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.evoskill import (
    MAX_TOTAL_SKILL_BYTES,
    EvoSkillPolicy,
    SkillFormatError,
    compile_system_prompt,
    compiled_prompt_sha256,
    load_skill,
    load_skills,
)
from agents.prompt import load_prompt
from agents.server import build_policy_from_env

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_SKILLS = ROOT / "examples" / "evoskill" / ".claude" / "skills"


def test_example_skill_parses_as_evoskill_format() -> None:
    skill = load_skill(EXAMPLE_SKILLS / "perps-leverage-discipline")
    assert skill.name == "perps-leverage-discipline"
    assert skill.description.startswith("Position sizing")
    assert "action/v1" in skill.body
    assert skill.sha256.startswith("sha256:")
    assert skill.has_scripts is False


def test_skill_name_must_match_directory(tmp_path: Path) -> None:
    directory = tmp_path / "my-skill"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: d\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillFormatError, match="must match directory"):
        load_skill(directory)


def test_frontmatter_is_required_and_terminated(tmp_path: Path) -> None:
    directory = tmp_path / "bad-skill"
    directory.mkdir()
    (directory / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
    with pytest.raises(SkillFormatError, match="must start with"):
        load_skill(directory)
    (directory / "SKILL.md").write_text(
        "---\nname: bad-skill\ndescription: d\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillFormatError):
        load_skill(directory)


def test_skill_text_budget_is_enforced(tmp_path: Path) -> None:
    directory = tmp_path / "big-skill"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: big-skill\ndescription: d\n---\n"
        + "x" * (MAX_TOTAL_SKILL_BYTES + 1),
        encoding="utf-8",
    )
    with pytest.raises(SkillFormatError, match="prompt budget"):
        load_skills(tmp_path)


def test_compiled_prompt_is_deterministic_and_committed() -> None:
    skills = load_skills(EXAMPLE_SKILLS)
    base = load_prompt()
    first = compile_system_prompt(base, skills)
    second = compile_system_prompt(base, skills)
    assert first == second
    assert first.startswith(base.rstrip("\n"))
    assert "## Skill: perps-leverage-discipline" in first
    assert compiled_prompt_sha256(first) == compiled_prompt_sha256(second)


def test_policy_from_env_builds_and_carries_skill_prompt() -> None:
    environ = {
        "TRADEVOLVE_AGENT_MODE": "evoskill",
        "TRADEVOLVE_EVOSKILL_SKILLS_DIR": str(EXAMPLE_SKILLS),
        "TRADEVOLVE_LLM_MODEL": "accounts/fireworks/models/kimi-k3",
        "FIREWORKS_API_KEY": "test-key-not-real",
    }
    policy = build_policy_from_env(environ)
    assert isinstance(policy, EvoSkillPolicy)
    assert "## Skill: perps-leverage-discipline" in policy.system_prompt
    assert policy.inner.system_prompt == policy.system_prompt
    assert len(policy.skills) == 1
