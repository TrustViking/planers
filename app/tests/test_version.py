from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import version as version_module
from app.version import APP_VERSION, VERSION_FORMAT, bump_version_file, next_patch_version


def test_app_version_is_major_minor_patch() -> None:
    assert VERSION_FORMAT.fullmatch(APP_VERSION)


@pytest.mark.parametrize(
    ("current", "expected"),
    [("0.1.0", "0.1.1"), ("0.1.9", "0.1.10"), ("1.2.99", "1.2.100")],
)
def test_next_patch_version_adds_one_to_patch(current: str, expected: str) -> None:
    assert next_patch_version(current) == expected


@pytest.mark.parametrize("bad", ["0.1", "v0.1.0", "0.1.0-rc1", ""])
def test_next_patch_version_refuses_other_shapes(bad: str) -> None:
    with pytest.raises(ValueError):
        next_patch_version(bad)


def _copy_version_file(tmp_path: Path) -> Path:
    target: Path = tmp_path / "version.py"
    shutil.copyfile(Path(version_module.__file__), target)
    return target


def test_bump_rewrites_only_the_version_line(tmp_path: Path) -> None:
    """Сборка поднимает patch в самом файле; остальное содержимое — байт в байт."""
    target: Path = _copy_version_file(tmp_path)
    before: bytes = target.read_bytes()
    new_version: str = bump_version_file(target)
    assert new_version == next_patch_version(APP_VERSION)
    assert target.read_bytes() == before.replace(
        f'"{APP_VERSION}"'.encode("utf-8"), f'"{new_version}"'.encode("utf-8"), 1
    )
    assert bump_version_file(target) == next_patch_version(new_version)


def test_bump_without_version_line_is_an_error(tmp_path: Path) -> None:
    target: Path = tmp_path / "version.py"
    target.write_text('VERSION = "0.1.0"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        bump_version_file(target)
    assert target.read_text(encoding="utf-8") == 'VERSION = "0.1.0"\n'


def test_bump_command_prints_the_new_version(tmp_path: Path) -> None:
    """Так его зовёт build_release.bat; запускаем копию, чтобы не трогать version.py репо."""
    target: Path = _copy_version_file(tmp_path)
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, str(target), "--bump"], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == next_patch_version(APP_VERSION)
    assert f'APP_VERSION: Final[str] = "{result.stdout.strip()}"' in target.read_text(encoding="utf-8")


def test_command_without_bump_flag_changes_nothing(tmp_path: Path) -> None:
    target: Path = _copy_version_file(tmp_path)
    before: bytes = target.read_bytes()
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, str(target)], capture_output=True, text=True
    )
    assert result.returncode != 0 and target.read_bytes() == before
