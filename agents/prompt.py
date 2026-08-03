# SPDX-License-Identifier: Apache-2.0
"""Prompt loading and exact-byte hashing utilities."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Sequence

DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "baseline-system.txt"
)


def load_prompt(path: str | Path = DEFAULT_PROMPT_PATH) -> str:
    """Load prompt bytes as strict UTF-8 without normalizing newlines."""

    return Path(path).read_bytes().decode("utf-8")


def prompt_sha256(path: str | Path = DEFAULT_PROMPT_PATH) -> str:
    """Return the IC-5 prompt commitment over exact stored bytes."""

    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return f"sha256:{digest}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the exact-byte SHA-256 commitment for an agent prompt.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=str(DEFAULT_PROMPT_PATH),
    )
    args = parser.parse_args(argv)
    print(prompt_sha256(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
