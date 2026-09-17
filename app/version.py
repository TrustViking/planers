"""Номер версии планера — единственный источник: CLI (--version), лог, отчёт, spec и инсталлятор (ТЗ §7.6, §9).

Сборка (build_release.bat, через него и build_local.bat) перед PyInstaller зовёт `python -m app.version --bump`:
patch +1 прямо в этом файле, новый номер — в stdout. Руками номер меняется только в major/minor.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

APP_VERSION: Final[str] = "0.1.5"

BUMP_FLAG: Final[str] = "--bump"
VERSION_FORMAT: Final[re.Pattern[str]] = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
VERSION_LINE: Final[re.Pattern[str]] = re.compile(r'^APP_VERSION: Final\[str\] = "([^"]*)"$', re.MULTILINE)


def next_patch_version(version: str) -> str:
    """0.1.9 → 0.1.10; номер не вида MAJOR.MINOR.PATCH — ValueError."""
    match: re.Match[str] | None = VERSION_FORMAT.fullmatch(version)
    if match is None:
        raise ValueError(f"version is not MAJOR.MINOR.PATCH: {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def bump_version_file(path: Path) -> str:
    """Переписывает строку APP_VERSION в файле версии; остальное — байт в байт. Возвращает новый номер."""
    text: str = path.read_bytes().decode("utf-8")
    matches: list[re.Match[str]] = list(VERSION_LINE.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one APP_VERSION line in {path}, found {len(matches)}")
    match: re.Match[str] = matches[0]
    version: str = next_patch_version(match.group(1))
    start, end = match.span(1)
    path.write_bytes((text[:start] + version + text[end:]).encode("utf-8"))
    return version


if __name__ == "__main__":
    if sys.argv[1:] != [BUMP_FLAG]:
        sys.exit(f"usage: python -m app.version {BUMP_FLAG}")
    print(bump_version_file(Path(__file__)))
