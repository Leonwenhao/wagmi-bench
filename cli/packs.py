# SPDX-License-Identifier: Apache-2.0
"""Safe local pack acquisition around committed manifest-only recipes."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core.pack import PackError, load_pack
from data.builder import BuiltPack
from data.catalog import fetch_and_build_pack


class PackAcquisitionError(RuntimeError):
    """A local pack cannot be installed without changing pinned evidence."""


@dataclass(frozen=True, slots=True)
class LocalPack:
    """A ready local pack plus acquisition state."""

    directory: Path
    content_hash: str
    series_paths: tuple[Path, ...]
    state: str


def pack_local_state(pack_dir: Path) -> str:
    """Return ``ready``, ``manifest-only``, or ``not-fetched`` offline."""

    try:
        load_pack(pack_dir)
    except PackError:
        if (pack_dir / "manifest.json").is_file():
            return "manifest-only"
        return "not-fetched"
    return "ready"


def _ready(pack_dir: Path, *, state: str) -> LocalPack:
    pack = load_pack(pack_dir)
    series = tuple(
        sorted(
            path
            for path in pack_dir.rglob("*.jsonl")
            if path.is_file()
        )
    )
    return LocalPack(
        directory=pack_dir,
        content_hash=pack.content_hash,
        series_paths=series,
        state=state,
    )


def _copy_exclusive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        target = destination.open("xb")
    except FileExistsError as exc:
        raise PackAcquisitionError(
            f"series appeared during hydration: {destination}"
        ) from exc
    try:
        with source.open("rb") as incoming, target:
            shutil.copyfileobj(incoming, target)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def _hydrate_committed_manifest(
    *,
    pack_id: str,
    raw_root: Path,
    packs_root: Path,
) -> LocalPack:
    destination = packs_root / pack_id
    manifest_path = destination / "manifest.json"
    if not destination.is_dir() or not manifest_path.is_file():
        raise PackAcquisitionError(
            f"{destination} exists but is not a manifest-only pack directory"
        )

    with tempfile.TemporaryDirectory(
        prefix=f"tradeevolve-{pack_id}-"
    ) as temporary:
        staging_root = Path(temporary) / "packs"
        built = fetch_and_build_pack(
            pack_id,
            raw_root=raw_root,
            packs_root=staging_root,
        )
        committed_manifest = manifest_path.read_bytes()
        generated_manifest = built.manifest_path.read_bytes()
        if committed_manifest != generated_manifest:
            raise PackAcquisitionError(
                "generated manifest is not byte-identical to the committed "
                f"manifest for {pack_id!r}; no series were installed"
            )

        planned: list[tuple[Path, Path]] = []
        for staged_path in built.series_paths:
            relative = staged_path.relative_to(built.directory)
            installed_path = destination / relative
            if installed_path.exists():
                if (
                    not installed_path.is_file()
                    or installed_path.read_bytes() != staged_path.read_bytes()
                ):
                    raise PackAcquisitionError(
                        f"local series conflicts with the pinned build: "
                        f"{installed_path}"
                    )
                continue
            planned.append((staged_path, installed_path))

        for staged_path, installed_path in planned:
            _copy_exclusive(staged_path, installed_path)

    try:
        return _ready(destination, state="hydrated")
    except (PackError, OSError) as exc:
        raise PackAcquisitionError(
            f"hydrated pack failed integrity loading: {exc}"
        ) from exc


def acquire_pack(
    pack_id: str,
    *,
    raw_root: Path,
    packs_root: Path,
) -> LocalPack:
    """Fetch a new pack or hydrate a committed manifest without replacing it."""

    destination = packs_root / pack_id
    state = pack_local_state(destination)
    if state == "ready":
        return _ready(destination, state="already-ready")
    if destination.exists():
        return _hydrate_committed_manifest(
            pack_id=pack_id,
            raw_root=raw_root,
            packs_root=packs_root,
        )
    built: BuiltPack = fetch_and_build_pack(
        pack_id,
        raw_root=raw_root,
        packs_root=packs_root,
    )
    try:
        return _ready(built.directory, state="built")
    except (PackError, OSError) as exc:
        raise PackAcquisitionError(
            f"newly built pack failed integrity loading: {exc}"
        ) from exc
