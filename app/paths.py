"""Корень планера и его папки (ТЗ §9): рядом с exe (frozen) или корень репо (dev)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT_ENV_VAR: Final[str] = "PLANER_ROOT"  # подмена корня — только для тестов и отладки


def resolve_root() -> Path:
    """PLANER_ROOT, если задан; иначе папка exe (frozen) или корень репо (родитель app\\)."""
    override: str = os.environ.get(ROOT_ENV_VAR, "").strip()
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class PlanerPaths:
    root: Path
    config_dir: Path
    config_file: Path
    config_example: Path
    secrets_dir: Path
    inbox_dir: Path
    inbox_done_dir: Path
    inbox_archive_dir: Path
    state_dir: Path
    registry_file: Path
    channels_file: Path
    out_dir: Path
    keys_file: Path
    reports_dir: Path
    logs_dir: Path

    @property
    def directories(self) -> tuple[Path, ...]:
        return (
            self.config_dir,
            self.secrets_dir,
            self.inbox_dir,
            self.inbox_done_dir,
            self.inbox_archive_dir,
            self.state_dir,
            self.out_dir,
            self.reports_dir,
            self.logs_dir,
        )


def build_paths(root: Path) -> PlanerPaths:
    config_dir: Path = root / "config"
    inbox_dir: Path = root / "inbox"
    state_dir: Path = root / "state"
    out_dir: Path = root / "out"
    return PlanerPaths(
        root=root,
        config_dir=config_dir,
        config_file=config_dir / "planer.yaml",
        config_example=config_dir / "planer.example.yaml",
        secrets_dir=root / "secrets",
        inbox_dir=inbox_dir,
        inbox_done_dir=inbox_dir / "done",
        inbox_archive_dir=inbox_dir / "archive",
        state_dir=state_dir,
        registry_file=state_dir / "registry.json",
        channels_file=state_dir / "channels.json",
        out_dir=out_dir,
        keys_file=out_dir / "keys.txt",
        reports_dir=root / "reports",
        logs_dir=root / "logs",
    )


def ensure_dirs(paths: PlanerPaths) -> None:
    for directory in paths.directories:
        directory.mkdir(parents=True, exist_ok=True)
