from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app import main as main_module
from app.google.auth import AuthError, AuthErrorReason
from app.main import ChannelConsole, run_cli
from app.paths import ROOT_ENV_VAR, PlanerPaths
from app.platforms import youtube as youtube_module
from app.platforms.base import BroadcastPlatform, ChannelInfo, PlatformError, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.tests.conftest import FakeFormSender
from app.ui import messages_ru as msg
from app.version import APP_VERSION

UA: str = "Канал UA"
RU: str = "Канал RU"
UA_GOOGLE: str = "ua@gmail.com"
RU_GOOGLE: str = "ru@gmail.com"
CHANNEL_NAMES: tuple[str, ...] = (UA, RU)
CHANNELS_JSON: dict[str, Any] = {
    "channels": [
        {"platform": "youtube", "account_name": UA, "google_account": UA_GOOGLE, "languages": ["uk"], "privacy": "public"},
        {"platform": "youtube", "account_name": RU, "google_account": RU_GOOGLE, "languages": ["ru", "en"], "privacy": "unlisted"},
    ]
}
PackageFactory = Callable[..., Path]
SlotFactory = Callable[..., dict[str, Any]]
CredentialsLoader = Callable[..., object]


@pytest.fixture
def fake_platform_in_main(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    """Площадка и отправитель формы в main подменяются фейками: сеть в тестах запрещена.

    Обёртка с проверкой привязки (VerifiedPlatform) и печать входа остаются боевыми.
    """
    platform: FakePlatform = FakePlatform()

    def _build(paths: PlanerPaths, console: ChannelConsole) -> BroadcastPlatform:
        platform.on_login = console.on_login
        return platform

    monkeypatch.setattr(main_module, "build_platform", _build)
    monkeypatch.setattr(main_module, "build_form_sender", lambda paths, now_utc: FakeFormSender())
    return platform


@pytest.fixture
def planer_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_planer_config: Path) -> Path:
    """Корень как после установки: planer.json поставлен, channels.json владелец ещё не создал."""
    root: Path = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(repo_planer_config, root / "config" / "planer.json")
    (root / "secrets").mkdir(parents=True)
    (root / "secrets" / "client_secret.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(ROOT_ENV_VAR, str(root))
    return root


def _write_config(root: Path) -> None:
    (root / "config" / "channels.json").write_text(json.dumps(CHANNELS_JSON, ensure_ascii=False), encoding="utf-8")


def _write_tokens(root: Path, *account_names: str) -> None:
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    for name in account_names or CHANNEL_NAMES:
        (root / "secrets" / f"{name}.token.json").write_text("{}", encoding="utf-8")


def _bindings_file(root: Path) -> Path:
    return root / "app" / "state" / "bindings.json"


def _write_bindings(root: Path, bindings: dict[str, str] | None = None) -> None:
    """По умолчанию — привязки, совпадающие с ответом FakePlatform."""
    known: dict[str, str] = bindings or {
        name: FakePlatform.default_channel_info(name).youtube_channel_id for name in CHANNEL_NAMES
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "channels": {
            name: {"youtube_channel_id": youtube_id, "title": f"Fake {name}", "authorized_at": "16-03-2027 12:00"}
            for name, youtube_id in known.items()
        },
    }
    _bindings_file(root).parent.mkdir(parents=True, exist_ok=True)
    _bindings_file(root).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_bindings(root: Path) -> dict[str, Any]:
    return json.loads(_bindings_file(root).read_text(encoding="utf-8"))["channels"]


def _ready(root: Path) -> None:
    _write_config(root)
    _write_tokens(root)
    _write_bindings(root)


def _fake_login(calls: list[Path] | None = None) -> CredentialsLoader:
    """load_credentials без браузера: зовёт on_login ровно там, где его позвал бы боевой код."""

    def _load(
        client_secret: Path,
        token_file: Path,
        login_hint: str,
        force_reauth: bool = False,
        on_login: Any = None,
    ) -> None:
        if on_login is not None:
            on_login()
        if calls is not None:
            calls.append(token_file)

    return _load


def test_run_without_flags_is_the_full_cycle(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Двойной клик по planer.bat: полный цикл §4 — эфиры создаются."""
    _ready(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    assert run_cli([]) == 0
    out: str = capsys.readouterr().out
    lines: list[str] = out.splitlines()
    assert lines[0].startswith(f"Планер {APP_VERSION} — ")
    assert lines[1] == "Итог: опубликовано 1, исправлено 0, уже стояло 0, не публиковали 0, ошибок 0"
    assert f"  {UA} ({UA_GOOGLE})" in lines
    assert "    01-01-2099  19:00  uk  ****-0000  передан в форму" in lines
    assert "## " not in out                                            # markdown — только в отчёте
    assert re.search(r"^  лог +logs\\\d{2}-\d{2}-\d{4}_\d{6}_planer\.log$", out, re.MULTILINE)
    assert len(fake_platform_in_main.created) == 1
    assert not (planer_root / "state").exists()                       # технические данные — только в app\state
    keys_text: str = (planer_root / "keystreams" / "keys.txt").read_text(encoding="utf-8")
    assert "fake-0001" in keys_text and "форма  отправлен в форму " in keys_text


def test_run_without_flags_on_empty_bcast_exits_3_without_touching_channels(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    fake_platform_in_main.tokens_missing = set(CHANNEL_NAMES)
    assert run_cli([]) == 3
    out: str = capsys.readouterr().out
    assert "нет пакетов" in out and "bcast" in out
    assert fake_platform_in_main.describe_calls == [] and fake_platform_in_main.logins == []


def test_version_flag_prints_the_single_version_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        run_cli(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"Планер {APP_VERSION}"


def test_run_started_log_line_carries_the_version(planer_root: Path) -> None:
    assert run_cli(["--dry-run"]) == 2                     # channels.json ещё нет — сеть не нужна
    [log_file] = list((planer_root / "logs").glob("*_planer.log"))
    [line] = [line for line in log_file.read_text(encoding="utf-8").splitlines() if "| run_started " in line]
    assert f"run_started version={APP_VERSION} " in line


def test_missing_channels_json_prints_template_and_creates_nothing(
    planer_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(["--dry-run"]) == 2
    out: str = capsys.readouterr().out
    channels_file: Path = planer_root / "config" / "channels.json"
    assert msg.CONFIG_CHANNELS_HINT.format(path=channels_file) in out
    assert msg.CONFIG_CHANNELS_TEMPLATE in out
    assert sorted(path.name for path in (planer_root / "config").iterdir()) == ["planer.json"]


def test_missing_planer_json_prints_its_template(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(planer_root)
    (planer_root / "config" / "planer.json").unlink()
    assert run_cli(["--dry-run"]) == 2
    out: str = capsys.readouterr().out
    assert msg.CONFIG_PLANER_HINT.format(path=planer_root / "config" / "planer.json") in out
    assert msg.CONFIG_PLANER_TEMPLATE in out
    assert sorted(path.name for path in (planer_root / "config").iterdir()) == ["channels.json"]


def test_missing_field_prints_template(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config: dict[str, Any] = json.loads(json.dumps(CHANNELS_JSON))
    config["channels"][1].pop("privacy")
    (planer_root / "config" / "channels.json").write_text(json.dumps(config), encoding="utf-8")
    assert run_cli(["--dry-run"]) == 2
    out: str = capsys.readouterr().out
    assert "channels[1].privacy — обязательное поле отсутствует" in out
    assert msg.CONFIG_CHANNELS_TEMPLATE in out


def test_bad_config_exits_2_without_template(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (planer_root / "config" / "channels.json").write_text('{"channels": []}', encoding="utf-8")
    assert run_cli(["--dry-run"]) == 2
    out: str = capsys.readouterr().out
    assert "Ошибка в конфиге" in out
    assert msg.CONFIG_CHANNELS_TEMPLATE not in out


def test_missing_client_secret_exits_2(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(planer_root)
    (planer_root / "secrets" / "client_secret.json").unlink()
    assert run_cli(["--dry-run"]) == 2
    assert "client_secret.json" in capsys.readouterr().out


def test_missing_token_logs_in_at_first_access_and_run_continues(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ТЗ §5.3: вход — когда до канала дошло дело; канал без объектов не трогается."""
    _write_config(planer_root)
    fake_platform_in_main.tokens_missing = set(CHANNEL_NAMES)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    assert run_cli(["--dry-run"]) == 0
    out: str = capsys.readouterr().out
    # порядок: что происходит, какой аккаунт, какой канал, и только потом длинное предупреждение
    login_lines: list[str] = [
        msg.AUTH_STARTING.format(account_name=UA),
        msg.AUTH_CHOOSE_ACCOUNT.format(google_account=UA_GOOGLE, account_name=UA),
        msg.AUTH_CHOOSE_RIGHT_CHANNEL.format(account_name=UA),
        msg.AUTH_UNVERIFIED_APP_WARNING,
    ]
    positions: list[int] = [out.index(line) for line in login_lines]
    assert positions == sorted(positions)
    assert msg.AUTH_OK.format(account_name=UA, title=f"Fake {UA}", youtube_channel_id=f"UCfake{UA}") in out
    assert out.index("Google hasn't verified this app") < out.index(f"Планер {APP_VERSION} — ")
    assert fake_platform_in_main.logins == [UA]
    assert list(_read_bindings(planer_root)) == [UA]


def test_binding_mismatch_fails_only_that_channel(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root)
    _write_bindings(planer_root, {UA: "UCsomeoneElse", RU: f"UCfake{RU}"})
    make_package(
        planer_root / "bcast",
        slots=[make_slot("01-01-2099", "19:00", "uk"), make_slot("01-01-2099", "19:00", "ru")],
    )
    assert run_cli([]) == 1
    out: str = capsys.readouterr().out
    assert "UCsomeoneElse" in out
    assert f"  {RU} ({RU_GOOGLE})" in out.splitlines()
    assert "  ошибка: 01-01-2099 19:00 uk -> Канал UA — YouTube: channelBindingMismatch (" in out
    assert [call.channel_id for call in fake_platform_in_main.created] == [RU]
    assert _read_bindings(planer_root)[UA]["youtube_channel_id"] == "UCsomeoneElse"


def test_dry_run_on_valid_package(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    path: Path = make_package(
        planer_root / "bcast",
        slots=[make_slot("01-01-2099", "19:00", "uk"), make_slot("01-01-2099", "19:00", "en")],
    )
    assert run_cli(["--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "dry-run" in captured.out.splitlines()[0]
    assert "Итог: опубликуем 2, исправим 0, уже стояло 0, не публиковали 0, ошибок 0" in captured.out
    assert "ОПУБЛИКУЕМ (2)" in captured.out and "КЛЮЧИ СТРИМЕРУ" not in captured.out
    # каналы — в порядке channels.json: сначала UA, потом RU
    assert captured.out.index(f"  {UA} ({UA_GOOGLE})") < captured.out.index(f"  {RU} ({RU_GOOGLE})")
    assert "будет создан" not in captured.out                          # подробности — в отчёте
    assert "run_started" not in captured.err
    [report] = list((planer_root / "logs").glob("*_report.md"))
    report_text: str = report.read_text(encoding="utf-8")
    assert f"{path.name} — принят, слотов 2, из них под мои языки 2" in report_text
    assert f"- 01-01-2099 19:00 en -> {RU} — эфира нет, будет создан — не выполнено (dry-run)" in report_text
    assert f"- 01-01-2099 19:00 uk -> {UA} — эфира нет, будет создан — не выполнено (dry-run)" in report_text
    assert f"отчёт   logs\\{report.name}" in captured.out
    assert path.exists()
    assert not (planer_root / "keystreams" / "keys.txt").exists()


def test_damaged_package_next_to_valid_exits_1(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    (planer_root / "bcast" / "broken.bcast").write_bytes(b"not a zip")
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
    assert len(lines) == 3 and all(line.startswith("# ") for line in lines)
    out: str = capsys.readouterr().out
    assert out.splitlines()[1] == "Итог: уже стояло 0, ошибок 0"
    assert "=====" not in out                                         # пустые блоки не печатаются
    assert re.search(r"^  ключи +keystreams", out, re.MULTILINE)


def test_undated_broadcast_goes_to_attention_not_to_the_log_console(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Эфир без времени старта: строка лога — только в planer.log, владельцу — строкой во «Внимание»."""
    _ready(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    listed_by_fake = fake_platform_in_main.list_upcoming

    def _list_with_undated(channel: Any) -> list[UpcomingBroadcast]:
        undated: dict[str, Any] = {"id": "NOSTART", "snippet": {"title": "Брифинг без времени"}}
        assert youtube_module._broadcast_from_item(undated, channel.account_name) is None   # боевой разбор
        return listed_by_fake(channel)

    fake_platform_in_main.list_upcoming = _list_with_undated  # type: ignore[method-assign]
    assert run_cli(["--dry-run"]) == 0
    captured = capsys.readouterr()
    lines: list[str] = captured.out.splitlines()
    attention: int = next(index for index, line in enumerate(lines) if msg.CONSOLE_BLOCK_ATTENTION in line)
    assert lines[attention + 1] == (
        f"  эфир без времени старта: {UA} — «Брифинг без времени»; у эфира нет запланированного времени, "
        "планер его не видит"
    )
    assert "broadcast_without_start" not in captured.err and "broadcast_without_start" not in captured.out
    [log_file] = list((planer_root / "logs").glob("*_planer.log"))
    assert "broadcast_without_start" in log_file.read_text(encoding="utf-8")


def test_check_reports_every_channel(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    fake_platform_in_main.channel_info[UA] = ChannelInfo(
        youtube_channel_id=f"UCfake{UA}",
        title="Тестовый канал",
        default_language="uk",
    )
    assert run_cli(["--check"]) == 0
    out: str = capsys.readouterr().out
    assert f"- {UA}: Тестовый канал (id UCfake{UA}), язык канала на YouTube: uk" in out
    assert msg.CHECK_CHANNEL_LANGUAGE_UNSET in out       # у второго канала язык не задан
    assert "языки стримов из channels.json: ru, en" in out
    assert "запланированных эфиров: 0" in out
    assert msg.CHECK_ALL_OK in out


def test_check_reports_disabled_streaming_and_exits_1(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    fake_platform_in_main.fail_list[RU] = PlatformError("liveStreamingNotEnabled", "трансляции не включены")
    assert run_cli(["--check"]) == 1
    out: str = capsys.readouterr().out
    assert f"- {RU}: liveStreamingNotEnabled (трансляции не включены)" in out
    assert msg.CHECK_HAS_PROBLEMS in out


def test_check_logs_in_channel_without_token_and_binds_it(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root, UA)
    _write_bindings(planer_root, {UA: f"UCfake{UA}"})
    fake_platform_in_main.tokens_missing = {RU}
    assert run_cli(["--check"]) == 0
    out: str = capsys.readouterr().out
    assert msg.AUTH_STARTING.format(account_name=RU) in out
    assert msg.AUTH_STARTING.format(account_name=UA) not in out
    assert msg.CHECK_ALL_OK in out
    assert sorted(_read_bindings(planer_root)) == sorted(CHANNEL_NAMES)


def test_check_refuses_youtube_channel_bound_under_another_name(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root)
    _write_bindings(planer_root, {"Старое имя": f"UCfake{UA}", RU: f"UCfake{RU}"})
    assert run_cli(["--check"]) == 1
    out: str = capsys.readouterr().out
    assert "«Старое имя»" in out
    assert UA not in _read_bindings(planer_root)


def test_auth_all_writes_bindings_under_account_names(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    calls: list[Path] = []
    monkeypatch.setattr(main_module, "load_credentials", _fake_login(calls))
    assert run_cli(["--auth", "all"]) == 0
    out: str = capsys.readouterr().out
    assert "Google hasn't verified this app" in out
    assert msg.AUTH_OK.format(account_name=RU, title=f"Fake {RU}", youtube_channel_id=f"UCfake{RU}") in out
    assert [path.name for path in calls] == [f"{UA}.token.json", f"{RU}.token.json"]
    bindings: dict[str, Any] = _read_bindings(planer_root)
    assert sorted(bindings) == sorted(CHANNEL_NAMES)
    assert bindings[UA]["youtube_channel_id"] == f"UCfake{UA}"


def test_auth_refuses_to_rebind_another_channel(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_bindings(planer_root, {UA: "UCtheRightOne"})
    monkeypatch.setattr(main_module, "load_credentials", _fake_login())
    assert run_cli(["--auth", UA]) == 1
    out: str = capsys.readouterr().out
    assert "UCtheRightOne" in out and f"UCfake{UA}" in out
    assert _read_bindings(planer_root)[UA]["youtube_channel_id"] == "UCtheRightOne"


def test_auth_unknown_channel_lists_account_names(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    assert run_cli(["--auth", "yt_unknown"]) == 1
    assert f"«{UA}», «{RU}»" in capsys.readouterr().out


def test_auth_failure_names_the_reason_and_scope_hint(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)

    def _fail(
        client_secret: Path,
        token_file: Path,
        login_hint: str,
        force_reauth: bool = False,
        on_login: Any = None,
    ) -> None:
        raise AuthError(AuthErrorReason.FLOW_FAILED, "browser closed")

    monkeypatch.setattr(main_module, "load_credentials", _fail)
    assert run_cli(["--auth", UA]) == 1
    out: str = capsys.readouterr().out
    assert msg.AUTH_REASON_TEXT["flow_failed"] in out
    assert "скоуп youtube не добавлен" in out
