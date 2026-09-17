from __future__ import annotations

import random
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import ChannelConfig, PlanerConfig, load_channels, render_channels_file
from app.google.auth import token_file_for
from app.paths import PlanerPaths
from app.pipeline.runner import RunMode, run
from app.platforms.base import ChannelInfo, PlatformError
from app.platforms.channel import (
    ERROR_CHANNEL_HANDLE_MISMATCH,
    ERROR_CHANNEL_HANDLE_MISSING,
    ERROR_CHANNEL_ID_MISMATCH,
    LOGIN_MAX_ATTEMPTS,
    Channel,
    ChannelBindingError,
    ChannelBook,
    ChannelStatus,
    CheckVerdict,
)
from app.platforms.channel_sync import ChannelSync
from app.platforms.fake import FAKE_TOKEN_TEXT, FakePlatform
from app.platforms.passport import ChannelPassport, PassportEntry
from app.platforms.verified import VerifiedPlatform
from app.tests.conftest import FIXED_NOW, FakeFormSender
from app.ui import messages_ru as msg

ConfigFactory = Callable[..., PlanerConfig]
PASHA_ENCODED: str = "@%D0%9F%D0%B0%D1%88%D0%B0%D0%AD%D0%BA%D1%81%D0%BA%D0%B0%D0%B2%D0%B0%D1%82%D0%BE%D1%89%D0%B8%D0%BA"


class _Console:
    """LoginConsole для тестов: события входа по порядку."""

    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []

    def on_login(self, channel: ChannelConfig) -> None:
        self.events.append(("login", channel.key))

    def on_wrong_channel(self, channel: ChannelConfig, info: ChannelInfo, will_retry: bool) -> None:
        self.events.append(("wrong", channel.key, info.title, str(will_retry)))

    def on_login_failed(self, channel: ChannelConfig, error: PlatformError) -> None:
        self.events.append(("failed", channel.key, error.code))

    def on_channel_ready(self, channel: ChannelConfig, info: ChannelInfo) -> None:
        self.events.append(("ready", channel.key))


def _book(platform: FakePlatform, paths: PlanerPaths, console: _Console | None = None) -> ChannelBook:
    platform.secrets_dir = paths.secrets_dir
    return ChannelBook(platform, ChannelSync(platform, paths, FIXED_NOW), console)


def _info(channel: ChannelConfig, **changes: Any) -> ChannelInfo:
    return replace(FakePlatform.default_channel_info(channel), **changes)


def _entry(channel: ChannelConfig, channel_id: str) -> PassportEntry:
    passport: ChannelPassport = ChannelPassport(Path("passport.json"))
    return passport.record_verified(
        channel, channel, _info(channel, youtube_channel_id=channel_id),
        token_file="t.json", verified_at="01-09-2026 10:00",
    )


def _write_channels(paths: PlanerPaths, *channels: ChannelConfig) -> None:
    paths.channels_file.write_text(render_channels_file(channels), encoding="utf-8")


def _token(paths: PlanerPaths, channel: ChannelConfig) -> Path:
    return token_file_for(paths.secrets_dir, channel.handle)


# --- правило проверки: ник → id по паспорту → название


@pytest.mark.parametrize(
    ("changes", "entry_id", "verdict", "code"),
    [
        ({}, None, CheckVerdict.CONFIRMED, None),
        ({}, "UCfakeyt_ua", CheckVerdict.CONFIRMED, None),
        ({"title": "Новое"}, None, CheckVerdict.ALIGN, None),
        ({"handle_raw": None}, None, CheckVerdict.REFUSED, ERROR_CHANNEL_HANDLE_MISSING),
        ({"handle_raw": "@other"}, None, CheckVerdict.REFUSED, ERROR_CHANNEL_HANDLE_MISMATCH),
        ({"handle_raw": "@other"}, "UCfakeyt_ua", CheckVerdict.ALIGN, None),
        ({}, "UCother", CheckVerdict.REFUSED, ERROR_CHANNEL_ID_MISMATCH),
    ],
    ids=["same", "same_in_passport", "title", "no_handle", "other_handle", "handle_by_id", "other_id"],
)
def test_check_rule(
    make_config: ConfigFactory,
    changes: dict[str, Any],
    entry_id: str | None,
    verdict: CheckVerdict,
    code: str | None,
) -> None:
    config: ChannelConfig = make_config().channels[0]
    channel: Channel = Channel(
        config=config,
        token_file=Path("t.json"),
        passport_entry=_entry(config, entry_id) if entry_id else None,
    )
    check = channel.check(_info(config, **changes))
    assert (check.verdict, check.code) == (verdict, code)


@pytest.mark.parametrize(
    ("handle", "handle_raw"),
    [("@Ukrainian_girl25", "@ukrainian_girl25"), ("@ПашаЭкскаватощик", PASHA_ENCODED), ("@Osvald.X", " Osvald.X ")],
    ids=["lower_case", "percent_encoded", "without_at_and_spaces"],
)
def test_handle_from_youtube_is_compared_by_key(make_config: ConfigFactory, handle: str, handle_raw: str) -> None:
    config: ChannelConfig = replace(make_config().channels[0], account_name="Канал", handle=handle)
    channel: Channel = Channel(config=config, token_file=Path("t.json"))
    assert channel.check(_info(config, handle_raw=handle_raw)).verdict is CheckVerdict.CONFIRMED


def test_title_is_compared_in_nfc_without_edge_spaces(make_config: ConfigFactory) -> None:
    name: str = unicodedata.normalize("NFC", "Канал Лейла")
    config: ChannelConfig = replace(make_config().channels[0], account_name=name, handle="@kanal_leyla")
    channel: Channel = Channel(config=config, token_file=Path("t.json"))
    decomposed: str = unicodedata.normalize("NFD", name)
    assert channel.check(_info(config, title=f"  {decomposed} ")).verdict is CheckVerdict.CONFIRMED


# --- статусы после проверки без браузера


def test_statuses_after_check_without_login(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = make_config(channels=(("yt_ua", ["uk"]), ("yt_ru", ["ru"]), ("yt_en", ["en"])))
    _write_channels(planer_paths, *config.channels)
    ua, ru, en = config.channels
    _token(planer_paths, ua).write_text("t", encoding="utf-8")
    _token(planer_paths, en).write_text("t", encoding="utf-8")
    fake_platform.fail_describe["yt_en"] = PlatformError("backendError", "503")
    book: ChannelBook = _book(fake_platform, planer_paths)
    book.check_without_login(config)
    statuses = {channel.key: book.channel(channel).status for channel in config.channels}
    assert statuses == {"yt_ua": ChannelStatus.READY, "yt_ru": ChannelStatus.NEEDS_LOGIN, "yt_en": ChannelStatus.FAILED}
    assert book.channel(ua).info == FakePlatform.default_channel_info(ua)
    assert fake_platform.logins == []


def test_foreign_token_is_dropped_and_warned(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    """Живой прогон 17-09-2026: токен «Українка Я» вёл на «Lisa Thomson» — файл удаляется, будет вход."""
    config: PlanerConfig = make_config()
    ua: ChannelConfig = config.channels[0]
    _write_channels(planer_paths, *config.channels)
    token: Path = _token(planer_paths, ua)
    token.write_text("t", encoding="utf-8")
    fake_platform.channel_info["yt_ua"] = _info(
        ua, youtube_channel_id="UCawc4wo6XzZPoxvjyzpFtsw", title="Lisa Thomson", handle_raw="@lisathomson-v3l"
    )
    book: ChannelBook = _book(fake_platform, planer_paths)
    _, warnings = book.check_without_login(config)
    assert not token.exists()
    assert book.channel(ua).status is ChannelStatus.NEEDS_LOGIN
    assert warnings == [
        msg.WARNING_TOKEN_REJECTED.format(
            account_name="yt_ua", handle="@yt_ua", youtube_title="Lisa Thomson",
            youtube_handle="@lisathomson-v3l", youtube_channel_id="UCawc4wo6XzZPoxvjyzpFtsw",
        )
    ]


# --- вход


def test_wrong_channel_then_right_channel_is_ready(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = make_config()
    ua: ChannelConfig = config.channels[0]
    _write_channels(planer_paths, *config.channels)
    fake_platform.tokens_missing = {"yt_ua"}
    fake_platform.login_answers["yt_ua"] = [
        _info(ua, youtube_channel_id="UCother", title="Lisa Thomson", handle_raw="@lisathomson-v3l"),
        FakePlatform.default_channel_info(ua),
    ]
    console: _Console = _Console()
    book: ChannelBook = _book(fake_platform, planer_paths, console)
    channel: Channel = book.log_in(ua)
    assert channel.status is ChannelStatus.READY and channel.login_attempts == 2
    assert console.events == [
        ("login", "yt_ua"), ("wrong", "yt_ua", "Lisa Thomson", "True"), ("login", "yt_ua"), ("ready", "yt_ua"),
    ]
    assert _token(planer_paths, ua).read_text(encoding="utf-8") == FAKE_TOKEN_TEXT
    entry = ChannelPassport.load(planer_paths.channels_passport_file)[0].find_by_key("yt_ua")
    assert entry is not None and entry.youtube_channel_id == "UCfakeyt_ua"
    assert fake_platform.kept_logins == ["yt_ua"]


def test_two_wrong_channels_are_refused_without_token(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = make_config()
    ua: ChannelConfig = config.channels[0]
    _write_channels(planer_paths, *config.channels)
    fake_platform.tokens_missing = {"yt_ua"}
    wrong: ChannelInfo = _info(ua, youtube_channel_id="UCother", title="Lisa Thomson", handle_raw="@lisathomson-v3l")
    fake_platform.login_answers["yt_ua"] = [wrong] * LOGIN_MAX_ATTEMPTS
    console: _Console = _Console()
    book: ChannelBook = _book(fake_platform, planer_paths, console)
    channel: Channel = book.log_in(ua)
    assert channel.status is ChannelStatus.REFUSED and channel.login_attempts == LOGIN_MAX_ATTEMPTS
    assert console.events[-1] == ("wrong", "yt_ua", "Lisa Thomson", "False")
    assert not _token(planer_paths, ua).exists() and fake_platform.kept_logins == []
    assert not planer_paths.channels_passport_file.exists()
    assert isinstance(channel.error, ChannelBindingError)
    assert channel.error.code == ERROR_CHANNEL_HANDLE_MISMATCH
    assert channel.error.message == msg.AUTH_CHANNEL_HANDLE_MISMATCH.format(
        account_name="yt_ua", handle="@yt_ua", youtube_title="Lisa Thomson", youtube_handle="@lisathomson-v3l",
        youtube_channel_id="UCother", channels_file=planer_paths.channels_file,
    )
    assert "--auth" not in channel.error.message and msg.AUTH_NEXT_RUN_HINT in channel.error.message
    book.log_in(ua)                                        # статус не NEEDS_LOGIN: третьего входа нет
    assert fake_platform.logins == ["yt_ua"] * LOGIN_MAX_ATTEMPTS


def test_login_failure_is_failed(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    ua: ChannelConfig = make_config().channels[0]
    fake_platform.tokens_missing = {"yt_ua"}
    fake_platform.fail_login["yt_ua"] = PlatformError("authFailed", "flow_failed: browser closed")
    console: _Console = _Console()
    channel: Channel = _book(fake_platform, planer_paths, console).log_in(ua)
    assert channel.status is ChannelStatus.FAILED and channel.login_attempts == 1
    assert channel.error is not None and channel.error.code == "authFailed"
    assert console.events == [("login", "yt_ua"), ("failed", "yt_ua", "authFailed")]
    assert not _token(planer_paths, ua).exists()


def test_login_with_other_title_aligns_channels_file(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    """Ник совпал, название на YouTube новое: токен записан, channels.json выровнен для следующих запусков."""
    ua: ChannelConfig = make_config().channels[0]
    _write_channels(planer_paths, ua)
    fake_platform.tokens_missing = {"yt_ua"}
    fake_platform.channel_info["yt_ua"] = _info(ua, title="Новое название")
    book: ChannelBook = _book(fake_platform, planer_paths)
    assert book.log_in(ua).status is ChannelStatus.READY
    assert [item.account_name for item in load_channels(planer_paths.channels_file)] == ["Новое название"]
    assert _token(planer_paths, ua).exists()
    [warning] = book.take_warnings()
    assert warning.endswith(msg.WARNING_CHANNEL_ALIGNED_IN_RUN)


def test_forced_login_keeps_old_token_until_confirmed(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    ua: ChannelConfig = make_config().channels[0]
    token: Path = _token(planer_paths, ua)
    token.write_text("old", encoding="utf-8")
    fake_platform.login_answers["yt_ua"] = [_info(ua, handle_raw="@other")] * LOGIN_MAX_ATTEMPTS
    book: ChannelBook = _book(fake_platform, planer_paths)
    assert book.log_in(ua, force=True).status is ChannelStatus.REFUSED
    assert token.read_text(encoding="utf-8") == "old"
    fake_platform.login_answers["yt_ua"] = [FakePlatform.default_channel_info(ua)]
    assert _book(fake_platform, planer_paths).log_in(ua, force=True).status is ChannelStatus.READY
    assert token.read_text(encoding="utf-8") == FAKE_TOKEN_TEXT


def test_channel_without_objects_does_not_log_in(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    config: PlanerConfig = make_config()
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    fake_platform.tokens_missing = {"yt_ua", "yt_ru"}
    book: ChannelBook = _book(fake_platform, planer_paths)
    book.check_without_login(config)
    run(RunMode.DRY_RUN, config, planer_paths, VerifiedPlatform(fake_platform, book), FakeFormSender(), now, rng,
        logins=book)
    assert fake_platform.logins == ["yt_ua"]
    assert book.channel(config.channels[1]).status is ChannelStatus.NEEDS_LOGIN
