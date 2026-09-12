from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app import main as main_module
from app.google.auth import AuthError, AuthErrorReason
from app.main import run_cli
from app.paths import ROOT_ENV_VAR
from app.platforms.base import ChannelInfo, PlatformError
from app.platforms.fake import FakePlatform
from app.tests.conftest import FakeFormSender
from app.ui import messages_ru as msg

CONFIG_YAML: str = """owner: "Тест"
"""
CHANNELS_YAML: str = """channels:
  - id: yt_ua
    platform: youtube
    account_name: "Test UA"
    languages: [uk]
  - id: yt_ru
    platform: youtube
    account_name: "Test RU"
    languages: [ru, en]
"""
CHANNEL_KEYS: tuple[str, ...] = ("yt_ua", "yt_ru")
PackageFactory = Callable[..., Path]
SlotFactory = Callable[..., dict[str, Any]]


@pytest.fixture
def fake_platform_in_main(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    """Площадка и отправитель формы в main подменяются фейками: сеть в тестах запрещена."""
    platform: FakePlatform = FakePlatform()
    monkeypatch.setattr(main_module, "build_platform", lambda paths: platform)
    monkeypatch.setattr(main_module, "build_form_sender", lambda paths, now_utc: FakeFormSender())
    return platform


@pytest.fixture
def planer_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_config_example: Path,
    repo_channels_example: Path,
) -> Path:
    root: Path = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(repo_config_example, root / "config" / "planer.example.yaml")
    shutil.copyfile(repo_channels_example, root / "config" / "channels.example.yaml")
    (root / "secrets").mkdir(parents=True)
    (root / "secrets" / "client_secret.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(ROOT_ENV_VAR, str(root))
    return root


def _write_config(root: Path) -> None:
    (root / "config" / "planer.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (root / "config" / "channels.yaml").write_text(CHANNELS_YAML, encoding="utf-8")


def _write_tokens(root: Path, *channel_keys: str) -> None:
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    for key in channel_keys or CHANNEL_KEYS:
        (root / "secrets" / f"{key}.token.json").write_text("{}", encoding="utf-8")


def _write_bindings(root: Path, bindings: dict[str, str] | None = None) -> None:
    """По умолчанию — привязки, совпадающие с ответом FakePlatform."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "channels": {
            key: {
                "youtube_channel_id": youtube_id,
                "title": f"Fake {key}",
                "authorized_at": "16-03-2027 12:00",
            }
            for key, youtube_id in (
                bindings or {key: FakePlatform.default_channel_info(key).youtube_channel_id for key in CHANNEL_KEYS}
            ).items()
        },
    }
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / "channels.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _ready(root: Path) -> None:
    _write_config(root)
    _write_tokens(root)
    _write_bindings(root)


def test_run_without_flags_is_the_full_cycle(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Двойной клик по planer.bat: полный цикл §4 — эфиры создаются."""
    _ready(planer_root)
    make_package(planer_root / "promo", slots=[make_slot("01-01-2099", "19:00", "uk")])
    assert run_cli([]) == 0
    out: str = capsys.readouterr().out
    assert msg.FIRST_RUN_HEADER in out
    assert "эфир создан, ключ получен" in out
    assert len(fake_platform_in_main.created) == 1
    assert (planer_root / "state" / "registry.json").exists()
    assert "fake-0001" in (planer_root / "keystreams" / "keys.txt").read_text(encoding="utf-8")


def test_run_without_flags_on_empty_promo_exits_3(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    assert run_cli([]) == 3
    assert "нет пакетов" in capsys.readouterr().out


def test_missing_configs_copy_examples_and_exit_2(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(["--dry-run"]) == 2
    config_dir: Path = planer_root / "config"
    assert (config_dir / "planer.yaml").read_bytes() == (config_dir / "planer.example.yaml").read_bytes()
    assert (config_dir / "channels.yaml").read_bytes() == (config_dir / "channels.example.yaml").read_bytes()
    assert "Создан" in capsys.readouterr().out


def test_bad_config_exits_2(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(planer_root)
    (planer_root / "config" / "channels.yaml").write_text("channels: []\n", encoding="utf-8")
    assert run_cli(["--dry-run"]) == 2
    assert "Ошибка в конфиге" in capsys.readouterr().out


def test_missing_client_secret_exits_2(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(planer_root)
    (planer_root / "secrets" / "client_secret.json").unlink()
    assert run_cli(["--dry-run"]) == 2
    assert "client_secret.json" in capsys.readouterr().out


def test_missing_token_triggers_authorization(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ТЗ §5.3 п.1-2: токена нет — браузер открывается сам, запуск продолжается."""
    _write_config(planer_root)
    monkeypatch.setattr(
        main_module,
        "load_credentials",
        lambda client_secret, token_file, force_reauth=False: token_file.write_text("{}", encoding="utf-8"),
    )
    assert run_cli(["--dry-run"]) == 3          # авторизовались, а пакетов в promo нет
    out: str = capsys.readouterr().out
    assert "Google hasn't verified this app" in out
    assert fake_platform_in_main.describe_calls != []
    assert (planer_root / "state" / "channels.json").exists()


def test_binding_mismatch_stops_dry_run(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root)
    _write_bindings(planer_root, {"yt_ua": "UCsomeoneElse", "yt_ru": "UCfakeyt_ru"})
    assert run_cli(["--dry-run"]) == 2
    out: str = capsys.readouterr().out
    assert "UCsomeoneElse" in out
    assert msg.BINDINGS_NOT_VERIFIED in out


def test_empty_promo_exits_3(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    assert run_cli(["--dry-run"]) == 3
    assert "нет пакетов" in capsys.readouterr().out


def test_dry_run_on_valid_package(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    path: Path = make_package(
        planer_root / "promo",
        slots=[make_slot("01-01-2099", "19:00", "uk"), make_slot("01-01-2099", "19:00", "en")],
    )
    assert run_cli(["--dry-run"]) == 0
    captured = capsys.readouterr()
    assert f"{path.name} — принят, слотов 2, из них под мои языки 2" in captured.out
    assert "- 01-01-2099 19:00 en → Test RU — эфира нет, будет создан — не выполнено (dry-run)" in captured.out
    assert "- 01-01-2099 19:00 uk → Test UA — эфира нет, будет создан — не выполнено (dry-run)" in captured.out
    assert "run_started" not in captured.err
    [report] = list((planer_root / "logs").glob("*_report.md"))
    assert "## Пакеты" in report.read_text(encoding="utf-8")
    assert path.exists()
    assert not (planer_root / "state" / "registry.json").exists()
    assert not (planer_root / "keystreams" / "keys.txt").exists()


def test_damaged_package_next_to_valid_exits_1(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    make_package(planer_root / "promo", slots=[make_slot("01-01-2099", "19:00", "uk")])
    (planer_root / "promo" / "broken.bcast").write_bytes(b"not a zip")
    assert run_cli(["--dry-run"]) == 1
    assert "broken.bcast — пакет повреждён: не ZIP-архив" in capsys.readouterr().out


def test_status_writes_keys_file(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    assert run_cli(["--status"]) == 0
    keys_file: Path = planer_root / "keystreams" / "keys.txt"
    lines: list[str] = keys_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(line.startswith("# ") for line in lines)
    out: str = capsys.readouterr().out
    assert "## Запланировано на каналах (0)" in out
    assert "Файл ключей: keystreams" in out


def test_unreadable_registry_exits_2(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    (planer_root / "state" / "registry.json").write_text("{broken", encoding="utf-8")
    assert run_cli(["--status"]) == 2
    assert "не читается" in capsys.readouterr().out


def test_check_reports_every_channel(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    fake_platform_in_main.channel_info["yt_ua"] = ChannelInfo(
        youtube_channel_id="UCfakeyt_ua",
        title="Тестовый канал",
        default_language="uk",
    )
    assert run_cli(["--check"]) == 0
    out: str = capsys.readouterr().out
    assert "- yt_ua: Тестовый канал (id UCfakeyt_ua), язык канала на YouTube: uk" in out
    assert msg.CHECK_CHANNEL_LANGUAGE_UNSET in out       # у yt_ru язык не задан
    assert "языки стримов из channels.yaml: ru, en" in out
    assert "запланированных эфиров: 0" in out
    assert msg.CHECK_ALL_OK in out


def test_check_reports_disabled_streaming_and_exits_1(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    fake_platform_in_main.fail_list["yt_ru"] = PlatformError("liveStreamingNotEnabled", "трансляции не включены")
    assert run_cli(["--check"]) == 1
    out: str = capsys.readouterr().out
    assert "- yt_ru: liveStreamingNotEnabled (трансляции не включены)" in out
    assert msg.CHECK_HAS_PROBLEMS in out


def test_check_authorizes_channel_without_token(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root, "yt_ua")
    _write_bindings(planer_root, {"yt_ua": "UCfakeyt_ua"})
    monkeypatch.setattr(
        main_module,
        "load_credentials",
        lambda client_secret, token_file, force_reauth=False: token_file.write_text("{}", encoding="utf-8"),
    )
    assert run_cli(["--check"]) == 0
    out: str = capsys.readouterr().out
    assert msg.CHECK_AUTHORIZING.format(key="yt_ru") in out
    assert msg.CHECK_ALL_OK in out


def test_auth_all_writes_bindings(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    calls: list[Path] = []
    monkeypatch.setattr(
        main_module,
        "load_credentials",
        lambda client_secret, token_file, force_reauth=False: calls.append(token_file),
    )
    assert run_cli(["--auth", "all"]) == 0
    out: str = capsys.readouterr().out
    assert "Google hasn't verified this app" in out
    assert [path.name for path in calls] == ["yt_ua.token.json", "yt_ru.token.json"]
    payload: dict[str, Any] = json.loads((planer_root / "state" / "channels.json").read_text(encoding="utf-8"))
    assert sorted(payload["channels"]) == ["yt_ru", "yt_ua"]
    assert payload["channels"]["yt_ua"]["youtube_channel_id"] == "UCfakeyt_ua"


def test_auth_refuses_to_rebind_another_channel(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_bindings(planer_root, {"yt_ua": "UCtheRightOne"})
    monkeypatch.setattr(
        main_module,
        "load_credentials",
        lambda client_secret, token_file, force_reauth=False: None,
    )
    assert run_cli(["--auth", "yt_ua"]) == 1
    out: str = capsys.readouterr().out
    assert "UCtheRightOne" in out and "UCfakeyt_ua" in out
    payload: dict[str, Any] = json.loads((planer_root / "state" / "channels.json").read_text(encoding="utf-8"))
    assert payload["channels"]["yt_ua"]["youtube_channel_id"] == "UCtheRightOne"


def test_auth_unknown_channel_key_exits_1(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    assert run_cli(["--auth", "yt_unknown"]) == 1
    assert "yt_ua, yt_ru" in capsys.readouterr().out


def test_auth_failure_names_the_reason_and_scope_hint(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)

    def _fail(client_secret: Path, token_file: Path, force_reauth: bool = False) -> None:
        raise AuthError(AuthErrorReason.FLOW_FAILED, "browser closed")

    monkeypatch.setattr(main_module, "load_credentials", _fail)
    assert run_cli(["--auth", "yt_ua"]) == 1
    out: str = capsys.readouterr().out
    assert msg.AUTH_REASON_TEXT["flow_failed"] in out
    assert "скоуп youtube не добавлен" in out
