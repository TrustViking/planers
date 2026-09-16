from __future__ import annotations

import json
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
from app.output.report import OutcomeKind
from app.paths import PlanerPaths
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, run
from app.platforms.base import ChannelInfo, PlatformError, PlatformNoticeKind
from app.platforms.channel_sync import ChannelSync
from app.platforms.fake import FakePlatform
from app.platforms.passport import ChannelPassport
from app.platforms.verified import (
    ERROR_CHANNEL_HANDLE_MISMATCH,
    ERROR_CHANNEL_HANDLE_MISSING,
    ERROR_CHANNEL_ID_MISMATCH,
    ChannelBindingError,
    VerifiedPlatform,
)
from app.tests.conftest import FIXED_NOW, FakeFormSender
from app.ui import messages_ru as msg

ConfigFactory = Callable[..., PlanerConfig]
PASHA_ENCODED: str = "@%D0%9F%D0%B0%D1%88%D0%B0%D0%AD%D0%BA%D1%81%D0%BA%D0%B0%D0%B2%D0%B0%D1%82%D0%BE%D1%89%D0%B8%D0%BA"


class _Listener:
    def __init__(self) -> None:
        self.ready: list[str] = []

    def on_channel_ready(self, channel: ChannelConfig, info: ChannelInfo) -> None:
        self.ready.append(channel.account_name)


def _sync(platform: FakePlatform, paths: PlanerPaths) -> ChannelSync:
    return ChannelSync(platform, paths, FIXED_NOW)


def _verified(platform: FakePlatform, paths: PlanerPaths, listener: _Listener | None = None) -> VerifiedPlatform:
    return VerifiedPlatform(platform, _sync(platform, paths), listener=listener)


def _channel(make_config: ConfigFactory, account_name: str, handle: str) -> ChannelConfig:
    return replace(make_config().channels[0], account_name=account_name, handle=handle)


def _answer(platform: FakePlatform, channel: ChannelConfig, **changes: Any) -> ChannelInfo:
    """Ответ YouTube для канала: по умолчанию «тот же канал», поля — из changes."""
    info: ChannelInfo = replace(FakePlatform.default_channel_info(channel), **changes)
    platform.channel_info[channel.key] = info
    return info


def _write_channels(paths: PlanerPaths, *channels: ChannelConfig) -> None:
    paths.channels_file.write_text(render_channels_file(channels), encoding="utf-8")


def _seed_passport(paths: PlanerPaths, platform: FakePlatform, channel: ChannelConfig, channel_id: str) -> None:
    passport: ChannelPassport = ChannelPassport(paths.channels_passport_file)
    info: ChannelInfo = replace(FakePlatform.default_channel_info(channel), youtube_channel_id=channel_id)
    passport.record_verified(channel, channel, info, token_file=token_file_for(paths.secrets_dir, channel.handle).name,
                             verified_at="01-09-2026 10:00")
    assert passport.save() is None


def test_matching_channel_is_described_once(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    listener: _Listener = _Listener()
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, listener)
    channel: ChannelConfig = make_config().channels[0]
    platform.list_upcoming(channel)
    platform.list_upcoming(channel)
    assert fake_platform.describe_calls == ["yt_ua"]
    assert fake_platform.list_calls == ["yt_ua", "yt_ua"]
    assert listener.ready == ["yt_ua"]


def test_verification_writes_only_the_passport(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    before: list[Path] = sorted(planer_paths.root.rglob("*"))
    _verified(fake_platform, planer_paths).list_upcoming(make_config().channels[0])
    after: list[Path] = sorted(planer_paths.root.rglob("*"))
    assert [path for path in after if path not in before] == [planer_paths.channels_passport_file]


def test_login_happens_at_first_access(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    fake_platform.tokens_missing = {"yt_ua"}
    logins: list[str] = []
    fake_platform.on_login = lambda channel: logins.append(channel.account_name)
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    assert logins == []
    platform.list_upcoming(make_config().channels[0])
    assert logins == ["yt_ua"]


@pytest.mark.parametrize(
    ("handle", "handle_raw"),
    [("@Ukrainian_girl25", "@ukrainian_girl25"), ("@ПашаЭкскаватощик", PASHA_ENCODED), ("@Osvald.X", " Osvald.X ")],
    ids=["lower_case", "percent_encoded", "without_at_and_spaces"],
)
def test_handle_from_youtube_is_compared_by_key(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    handle: str,
    handle_raw: str,
) -> None:
    channel: ChannelConfig = _channel(make_config, "Канал", handle)
    _answer(fake_platform, channel, handle_raw=handle_raw)
    assert _verified(fake_platform, planer_paths).verify(channel).handle_raw == handle_raw
    entry = ChannelPassport.load(planer_paths.channels_passport_file)[0].find_by_key(channel.key)
    assert entry is not None and entry.handle == handle and entry.youtube_handle_raw == handle_raw


def _assert_refused_until_end_of_run(
    platform: VerifiedPlatform, fake_platform: FakePlatform, channel: ChannelConfig, code: str
) -> ChannelBindingError:
    with pytest.raises(ChannelBindingError) as raised:
        platform.list_upcoming(channel)
    assert raised.value.code == code
    with pytest.raises(ChannelBindingError):
        platform.create_broadcast(channel, None)  # type: ignore[arg-type]   # до площадки не доходит
    assert fake_platform.describe_calls == [channel.key]      # второй вызов на площадку не пошёл
    assert fake_platform.list_calls == [] and fake_platform.created == []
    return raised.value


def test_channel_without_handle_is_refused(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    channel: ChannelConfig = make_config().channels[0]
    info: ChannelInfo = _answer(fake_platform, channel, handle_raw=None, title="Без ника")
    listener: _Listener = _Listener()
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, listener)
    error = _assert_refused_until_end_of_run(platform, fake_platform, channel, ERROR_CHANNEL_HANDLE_MISSING)
    assert error.message == msg.AUTH_CHANNEL_HANDLE_MISSING.format(
        account_name="yt_ua", handle="@yt_ua", youtube_title="Без ника",
        youtube_channel_id=info.youtube_channel_id, channels_file=planer_paths.channels_file,
    )
    assert listener.ready == []
    assert not planer_paths.channels_passport_file.exists()


def test_other_handle_without_passport_is_refused(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    channel: ChannelConfig = make_config().channels[0]
    _answer(fake_platform, channel, handle_raw="@chuzhoy", title="Чужой канал")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    error = _assert_refused_until_end_of_run(platform, fake_platform, channel, ERROR_CHANNEL_HANDLE_MISMATCH)
    assert "@chuzhoy" in error.message and "«Чужой канал»" in error.message and "@yt_ua" in error.message


def test_same_handle_with_other_id_in_passport_is_refused_and_files_kept(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    channel: ChannelConfig = make_config().channels[0]
    _write_channels(planer_paths, channel)
    _seed_passport(planer_paths, fake_platform, channel, "UCother")
    files: dict[Path, bytes] = {path: path.read_bytes() for path in planer_paths.secrets_dir.iterdir()}
    info: ChannelInfo = _answer(fake_platform, channel, title="Новое название")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    error = _assert_refused_until_end_of_run(platform, fake_platform, channel, ERROR_CHANNEL_ID_MISMATCH)
    assert error.message == msg.AUTH_CHANNEL_ID_MISMATCH.format(
        account_name="yt_ua", handle="@yt_ua", passport_channel_id="UCother", youtube_title="Новое название",
        youtube_handle="@yt_ua", youtube_channel_id=info.youtube_channel_id,
        token_file=token_file_for(planer_paths.secrets_dir, "@yt_ua"),
    )
    assert {path: path.read_bytes() for path in planer_paths.secrets_dir.iterdir()} == files


def test_other_title_after_login_is_aligned_for_next_runs(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    """Ник совпал — название на YouTube правит channels.json; этот запуск работает со старым."""
    channel: ChannelConfig = make_config().channels[0]
    _write_channels(planer_paths, channel)
    fake_platform.tokens_missing = {"yt_ua"}
    info: ChannelInfo = _answer(fake_platform, channel, title="Новое название")
    listener: _Listener = _Listener()
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, listener)
    platform.list_upcoming(channel)
    assert listener.ready == ["yt_ua"] and fake_platform.logins == ["yt_ua"]
    assert [item.account_name for item in load_channels(planer_paths.channels_file)] == ["Новое название"]
    assert planer_paths.channels_previous_file.read_text(encoding="utf-8") == render_channels_file([channel])
    entry = ChannelPassport.load(planer_paths.channels_passport_file)[0].find_by_key("yt_ua")
    assert entry is not None and entry.account_name == "Новое название" and entry.previous_titles == ("yt_ua",)
    [notice] = platform.take_notices()
    assert notice.kind is PlatformNoticeKind.CHANNEL
    assert notice.text == msg.WARNING_CHANNEL_ALIGNED.format(
        youtube_channel_id=info.youtube_channel_id, title_before="yt_ua", handle_before="@yt_ua",
        title_after="Новое название", handle_after="@yt_ua",
    ) + msg.WARNING_CHANNEL_ALIGNED_IN_RUN


def test_other_handle_confirmed_by_passport_is_aligned_in_run(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    channel: ChannelConfig = make_config().channels[0]
    _write_channels(planer_paths, channel)
    info: ChannelInfo = _answer(fake_platform, channel, handle_raw="@yt_ua_new")
    _seed_passport(planer_paths, fake_platform, channel, info.youtube_channel_id)
    token: Path = token_file_for(planer_paths.secrets_dir, "@yt_ua")
    token.write_text("token", encoding="utf-8")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    platform.list_upcoming(channel)
    assert fake_platform.list_calls == ["yt_ua"]              # этот запуск — со старым ключом канала
    assert [item.handle for item in load_channels(planer_paths.channels_file)] == ["@yt_ua_new"]
    assert not token.exists()
    assert token_file_for(planer_paths.secrets_dir, "@yt_ua_new").read_text(encoding="utf-8") == "token"
    passport: ChannelPassport = ChannelPassport.load(planer_paths.channels_passport_file)[0]
    assert passport.find_by_key("yt_ua") is None
    entry = passport.find_by_key("yt_ua_new")
    assert entry is not None and entry.previous_handles == ("@yt_ua",)
    assert [notice.text for notice in platform.take_notices()][0].endswith(msg.WARNING_CHANNEL_ALIGNED_IN_RUN)


def test_title_is_compared_in_nfc_without_edge_spaces(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    """«й» из «и» и знака на YouTube — то же название, что «й» одним символом в channels.json: не выравнивается."""
    name: str = unicodedata.normalize("NFC", "Канал Лейла")
    decomposed: str = unicodedata.normalize("NFD", name)
    assert decomposed != name
    channel: ChannelConfig = _channel(make_config, name, "@kanal_leyla")
    _answer(fake_platform, channel, title=f"  {decomposed} ")
    assert _verified(fake_platform, planer_paths).describe_channel(channel).title == f"  {decomposed} "
    assert not planer_paths.channels_file.exists() and not planer_paths.channels_previous_file.exists()


def test_platform_failure_is_not_remembered(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    """Временный сбой площадки — не отказ: следующее обращение спрашивает канал снова."""
    fake_platform.fail_describe["yt_ua"] = PlatformError("backendError", "503")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    channel: ChannelConfig = make_config().channels[0]
    with pytest.raises(PlatformError):
        platform.describe_channel(channel)
    del fake_platform.fail_describe["yt_ua"]
    assert platform.describe_channel(channel).youtube_channel_id == "UCfakeyt_ua"


def test_refused_channel_fails_only_its_own_objects(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    """Инвариант 9: канал с чужим ником — ошибка его объектов в отчёте, второй канал работает."""
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("17-03-2027", "19:00", "ru")],
    )
    config: PlanerConfig = make_config()
    _answer(fake_platform, config.channels[0], handle_raw="@chuzhoy", title="Чужой канал")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    outcome: RunOutcome = run(RunMode.FULL, config, planer_paths, platform, FakeFormSender(), now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    assert outcome.report is not None
    errors = [item for item in outcome.report.outcomes if item.kind is OutcomeKind.ERROR]
    assert [(item.account_name, item.handle) for item in errors] == [("yt_ua", "@yt_ua")]
    assert errors[0].error is not None and errors[0].error.code == ERROR_CHANNEL_HANDLE_MISMATCH
    assert [call.channel_id for call in fake_platform.created] == ["yt_ru"]


def test_channels_without_objects_are_not_touched(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    """Слоты только на uk — канал ru не спрашивается."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    run(RunMode.DRY_RUN, make_config(), planer_paths, platform, FakeFormSender(), now, rng)
    assert fake_platform.describe_calls == ["yt_ua"]
    assert fake_platform.list_calls == ["yt_ua"]


def test_passport_keeps_first_verification_time(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    channel: ChannelConfig = make_config().channels[0]
    _seed_passport(planer_paths, fake_platform, channel, "UCfakeyt_ua")
    _verified(fake_platform, planer_paths).verify(channel)
    raw: dict[str, Any] = json.loads(planer_paths.channels_passport_file.read_text(encoding="utf-8"))
    [entry] = raw["channels"]
    assert (entry["first_verified_at"], entry["last_verified_at"]) == ("01-09-2026 10:00", "16-03-2027 12:00")
