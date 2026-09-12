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
    channels_file: Path          # config\channels.yaml — каналы владельца (ТЗ §5.2)
    channels_example: Path
    secrets_dir: Path
    client_secret_file: Path     # secrets\client_secret.json — паспорт программы (ТЗ §5.3)
    promo_dir: Path
    state_dir: Path
    registry_file: Path
    channels_state_file: Path    # state\channels.json — привязки каналов (ТЗ §5.3)
    keystreams_dir: Path
    keys_file: Path
    logs_dir: Path

    @property
    def directories(self) -> tuple[Path, ...]:
        return (
            self.config_dir,
            self.secrets_dir,
            self.promo_dir,
            self.state_dir,
            self.keystreams_dir,
            self.logs_dir,
        )


def build_paths(root: Path) -> PlanerPaths:
    config_dir: Path = root / "config"
    secrets_dir: Path = root / "secrets"
    state_dir: Path = root / "state"
    keystreams_dir: Path = root / "keystreams"
    return PlanerPaths(
        root=root,
        config_dir=config_dir,
        config_file=config_dir / "planer.yaml",
        config_example=config_dir / "planer.example.yaml",
        channels_file=config_dir / "channels.yaml",
        channels_example=config_dir / "channels.example.yaml",
        secrets_dir=secrets_dir,
        client_secret_file=secrets_dir / "client_secret.json",
        promo_dir=root / "promo",
        state_dir=state_dir,
        registry_file=state_dir / "registry.json",
        channels_state_file=state_dir / "channels.json",
        keystreams_dir=keystreams_dir,
        keys_file=keystreams_dir / "keys.txt",
        logs_dir=root / "logs",
    )


def ensure_dirs(paths: PlanerPaths) -> None:
    for directory in paths.directories:
        directory.mkdir(parents=True, exist_ok=True)
