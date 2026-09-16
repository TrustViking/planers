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
from app.platforms.base import BroadcastPlatform, ChannelInfo, PlatformError
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

    Обёртка с проверкой названия канала (VerifiedPlatform) и печать входа остаются боевыми.
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
    (root / "secrets").mkdir(parents=True)
    shutil.copyfile(repo_planer_config, root / "secrets" / "planer.json")
    (root / "secrets" / "client_secret.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(ROOT_ENV_VAR, str(root))
    return root


def _write_config(root: Path) -> None:
    (root / "secrets" / "channels.json").write_text(json.dumps(CHANNELS_JSON, ensure_ascii=False), encoding="utf-8")


def _write_tokens(root: Path, *account_names: str) -> None:
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    for name in account_names or CHANNEL_NAMES:
        (root / "secrets" / f"{name}.token.json").write_text("{}", encoding="utf-8")


def _ready(root: Path) -> None:
    _write_config(root)
    _write_tokens(root)


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
    assert re.fullmatch(rf"Planer {re.escape(APP_VERSION)} — \d{{2}}-\d{{2}}-\d{{4}} \d{{2}}:\d{{2}}", lines[0])
    assert lines.count("Итог: опубликовано 1, исправлено 0, уже стояло 0, не публиковали 0, ошибок 0") == 1
    assert sum(1 for line in lines if line.startswith("Planer ")) == 1          # шапка одна на прогон
    assert f"  {UA} ({UA_GOOGLE})" in lines
    assert "    01-01-2099  19:00  uk  ****-0000  передан в форму" in lines
    assert "## " not in out                                            # markdown — только в отчёте
    assert re.search(r"^  лог +logs\\\d{2}-\d{2}-\d{4}_\d{6}_planer\.log$", out, re.MULTILINE)
    assert len(fake_platform_in_main.created) == 1
    assert sorted(path.name for path in planer_root.iterdir()) == ["bcast", "keystreams", "logs", "secrets"]
    keys_text: str = (planer_root / "keystreams" / "keys.txt").read_text(encoding="utf-8")
    assert "fake-0001" in keys_text and "форма  отправлен в форму " in keys_text


def test_full_run_prints_title_then_progress_then_blank_line_then_total(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Владелец не ждёт немого курсора: шапка сразу, строка на каждый долгий шаг, потом итог."""
    _ready(planer_root)
    make_package(
        planer_root / "bcast",
        slots=[make_slot("01-01-2099", "19:00", "uk"), make_slot("01-01-2099", "19:00", "ru")],
    )
    assert run_cli([]) == 0
    lines: list[str] = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f"Planer {APP_VERSION} — ") and "dry-run" not in lines[0]
    progress: list[str] = [
        "  " + msg.PROGRESS_PACKAGES_READ.format(packages=1, slots_total=2, slots_mine=2),
        # каналы — в порядке объектов (слоты по дате, времени и языку): ru раньше uk
        "  " + msg.PROGRESS_CHANNEL_READ_STARTED.format(account_name=RU),
        "  " + msg.PROGRESS_CHANNEL_READ_DONE.format(account_name=RU, count=0),
        "  " + msg.PROGRESS_CHANNEL_READ_STARTED.format(account_name=UA),
        "  " + msg.PROGRESS_CHANNEL_READ_DONE.format(account_name=UA, count=0),
        "  " + msg.PROGRESS_BROADCAST_CREATE.format(account_name=RU, date="01-01-2099", time="19:00", language="ru"),
        "  " + msg.PROGRESS_BROADCAST_CREATE.format(account_name=UA, date="01-01-2099", time="19:00", language="uk"),
        "  " + msg.PROGRESS_KEY_SEND.format(account_name=RU, date="01-01-2099", time="19:00", language="ru"),
        "  " + msg.PROGRESS_KEY_SEND.format(account_name=UA, date="01-01-2099", time="19:00", language="uk"),
        "  " + msg.PROGRESS_REPORT,
    ]
    assert lines[1 : 1 + len(progress)] == progress
    total: int = lines.index("Итог: опубликовано 2, исправлено 0, уже стояло 0, не публиковали 0, ошибок 0")
    assert total == len(progress) + 2 and lines[total - 1] == ""


def test_dry_run_prints_channel_and_package_progress_but_no_actions(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    assert run_cli(["--dry-run"]) == 0
    lines: list[str] = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f"Planer {APP_VERSION} — ") and lines[0].endswith(
        " — dry-run: ничего не создано и в форму не отправлено"
    )
    assert "  " + msg.PROGRESS_CHANNEL_READ_DONE.format(account_name=UA, count=0) in lines
    assert "  " + msg.PROGRESS_PACKAGES_READ.format(packages=1, slots_total=1, slots_mine=1) in lines
    assert not [line for line in lines if "создаю эфир" in line or "исправляю эфир" in line or "отправляю ключ" in line]
    assert lines.index("  " + msg.PROGRESS_REPORT) < lines.index(
        "Итог: опубликуем 1, исправим 0, уже стояло 0, не публиковали 0, ошибок 0"
    )


def test_title_is_printed_before_config_is_read(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--check без channels.json: шапка (обычная) — первой строкой, ещё до ошибки конфига."""
    assert run_cli(["--check"]) == 2
    lines: list[str] = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f"Planer {APP_VERSION} — ") and " — " not in lines[0].split(" — ", 1)[1]
    assert msg.CONFIG_CHANNELS_TEMPLATE.splitlines()[0] in lines[1:]


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
    assert capsys.readouterr().out.strip() == f"Planer {APP_VERSION}"


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
    channels_file: Path = planer_root / "secrets" / "channels.json"
    assert msg.CONFIG_CHANNELS_HINT.format(path=channels_file) in out
    assert msg.CONFIG_CHANNELS_TEMPLATE in out
    assert sorted(path.name for path in (planer_root / "secrets").iterdir()) == ["client_secret.json", "planer.json"]


def test_missing_planer_json_prints_its_template(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(planer_root)
    (planer_root / "secrets" / "planer.json").unlink()
    assert run_cli(["--dry-run"]) == 2
    out: str = capsys.readouterr().out
    assert msg.CONFIG_PLANER_HINT.format(path=planer_root / "secrets" / "planer.json") in out
    assert msg.CONFIG_PLANER_TEMPLATE in out
    assert sorted(path.name for path in (planer_root / "secrets").iterdir()) == ["channels.json", "client_secret.json"]


def test_missing_field_prints_template(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config: dict[str, Any] = json.loads(json.dumps(CHANNELS_JSON))
    config["channels"][1].pop("privacy")
    (planer_root / "secrets" / "channels.json").write_text(json.dumps(config), encoding="utf-8")
    assert run_cli(["--dry-run"]) == 2
    out: str = capsys.readouterr().out
    assert "channels[1].privacy — обязательное поле отсутствует" in out
    assert msg.CONFIG_CHANNELS_TEMPLATE in out


def test_bad_config_exits_2_without_template(planer_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (planer_root / "secrets" / "channels.json").write_text('{"channels": []}', encoding="utf-8")
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
    assert msg.AUTH_OK.format(account_name=UA, title=UA, youtube_channel_id=f"UCfake{UA}") in out
    # шапка — до входа, итог — после
    assert out.index(f"Planer {APP_VERSION} — ") < out.index(msg.AUTH_STARTING.format(account_name=UA))
    assert out.index("Google hasn't verified this app") < out.index("Итог: ")
    assert fake_platform_in_main.logins == [UA]
    assert fake_platform_in_main.describe_calls == [UA]


def test_channel_title_mismatch_fails_only_that_channel(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root)
    fake_platform_in_main.channel_info[UA] = ChannelInfo(
        youtube_channel_id="UCsomeoneElse", title="Чужой канал", default_language=None
    )
    make_package(
        planer_root / "bcast",
        slots=[make_slot("01-01-2099", "19:00", "uk"), make_slot("01-01-2099", "19:00", "ru")],
    )
    assert run_cli([]) == 1
    out: str = capsys.readouterr().out
    assert "«Чужой канал»" in out
    assert f"  {RU} ({RU_GOOGLE})" in out.splitlines()
    assert "  ошибка: 01-01-2099 19:00 uk -> Канал UA — YouTube: channelNameMismatch (" in out
    assert [call.channel_id for call in fake_platform_in_main.created] == [RU]
    assert not (planer_root / "app").exists()                          # файлов привязок больше нет


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
    assert len(lines) == len(msg.KEYS_FILE_HEADER) and all(line.startswith("# ") for line in lines)
    out: str = capsys.readouterr().out
    assert out.splitlines()[0].endswith(" — --status: эфиры планера на каналах")
    assert "Итог: уже стояло 0, ошибок 0" in out.splitlines()
    assert "=====" not in out                                         # пустые блоки не печатаются
    assert re.search(r"^  ключи +keystreams", out, re.MULTILINE)


def test_undated_broadcast_goes_to_attention_not_to_the_log_console(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Замечание площадки приходит данными (take_notices): одна строка — и во «Внимание», и в файле отчёта."""
    _ready(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    fake_platform_in_main.seed_undated_broadcast(UA, "Брифинг без времени")
    assert run_cli(["--dry-run"]) == 0
    captured = capsys.readouterr()
    warning: str = msg.WARNING_UNDATED_BROADCAST.format(account_name=UA, title="Брифинг без времени")
    lines: list[str] = captured.out.splitlines()
    attention: int = next(index for index, line in enumerate(lines) if msg.CONSOLE_BLOCK_ATTENTION in line)
    assert lines[attention + 1] == f"  {warning}"
    [report_file] = list((planer_root / "logs").glob("*_report.md"))
    assert f"- {warning}" in report_file.read_text(encoding="utf-8").splitlines()
    assert "broadcast_without_start" not in captured.err and "broadcast_without_start" not in captured.out


def test_check_reports_every_channel(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    fake_platform_in_main.channel_info[UA] = ChannelInfo(
        youtube_channel_id=f"UCfake{UA}",
        title=UA,
        default_language="uk",
    )
    assert run_cli(["--check"]) == 0
    out: str = capsys.readouterr().out
    assert f"- {UA}: {UA} (id UCfake{UA}), язык канала на YouTube: uk" in out
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


def test_check_logs_in_channel_without_token(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root, UA)
    fake_platform_in_main.tokens_missing = {RU}
    assert run_cli(["--check"]) == 0
    out: str = capsys.readouterr().out
    assert msg.AUTH_STARTING.format(account_name=RU) in out
    assert msg.AUTH_STARTING.format(account_name=UA) not in out
    assert msg.CHECK_ALL_OK in out


def test_check_refuses_channel_renamed_on_youtube(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root)
    fake_platform_in_main.channel_info[UA] = ChannelInfo(
        youtube_channel_id=f"UCfake{UA}", title="Новое имя", default_language=None
    )
    assert run_cli(["--check"]) == 1
    out: str = capsys.readouterr().out
    refusal: str = msg.AUTH_CHANNEL_NAME_MISMATCH.format(
        account_name=UA, youtube_title="Новое имя", channels_file=planer_root / "secrets" / "channels.json"
    )
    assert msg.CHECK_CHANNEL_REFUSED.format(message=refusal) in out.splitlines()
    assert f"- {RU}: {RU} (id UCfake{RU})" in out
    assert fake_platform_in_main.list_calls == [RU]
    assert msg.CHECK_HAS_PROBLEMS in out


def test_auth_all_logs_in_every_channel(
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
    assert msg.AUTH_OK.format(account_name=RU, title=RU, youtube_channel_id=f"UCfake{RU}") in out
    assert [path.name for path in calls] == [f"{UA}.token.json", f"{RU}.token.json"]
    assert [path.parent for path in calls] == [planer_root / "secrets"] * 2


def test_auth_refuses_channel_with_other_title(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    fake_platform_in_main.channel_info[UA] = ChannelInfo(
        youtube_channel_id="UCwrong", title="Другой канал аккаунта", default_language=None
    )
    monkeypatch.setattr(main_module, "load_credentials", _fake_login())
    assert run_cli(["--auth", UA]) == 1
    out: str = capsys.readouterr().out
    assert "«Другой канал аккаунта»" in out and f'planer.bat --auth "{UA}"' in out
    assert msg.AUTH_OK.format(account_name=UA, title="Другой канал аккаунта", youtube_channel_id="UCwrong") not in out


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
