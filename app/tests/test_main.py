from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.main import run_cli
from app.paths import ROOT_ENV_VAR

CONFIG_YAML: str = """owner: "Тест"
channels:
  - id: yt_ua
    platform: youtube
    account_name: "Test UA"
    languages: [uk]
  - id: yt_ru
    platform: youtube
    account_name: "Test RU"
    languages: [ru, en]
"""
PackageFactory = Callable[..., Path]
SlotFactory = Callable[..., dict[str, Any]]


@pytest.fixture
def planer_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_config_example: Path) -> Path:
    root: Path = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(repo_config_example, root / "config" / "planer.example.yaml")
    monkeypatch.setenv(ROOT_ENV_VAR, str(root))
    return root


def _write_config(root: Path) -> None:
    (root / "config" / "planer.yaml").write_text(CONFIG_YAML, encoding="utf-8")


def test_missing_config_copies_example_and_exits_2(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli([]) == 2
    config_dir: Path = planer_root / "config"
    assert (config_dir / "planer.yaml").read_bytes() == (config_dir / "planer.example.yaml").read_bytes()
    assert "Создан" in capsys.readouterr().out


def test_bad_config_exits_2(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (planer_root / "config" / "planer.yaml").write_text("owner: \"\"\nchannels: []\n", encoding="utf-8")
    assert run_cli([]) == 2
    assert "Ошибка в конфиге" in capsys.readouterr().out


def test_empty_inbox_exits_3(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(planer_root)
    assert run_cli([]) == 3
    assert "нет пакетов" in capsys.readouterr().out


def test_valid_package_exits_0_and_writes_report(
    planer_root: Path,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    path: Path = make_package(
        planer_root / "inbox",
        slots=[make_slot("01-01-2099", "19:00", "uk"), make_slot("01-01-2099", "19:00", "en")],
    )
    assert run_cli(["--dry-run"]) == 0
    out: str = capsys.readouterr().out
    assert "## Пакеты" in out
    assert f"{path.name} — принят, слотов 2, из них под мои языки 2" in out
    assert "- 01-01-2099 19:00 en → Test RU (yt_ru)" in out
    assert "- 01-01-2099 19:00 uk → Test UA (yt_ua)" in out
    [report] = list((planer_root / "reports").glob("report_*.md"))
    assert "## Пакеты" in report.read_text(encoding="utf-8")
    assert path.exists()


def test_damaged_package_next_to_valid_exits_1(
    planer_root: Path,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    make_package(planer_root / "inbox", slots=[make_slot("01-01-2099", "19:00", "uk")])
    (planer_root / "inbox" / "broken.bcast").write_bytes(b"not a zip")
    assert run_cli(["--dry-run"]) == 1
    assert "broken.bcast — пакет повреждён: не ZIP-архив" in capsys.readouterr().out


def test_all_past_package_is_archived_only_without_dry_run(
    planer_root: Path,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    path: Path = make_package(planer_root / "inbox", slots=[make_slot("01-01-2020", "19:00", "uk")])
    assert run_cli(["--dry-run"]) == 0
    assert path.exists()
    assert "все слоты в прошлом (dry-run: не перенесён)" in capsys.readouterr().out
    assert run_cli([]) == 0
    assert not path.exists()
    assert (planer_root / "inbox" / "archive" / path.name).exists()
    assert "все слоты в прошлом, перенесён в inbox\\archive" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "mode"),
    [(["--status"], "--status"), (["--check"], "--check"), (["--auth", "yt_ua"], "--auth")],
)
def test_modes_of_later_stages_exit_2(
    planer_root: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    mode: str,
) -> None:
    _write_config(planer_root)
    assert run_cli(argv) == 2
    out: str = capsys.readouterr().out
    assert mode in out
    assert "пока не реализован" in out
