from __future__ import annotations

import json
import logging
import random
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app import main as main_module
from app.config.loader import PlanerSettings
from app.google.auth import LOGIN_TIMEOUT_MINUTES
from app.main import run_cli
from app.paths import ROOT_ENV_VAR, PlanerPaths
from app.platforms.base import BroadcastPlatform, ChannelInfo, PlatformError
from app.platforms.fake import FAKE_TOKEN_TEXT, FakePlatform
from app.records.record_store import RecordStore
from app.tests.conftest import FakeFormSender
from app.ui import messages_ru as msg
from app.version import APP_VERSION

UA: str = "Канал UA"
RU: str = "Канал RU"
UA_HANDLE: str = "@KanalUA"
RU_HANDLE: str = "@KanalRU"
UA_KEY: str = "kanalua"
RU_KEY: str = "kanalru"
UA_NAMES: dict[str, str] = {"account_name": UA, "handle": UA_HANDLE}
RU_NAMES: dict[str, str] = {"account_name": RU, "handle": RU_HANDLE}
UA_GOOGLE: str = "ua@gmail.com"
RU_GOOGLE: str = "ru@gmail.com"
CHANNEL_KEYS: tuple[str, ...] = (UA_KEY, RU_KEY)
CHANNELS_JSON: dict[str, Any] = {
    "channels": [
        {"platform": "youtube", "account_name": UA, "handle": UA_HANDLE, "google_account": UA_GOOGLE,
         "languages": ["uk"], "privacy": "public"},
        {"platform": "youtube", "account_name": RU, "handle": RU_HANDLE, "google_account": RU_GOOGLE,
         "languages": ["ru", "en"], "privacy": "unlisted"},
    ]
}
PackageFactory = Callable[..., Path]
SlotFactory = Callable[..., dict[str, Any]]


@pytest.fixture
def fake_platform_in_main(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    """Площадка и отправитель формы в main подменяются фейками: сеть в тестах запрещена.

    Книга каналов, шлюз (VerifiedPlatform) и печать входа остаются боевыми; токен нового входа фейк пишет в secrets.
    """
    platform: FakePlatform = FakePlatform()

    def _build(paths: PlanerPaths, settings: PlanerSettings, rng: random.Random) -> BroadcastPlatform:
        platform.secrets_dir = paths.secrets_dir
        return platform

    monkeypatch.setattr(main_module, "build_platform", _build)
    monkeypatch.setattr(main_module, "build_form_sender", lambda paths, now_utc, rng: FakeFormSender())
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


def _write_tokens(root: Path, *handles: str) -> None:
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    for handle in handles or (UA_HANDLE, RU_HANDLE):
        (root / "secrets" / f"{handle}.token.json").write_text("{}", encoding="utf-8")


def _ready(root: Path) -> None:
    _write_config(root)
    _write_tokens(root)


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
    assert lines.count("Итог: опубликовано 1, исправлено 0, уже стояло 0, не допущено 0, не публиковали 0, ошибок 0") == 1
    assert sum(1 for line in lines if line.startswith("Planer ")) == 1          # шапка одна на прогон
    assert f"  {UA} {UA_HANDLE} ({UA_GOOGLE})" in lines
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
        "  " + msg.PROGRESS_CHANNEL_READ_STARTED.format(**RU_NAMES),
        "  " + msg.PROGRESS_CHANNEL_READ_DONE.format(**RU_NAMES, count=0),
        "  " + msg.PROGRESS_CHANNEL_READ_STARTED.format(**UA_NAMES),
        "  " + msg.PROGRESS_CHANNEL_READ_DONE.format(**UA_NAMES, count=0),
        # по объекту: создан — и сразу его ключ в форму, затем следующий объект
        "  " + msg.PROGRESS_BROADCAST_CREATE.format(**RU_NAMES, date="01-01-2099", time="19:00", language="ru"),
        "  " + msg.PROGRESS_KEY_SEND.format(**RU_NAMES, date="01-01-2099", time="19:00", language="ru"),
        "  " + msg.PROGRESS_BROADCAST_CREATE.format(**UA_NAMES, date="01-01-2099", time="19:00", language="uk"),
        "  " + msg.PROGRESS_KEY_SEND.format(**UA_NAMES, date="01-01-2099", time="19:00", language="uk"),
        "  " + msg.PROGRESS_REPORT,
    ]
    assert lines[1 : 1 + len(progress)] == progress
    total: int = lines.index("Итог: опубликовано 2, исправлено 0, уже стояло 0, не допущено 0, не публиковали 0, ошибок 0")
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
    assert "  " + msg.PROGRESS_CHANNEL_READ_DONE.format(**UA_NAMES, count=0) in lines
    assert "  " + msg.PROGRESS_PACKAGES_READ.format(packages=1, slots_total=1, slots_mine=1) in lines
    assert not [line for line in lines if "создаю эфир" in line or "исправляю эфир" in line or "отправляю ключ" in line]
    assert lines.index("  " + msg.PROGRESS_REPORT) < lines.index(
        "Итог: опубликуем 1, исправим 0, уже стояло 0, не допущено 0, не публиковали 0, ошибок 0"
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
    fake_platform_in_main.tokens_missing = set(CHANNEL_KEYS)
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
    lines: list[str] = out.splitlines()
    fields: list[str] = [
        "  languages — языки стримов этого канала: " + msg.CONFIG_LANGUAGES_RULE + ";",
        "  privacy — видимость эфиров: public, unlisted;",
        "  platform — youtube.",
    ]
    assert all(line in lines for line in fields)
    hint: int = lines.index(msg.CONFIG_CHANNELS_HINT.format(path=channels_file))
    assert lines[hint + 1].startswith("  account_name — название канала как на YouTube")
    assert lines[hint + 2].startswith("  handle — ник канала на YouTube, начинается с @")
    assert lines[hint + len(msg.CONFIG_CHANNELS_FIELDS) + 1] == msg.CONFIG_CHANNELS_TEMPLATE.splitlines()[0]
    assert "Osvald.X" not in out
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


def test_missing_token_logs_in_before_any_channel_is_read(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Фаза входов — до строк «запрашиваю канал»; канал без объектов не входит."""
    _write_config(planer_root)
    fake_platform_in_main.tokens_missing = set(CHANNEL_KEYS)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    assert run_cli(["--dry-run"]) == 0
    out: str = capsys.readouterr().out
    # порядок: что происходит, какой аккаунт, какой канал, и только потом длинное предупреждение
    login_lines: list[str] = [
        msg.AUTH_STARTING.format(**UA_NAMES),
        msg.AUTH_CHOOSE_ACCOUNT.format(google_account=UA_GOOGLE, **UA_NAMES),
        msg.AUTH_CHOOSE_RIGHT_CHANNEL.format(**UA_NAMES),
        msg.AUTH_UNVERIFIED_APP_WARNING,
    ]
    positions: list[int] = [out.index(line) for line in login_lines]
    assert positions == sorted(positions)
    assert msg.AUTH_OK.format(
        **UA_NAMES, title=UA, youtube_handle=UA_HANDLE, youtube_channel_id=f"UCfake{UA_KEY}"
    ) in out
    # шапка — до входа, итог — после
    assert out.index(f"Planer {APP_VERSION} — ") < out.index(msg.AUTH_STARTING.format(**UA_NAMES))
    assert out.index("Google hasn't verified this app") < out.index("Итог: ")
    ok_line: str = msg.AUTH_OK.format(
        **UA_NAMES, title=UA, youtube_handle=UA_HANDLE, youtube_channel_id=f"UCfake{UA_KEY}"
    )
    assert out.index(ok_line) < out.index(msg.PROGRESS_CHANNEL_READ_STARTED.format(**UA_NAMES))
    assert fake_platform_in_main.logins == [UA_KEY]
    assert fake_platform_in_main.describe_calls == [UA_KEY]        # сверка при старте без токенов канал не спрашивает
    assert (planer_root / "secrets" / f"{UA_HANDLE}.token.json").is_file()   # токен — после подтверждения
    assert not (planer_root / "secrets" / f"{RU_HANDLE}.token.json").exists()


def test_channel_handle_mismatch_fails_only_that_channel(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root)
    fake_platform_in_main.channel_info[UA_KEY] = ChannelInfo(
        youtube_channel_id="UCsomeoneElse", title="Чужой канал", default_language=None, handle_raw="@chuzhoy"
    )
    make_package(
        planer_root / "bcast",
        slots=[make_slot("01-01-2099", "19:00", "uk"), make_slot("01-01-2099", "19:00", "ru")],
    )
    assert run_cli([]) == 1
    out: str = capsys.readouterr().out
    assert "«Чужой канал»" in out
    lines: list[str] = out.splitlines()
    names: dict[str, str] = {**UA_NAMES, "youtube_title": "Чужой канал", "youtube_handle": "@chuzhoy"}
    assert msg.AUTH_WRONG_CHANNEL_RETRY.format(**names) in lines
    assert msg.AUTH_WRONG_CHANNEL_GIVE_UP.format(**names) in lines
    assert "--auth" not in out
    assert not (planer_root / "secrets" / f"{UA_HANDLE}.token.json").exists()   # чужой токен удалён при старте
    assert fake_platform_in_main.logins == [UA_KEY, UA_KEY]
    assert f"  {RU} {RU_HANDLE} ({RU_GOOGLE})" in lines
    # полный текст отказа — один раз на канал; объект канала — коротко во «Внимание»
    assert sum(1 for line in lines if line.startswith("  ошибка: Канал UA @KanalUA — YouTube: channelHandleMismatch (")) == 1
    assert "  не допущено: 01-01-2099 19:00 uk -> Канал UA @KanalUA — не тот канал; " + msg.NOT_ADMITTED_TAIL_CHANNEL in lines
    assert "не допущено 1" in lines[lines.index(next(line for line in lines if line.startswith("Итог: ")))]
    assert [call.channel_id for call in fake_platform_in_main.created] == [RU_KEY]
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
    assert "Итог: опубликуем 2, исправим 0, уже стояло 0, не допущено 0, не публиковали 0, ошибок 0" in captured.out
    assert "ОПУБЛИКУЕМ (2)" in captured.out and "КЛЮЧИ СТРИМЕРУ" not in captured.out
    # каналы — в порядке channels.json: сначала UA, потом RU
    assert captured.out.index(f"  {UA} {UA_HANDLE} ({UA_GOOGLE})") < captured.out.index(
        f"  {RU} {RU_HANDLE} ({RU_GOOGLE})"
    )
    assert "будет создан" not in captured.out                          # подробности — в отчёте
    assert "run_started" not in captured.err
    [report] = list((planer_root / "logs").glob("*_report.md"))
    report_text: str = report.read_text(encoding="utf-8")
    assert f"{path.name} — принят, слотов 2, из них под мои языки 2" in report_text
    assert f"- 01-01-2099 19:00 en -> {RU} {RU_HANDLE} — эфира нет, будет создан — не выполнено (dry-run)" in report_text
    assert f"- 01-01-2099 19:00 uk -> {UA} {UA_HANDLE} — эфира нет, будет создан — не выполнено (dry-run)" in report_text
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
    fake_platform_in_main.seed_undated_broadcast(UA, "Брифинг без времени", handle=UA_HANDLE)
    assert run_cli(["--dry-run"]) == 0
    captured = capsys.readouterr()
    warning: str = msg.WARNING_UNDATED_BROADCAST.format(channel=f"{UA} {UA_HANDLE}", title="Брифинг без времени")
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
    fake_platform_in_main.channel_info[UA_KEY] = ChannelInfo(
        youtube_channel_id=f"UCfake{UA_KEY}",
        title=UA,
        default_language="uk",
        handle_raw="@kanalua",
    )
    assert run_cli(["--check"]) == 0
    out: str = capsys.readouterr().out
    assert f"- «{UA}» {UA_HANDLE}: «{UA}» @kanalua (id UCfake{UA_KEY}), язык канала на YouTube: uk" in out
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
    fake_platform_in_main.fail_list[RU_KEY] = PlatformError("liveStreamingNotEnabled", "трансляции не включены")
    assert run_cli(["--check"]) == 1
    out: str = capsys.readouterr().out
    assert f"- «{RU}» {RU_HANDLE}: liveStreamingNotEnabled (трансляции не включены)" in out
    assert msg.CHECK_HAS_PROBLEMS in out


def test_check_logs_in_channel_without_token(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root, UA_HANDLE)
    fake_platform_in_main.tokens_missing = {RU_KEY}
    assert run_cli(["--check"]) == 0
    out: str = capsys.readouterr().out
    assert msg.AUTH_STARTING.format(**RU_NAMES) in out
    assert msg.AUTH_STARTING.format(**UA_NAMES) not in out
    assert msg.CHECK_ALL_OK in out


def test_check_aligns_channel_renamed_on_youtube_without_login(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ник совпал, название на YouTube новое: channels.json выровнен при старте, вход не нужен."""
    _write_config(planer_root)
    _write_tokens(planer_root)
    fake_platform_in_main.channel_info[UA_KEY] = ChannelInfo(
        youtube_channel_id=f"UCfake{UA_KEY}", title="Новое имя", default_language=None, handle_raw=UA_HANDLE
    )
    assert run_cli(["--check"]) == 0
    lines: list[str] = capsys.readouterr().out.splitlines()
    aligned: str = msg.WARNING_CHANNEL_ALIGNED.format(
        youtube_channel_id=f"UCfake{UA_KEY}", title_before=UA, handle_before=UA_HANDLE,
        title_after="Новое имя", handle_after=UA_HANDLE,
    )
    assert f"  {aligned}" in lines
    assert f"- «Новое имя» {UA_HANDLE}: «Новое имя» {UA_HANDLE} (id UCfake{UA_KEY}), язык канала на YouTube: не указан; " \
        "языки стримов из channels.json: uk; запланированных эфиров: 0" in lines
    secrets: Path = planer_root / "secrets"
    assert '"account_name": "Новое имя"' in (secrets / "channels.json").read_text(encoding="utf-8")
    assert json.loads((secrets / "channels.previous.json").read_text(encoding="utf-8")) == CHANNELS_JSON
    assert (secrets / "channels_passport.json").is_file()
    assert fake_platform_in_main.logins == []


def test_check_refuses_channel_with_other_handle(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root)
    fake_platform_in_main.channel_info[UA_KEY] = ChannelInfo(
        youtube_channel_id="UCother", title="Другой", default_language=None, handle_raw="@drugoy"
    )
    assert run_cli(["--check"]) == 1
    out: str = capsys.readouterr().out
    refusal: str = msg.AUTH_CHANNEL_HANDLE_MISMATCH.format(
        **UA_NAMES, youtube_title="Другой", youtube_handle="@drugoy", youtube_channel_id="UCother",
        channels_file=planer_root / "secrets" / "channels.json",
    )
    assert msg.CHECK_CHANNEL_REFUSED.format(message=refusal) in out.splitlines()
    assert f"- «{RU}» {RU_HANDLE}: «{RU}» {RU_HANDLE} (id UCfake{RU_KEY})" in out
    assert fake_platform_in_main.list_calls == [RU_KEY]
    assert msg.CHECK_HAS_PROBLEMS in out


def test_auth_all_logs_in_every_channel(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    assert run_cli(["--auth", "all"]) == 0
    out: str = capsys.readouterr().out
    assert "Google hasn't verified this app" in out
    assert msg.AUTH_OK.format(**RU_NAMES, title=RU, youtube_handle=RU_HANDLE, youtube_channel_id=f"UCfake{RU_KEY}") in out
    assert fake_platform_in_main.logins == [UA_KEY, RU_KEY]
    assert fake_platform_in_main.describe_without_login == []        # --auth без проверки при старте
    for handle in (UA_HANDLE, RU_HANDLE):
        assert (planer_root / "secrets" / f"{handle}.token.json").read_text(encoding="utf-8") == FAKE_TOKEN_TEXT


def test_auth_refused_keeps_the_previous_token(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--auth: дважды выбран другой канал — прежний токен как был, текст отказа без «planer.bat --auth»."""
    _write_config(planer_root)
    _write_tokens(planer_root, UA_HANDLE)
    fake_platform_in_main.channel_info[UA_KEY] = ChannelInfo(
        youtube_channel_id="UCwrong", title="Другой канал аккаунта", default_language=None, handle_raw="@drugoy"
    )
    assert run_cli(["--auth", "kanalua"]) == 1                      # ник — без «@» и в другом регистре
    out: str = capsys.readouterr().out
    assert "«Другой канал аккаунта»" in out and "planer.bat --auth" not in out
    assert msg.AUTH_NEXT_RUN_HINT in out
    assert "вход выполнен" not in out
    assert fake_platform_in_main.logins == [UA_KEY, UA_KEY]
    assert (planer_root / "secrets" / f"{UA_HANDLE}.token.json").read_text(encoding="utf-8") == "{}"


def test_auth_confirmed_replaces_the_previous_token(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    _write_tokens(planer_root, UA_HANDLE)
    wrong: ChannelInfo = ChannelInfo(
        youtube_channel_id="UCwrong", title="Другой", default_language=None, handle_raw="@drugoy"
    )
    right: ChannelInfo = ChannelInfo(
        youtube_channel_id=f"UCfake{UA_KEY}", title=UA, default_language=None, handle_raw=UA_HANDLE
    )
    fake_platform_in_main.login_answers[UA_KEY] = [wrong, right]
    assert run_cli(["--auth", UA_HANDLE]) == 0
    out: str = capsys.readouterr().out
    assert msg.AUTH_WRONG_CHANNEL_RETRY.format(**UA_NAMES, youtube_title="Другой", youtube_handle="@drugoy") in out
    assert (planer_root / "secrets" / f"{UA_HANDLE}.token.json").read_text(encoding="utf-8") == FAKE_TOKEN_TEXT


def test_auth_unknown_channel_lists_handles(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    assert run_cli(["--auth", "yt_unknown"]) == 1
    assert f"{UA_HANDLE} «{UA}», {RU_HANDLE} «{RU}»" in capsys.readouterr().out


def test_auth_failure_names_the_reason_and_scope_hint(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(planer_root)
    fake_platform_in_main.fail_login[UA_KEY] = PlatformError("authFailed", "flow_failed: browser closed")
    assert run_cli(["--auth", UA_HANDLE]) == 1
    out: str = capsys.readouterr().out
    assert msg.AUTH_REASON_TEXT["flow_failed"] in out
    assert "скоуп youtube не добавлен" in out
    assert not (planer_root / "secrets" / f"{UA_HANDLE}.token.json").exists()


def test_login_timeout_names_the_reason_without_scope_hint(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Вход не завершён за 10 минут: причина словами, подсказки про скоуп нет, объекты канала не допущены."""
    _write_config(planer_root)
    fake_platform_in_main.tokens_missing = {UA_KEY}
    fake_platform_in_main.fail_login[UA_KEY] = PlatformError("authFailed", "login_timeout: no answer in 600 s")
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    assert run_cli([]) == 1
    out: str = capsys.readouterr().out
    timeout_text: str = msg.AUTH_REASON_TEXT["login_timeout"].format(minutes=LOGIN_TIMEOUT_MINUTES)
    assert msg.AUTH_FAILED.format(**UA_NAMES, reason=timeout_text) in out
    assert "скоуп youtube не добавлен" not in out
    assert fake_platform_in_main.logins == [UA_KEY]                 # второй попытки нет
    assert fake_platform_in_main.created == []
    assert "login_timeout channel=" in _log_text(planer_root)


def test_terminal_has_no_raw_log_lines(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """WARNING и ERROR планера и чужих библиотек — только в файл лога; в терминале — тексты владельца."""

    class _NoisySender(FakeFormSender):
        def prepare(self, forms: Any) -> None:
            logging.getLogger("googleapiclient.discovery_cache").warning("file_cache is unavailable")
            super().prepare(forms)

    monkeypatch.setattr(main_module, "build_form_sender", lambda paths, now_utc, rng: _NoisySender())
    _write_config(planer_root)
    _write_tokens(planer_root, RU_HANDLE)
    fake_platform_in_main.tokens_missing = {UA_KEY}
    fake_platform_in_main.fail_login[UA_KEY] = PlatformError("authFailed", "flow_failed: browser closed")
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    assert run_cli([]) == 1
    captured: pytest.CaptureResult[str] = capsys.readouterr()
    for marker in (" | WARNING | ", " | INFO | ", " | ERROR | ", " | DEBUG | "):
        assert marker not in captured.out and marker not in captured.err
    assert captured.err == ""
    assert "не допущено: " in captured.out                           # та же причина — текстом владельца
    log: str = _log_text(planer_root)
    assert " | WARNING | planer.runner | slot_not_admitted " in log
    assert " | ERROR | planer.channel | login_failed " in log
    assert " | WARNING | googleapiclient.discovery_cache | file_cache is unavailable" in log


def _log_text(root: Path) -> str:
    [log_file] = list((root / "logs").glob("*_planer.log"))
    return log_file.read_text(encoding="utf-8")


def test_interrupted_run_is_logged_and_exits_1(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl+C во время входа: строка в лог и в консоль, код 1, run_finished есть."""
    _write_config(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    fake_platform_in_main.tokens_missing = {UA_KEY}
    fake_platform_in_main.fail_login[UA_KEY] = KeyboardInterrupt()  # type: ignore[assignment]
    assert run_cli([]) == 1
    assert msg.RUN_INTERRUPTED in capsys.readouterr().out.splitlines()
    log: str = _log_text(planer_root)
    assert "| run_interrupted" in log and "| run_finished exit_code=1" in log


def test_crashed_run_writes_the_traceback_to_the_log(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("сломалось внутри")

    monkeypatch.setattr(main_module, "render_console", _boom)
    assert run_cli([]) == 1
    [log_file] = list((planer_root / "logs").glob("*_planer.log"))
    assert msg.RUN_CRASHED.format(log=log_file) in capsys.readouterr().out.splitlines()
    log: str = _log_text(planer_root)
    assert "| run_crashed " in log and "Traceback (most recent call last)" in log
    assert "RuntimeError: сломалось внутри" in log and "| run_finished exit_code=1" in log


@pytest.fixture
def opened_stores(monkeypatch: pytest.MonkeyPatch) -> list[RecordStore]:
    """Каждая память, которую открыл main: после запуска она должна быть закрыта."""
    opened: list[RecordStore] = []
    original = RecordStore.open.__func__   # type: ignore[attr-defined]

    def _open(cls: type[RecordStore], path: Path, *, read_only: bool, now_local: Any) -> RecordStore:
        store: RecordStore = original(cls, path, read_only=read_only, now_local=now_local)
        opened.append(store)
        return store

    monkeypatch.setattr(RecordStore, "open", classmethod(_open))
    return opened


def test_memory_is_opened_for_the_run_and_closed(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    opened_stores: list[RecordStore],
) -> None:
    _ready(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    assert run_cli(["--dry-run"]) == 0
    assert not (planer_root / "secrets" / "planer.sqlite3").exists()        # dry-run память не создаёт
    assert run_cli([]) == 0
    assert (planer_root / "secrets" / "planer.sqlite3").is_file()
    assert [(store.is_read_only, store.is_closed) for store in opened_stores] == [(True, True), (False, True)]


def test_memory_is_closed_when_the_run_is_interrupted(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    opened_stores: list[RecordStore],
) -> None:
    _write_config(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    fake_platform_in_main.tokens_missing = {UA_KEY}
    fake_platform_in_main.fail_login[UA_KEY] = KeyboardInterrupt()  # type: ignore[assignment]
    assert run_cli([]) == 1
    [store] = opened_stores
    assert store.is_closed
    assert "| run_interrupted" in _log_text(planer_root)


def test_memory_is_closed_when_the_run_crashes(
    planer_root: Path,
    fake_platform_in_main: FakePlatform,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    opened_stores: list[RecordStore],
) -> None:
    _ready(planer_root)
    make_package(planer_root / "bcast", slots=[make_slot("01-01-2099", "19:00", "uk")])
    fake_platform_in_main.fail_list[UA_KEY] = RuntimeError("сломалось в сверке")  # type: ignore[assignment]
    assert run_cli([]) == 1
    [store] = opened_stores
    assert store.is_closed
    log: str = _log_text(planer_root)
    assert "| run_crashed " in log and "RuntimeError: сломалось в сверке" in log
