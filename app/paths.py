"""Корень планера и его папки (ТЗ §9): рядом с exe (frozen) или корень репо (dev).

Владелец видит в корне только своё: secrets\\ (каналы, настройки и ключи доступа),
bcast\\ (пакеты), keystreams\\ (ключи потоков), logs\\ (логи и отчёты).
"""
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
    secrets_dir: Path            # secrets\ — всё владельца: конфиги, паспорт, токены (token_file_for)
    config_file: Path            # secrets\planer.json — настройки планера, поставляются со сборкой (ТЗ §5.2)
    channels_file: Path          # secrets\channels.json — каналы владельца (ТЗ §5.2)
    client_secret_file: Path     # secrets\client_secret.json — паспорт программы (ТЗ §5.3)
    bcast_dir: Path              # bcast\ — пакеты броадкастера (ТЗ §7.1)
    keystreams_dir: Path
    keys_file: Path
    logs_dir: Path

    @property
    def directories(self) -> tuple[Path, ...]:
        return (
            self.secrets_dir,
            self.bcast_dir,
            self.keystreams_dir,
            self.logs_dir,
        )


def build_paths(root: Path) -> PlanerPaths:
    secrets_dir: Path = root / "secrets"
    keystreams_dir: Path = root / "keystreams"
    return PlanerPaths(
        root=root,
        secrets_dir=secrets_dir,
        config_file=secrets_dir / "planer.json",
        channels_file=secrets_dir / "channels.json",
        client_secret_file=secrets_dir / "client_secret.json",
        bcast_dir=root / "bcast",
        keystreams_dir=keystreams_dir,
        keys_file=keystreams_dir / "keys.txt",
        logs_dir=root / "logs",
    )


def ensure_dirs(paths: PlanerPaths) -> None:
    """Только папки: конфиги в secrets\\ планер не создаёт и не пишет никогда."""
    for directory in paths.directories:
        directory.mkdir(parents=True, exist_ok=True)
