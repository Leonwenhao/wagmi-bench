# SPDX-License-Identifier: Apache-2.0
"""EvoSkill-format skill consumption for WAGMI Bench contestants.

EvoSkill (github.com/sentient-agi/EvoSkill, Apache-2.0) evolves Agent-Skills
format artifacts: ``.claude/skills/<name>/SKILL.md`` with YAML frontmatter
(``name``, ``description``) followed by markdown instructions for an LLM.
Its deployment guidance is "copy ``.claude/skills/`` into your deployment".

This module is that deployment target for a single-shot /decide policy: the
skill bodies are compiled deterministically into the system prompt of the
existing LLM baseline. It consumes EvoSkill artifacts; it does not run the
evolution loop. ``scripts/`` helpers inside a skill are never executed —
only ``SKILL.md`` and ``references/*.md`` text reach the prompt.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agents.common import JsonObject
from agents.llm import LLMBaselinePolicy
from agents.prompt import DEFAULT_PROMPT_PATH, load_prompt

DEFAULT_SKILLS_DIR = ".claude/skills"
MAX_TOTAL_SKILL_BYTES = 65_536
_NAME_ALLOWED = "abcdefghijklmnopqrstuvwxyz0123456789-"


class SkillFormatError(ValueError):
    """A skill directory violates the EvoSkill/Agent-Skills format."""


@dataclass(frozen=True, slots=True)
class Skill:
    """One parsed EvoSkill-format skill."""

    name: str
    description: str
    body: str
    references: tuple[tuple[str, str], ...]
    sha256: str
    has_scripts: bool


def _parse_frontmatter(text: str, source: str) -> tuple[dict[str, str], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillFormatError(f"{source}: SKILL.md must start with '---'")
    fields: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            body = "\n".join(lines[index + 1 :]).strip("\n")
            return fields, body
        if not line.strip() or line.lstrip() != line:
            # Nested/empty YAML lines carry no fields this consumer needs.
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillFormatError(
                f"{source}: malformed frontmatter line {index + 1}"
            )
        fields[key.strip()] = value.strip().strip("'\"")
    raise SkillFormatError(f"{source}: unterminated frontmatter")


def load_skill(skill_dir: str | Path) -> Skill:
    directory = Path(skill_dir)
    skill_md = directory / "SKILL.md"
    if not skill_md.is_file():
        raise SkillFormatError(f"{directory}: missing SKILL.md")
    raw = skill_md.read_bytes()
    fields, body = _parse_frontmatter(
        raw.decode("utf-8"), str(skill_md)
    )
    name = fields.get("name", "")
    description = fields.get("description", "")
    if not name or not description:
        raise SkillFormatError(
            f"{skill_md}: frontmatter requires 'name' and 'description'"
        )
    if name != directory.name:
        raise SkillFormatError(
            f"{skill_md}: name {name!r} must match directory "
            f"{directory.name!r}"
        )
    if any(character not in _NAME_ALLOWED for character in name):
        raise SkillFormatError(
            f"{skill_md}: name must be lowercase with hyphens only"
        )
    references: list[tuple[str, str]] = []
    references_dir = directory / "references"
    if references_dir.is_dir():
        for path in sorted(references_dir.glob("*.md")):
            references.append((path.name, path.read_text("utf-8").strip("\n")))
    digest = hashlib.sha256(raw)
    for reference_name, reference_text in references:
        digest.update(reference_name.encode("utf-8"))
        digest.update(reference_text.encode("utf-8"))
    return Skill(
        name=name,
        description=description,
        body=body,
        references=tuple(references),
        sha256=f"sha256:{digest.hexdigest()}",
        has_scripts=(directory / "scripts").is_dir(),
    )


def load_skills(skills_root: str | Path) -> tuple[Skill, ...]:
    root = Path(skills_root)
    if not root.is_dir():
        raise SkillFormatError(f"{root}: skills directory does not exist")
    skills = tuple(
        load_skill(entry)
        for entry in sorted(root.iterdir(), key=lambda path: path.name)
        if entry.is_dir()
    )
    if not skills:
        raise SkillFormatError(f"{root}: contains no skill directories")
    total = sum(
        len(skill.body.encode("utf-8"))
        + sum(len(text.encode("utf-8")) for _name, text in skill.references)
        for skill in skills
    )
    if total > MAX_TOTAL_SKILL_BYTES:
        raise SkillFormatError(
            f"{root}: skill text totals {total} bytes, exceeding the "
            f"{MAX_TOTAL_SKILL_BYTES}-byte prompt budget"
        )
    return skills


def compile_system_prompt(base_prompt: str, skills: tuple[Skill, ...]) -> str:
    """Deterministically append skill sections to the contract prompt."""

    sections = [base_prompt.rstrip("\n")]
    for skill in skills:
        block = [f"## Skill: {skill.name}", "", skill.description, ""]
        if skill.body:
            block.extend([skill.body, ""])
        for reference_name, reference_text in skill.references:
            block.extend(
                [f"### Reference: {reference_name}", "", reference_text, ""]
            )
        sections.append("\n".join(block).rstrip("\n"))
    return "\n\n".join(sections) + "\n"


def compiled_prompt_sha256(prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True, slots=True)
class EvoSkillPolicy:
    """LLM baseline policy whose system prompt carries EvoSkill skills."""

    inner: LLMBaselinePolicy
    skills: tuple[Skill, ...]
    system_prompt: str

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> EvoSkillPolicy:
        source = os.environ if environ is None else environ
        skills_root = source.get(
            "TRADEVOLVE_EVOSKILL_SKILLS_DIR", DEFAULT_SKILLS_DIR
        )
        skills = load_skills(skills_root)
        prompt = compile_system_prompt(load_prompt(DEFAULT_PROMPT_PATH), skills)
        inner = LLMBaselinePolicy.from_env(source)
        inner = LLMBaselinePolicy(
            provider=inner.provider,
            config=inner.config,
            system_prompt=prompt,
        )
        return cls(inner=inner, skills=skills, system_prompt=prompt)

    def decide(self, request: Mapping[str, object]) -> JsonObject:
        return self.inner.decide(request)
