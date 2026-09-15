"""Корень планера и его папки (ТЗ §9): рядом с exe (frozen) или корень репо (dev).

Владелец видит в корне только своё: config\\ (что заполняет), secrets\\ (ключи доступа),
bcast\\ (пакеты), keystreams\\ (ключи потоков), logs\\ (логи). Технические данные —
в app\\state\\: в dev это папка пакета app/state, рядом с exe — папка app\\state\\ у exe.
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
    config_dir: Path
    config_file: Path            # config\planer.json — настройки планера, поставляются со сборкой (ТЗ §5.2)
    channels_file: Path          # config\channels.json — каналы владельца (ТЗ §5.2)
    secrets_dir: Path
    client_secret_file: Path     # secrets\client_secret.json — паспорт программы (ТЗ §5.3)
    bcast_dir: Path              # bcast\ — пакеты броадкастера (ТЗ §7.1)
    state_dir: Path              # app\state\ — только runtime-данные планера
    bindings_file: Path          # app\state\bindings.json — привязки каналов (ТЗ §5.3)
    keystreams_dir: Path
    keys_file: Path
    logs_dir: Path

    @property
    def directories(self) -> tuple[Path, ...]:
        return (
            self.config_dir,
            self.secrets_dir,
            self.bcast_dir,
            self.state_dir,
            self.keystreams_dir,
            self.logs_dir,
        )


def build_paths(root: Path) -> PlanerPaths:
    config_dir: Path = root / "config"
    secrets_dir: Path = root / "secrets"
    state_dir: Path = root / "app" / "state"
    keystreams_dir: Path = root / "keystreams"
    return PlanerPaths(
        root=root,
        config_dir=config_dir,
        config_file=config_dir / "planer.json",
        channels_file=config_dir / "channels.json",
        secrets_dir=secrets_dir,
        client_secret_file=secrets_dir / "client_secret.json",
        bcast_dir=root / "bcast",
        state_dir=state_dir,
        bindings_file=state_dir / "bindings.json",
        keystreams_dir=keystreams_dir,
        keys_file=keystreams_dir / "keys.txt",
        logs_dir=root / "logs",
    )


def ensure_dirs(paths: PlanerPaths) -> None:
    """Только папки: файлы в config\\ планер не создаёт никогда."""
    for directory in paths.directories:
        directory.mkdir(parents=True, exist_ok=True)
