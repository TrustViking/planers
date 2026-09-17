"""Сверка каналов при старте и выравнивание ника, названия и файла токена по id YouTube.

Id канала на YouTube не меняется никогда, а ник и название владелец может сменить. Если канал
подтверждается по id (паспорт каналов), планер сам переписывает ник и название в channels.json,
переименовывает файл токена и обновляет паспорт — повторный вход владельцу не нужен.

При старте (run) — по каждому каналу channels.json, без браузера (describe_channel(allow_login=False));
что делать, решает Channel.check (app/platforms/channel.py), итог записывается в объект Channel:
  - токен по нику есть: подтверждён — READY; тот же канал с другим ником или названием — выровнять, READY;
    не тот канал и паспорт его id не подтверждает — токен чужой: файл удаляется, NEEDS_LOGIN;
    нужен вход (токен отозван) — NEEDS_LOGIN; сбой площадки — FAILED;
  - токена по нику нет: токен ищется по записям паспорта, чьих ников нет в channels.json (ник поправили
    руками) — канал за таким токеном с ником из channels.json — тот же канал: токен переименовывается, READY;
    иначе — NEEDS_LOGIN.
Браузер здесь не открывается: вход — в фазе входов (ChannelBook.log_in_needed).
За старт channels.json переписывается один раз (прежний — в channels.previous.json), затем конфиг перечитывается.
Вход в фазе входов выравнивает тем же кодом (align_in_run).
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Final

from app.config.loader import (
    ChannelConfig,
    ConfigError,
    PlanerConfig,
    account_name_problem,
    handle_problem,
    load_channels,
    load_planer_config,
    save_channels_file,
)
from app.core.dates import format_datetime_text
from app.core.text import handle_from_custom_url, normalize_handle
from app.google.auth import AuthError, drop_token, token_file_for
from app.observability.logging_setup import get_logger
from app.paths import PlanerPaths
from app.platforms.base import LOGIN_REQUIRED_CODE, BroadcastPlatform, ChannelInfo, PlatformError
from app.platforms.channel import (
    Channel,
    ChannelCheck,
    CheckVerdict,
    normalize_channel_title,
    youtube_handle_key,
    youtube_handle_text,
)
from app.platforms.passport import ChannelPassport, PassportEntry
from app.ui import messages_ru as msg

LOGGER = get_logger("channel")

LOG_MISSING: Final[str] = "-"
ERROR_TOKEN_DROP: Final[str] = "authFailed"   # как ERROR_AUTH у YouTube: файл токена не удалился


@dataclass(frozen=True)
class _Alignment:
    """Что выровнять у одного канала: значения до и после, откуда и куда переименовать токен."""

    before: ChannelConfig
    after: ChannelConfig
    info: ChannelInfo
    token_source: Path
    token_target: Path

    @property
    def is_token_moved(self) -> bool:
        return str(self.token_source) != str(self.token_target)


class ChannelSync:
    """Один экземпляр на запуск: общий паспорт для сверки при старте и для входов (ChannelBook)."""

    def __init__(self, platform: BroadcastPlatform, paths: PlanerPaths, now_local: datetime) -> None:
        self._platform: BroadcastPlatform = platform
        self._paths: PlanerPaths = paths
        self._verified_at: str = format_datetime_text(now_local)
        self._warnings: list[str] = []
        self._channels: list[Channel] = []   # объекты каналов после run; забирает take_channels
        self._passport: ChannelPassport
        self._passport, problem = ChannelPassport.load(paths.channels_passport_file)
        self._is_passport_dirty: bool = problem is not None   # не читался — перезаписать
        if problem is not None:
            self._warnings.append(msg.WARNING_PASSPORT_UNREADABLE.format(path=paths.channels_passport_file, error=problem))

    @property
    def passport(self) -> ChannelPassport:
        return self._passport

    @property
    def paths(self) -> PlanerPaths:
        return self._paths

    def token_file(self, channel: ChannelConfig) -> Path:
        return token_file_for(self._paths.secrets_dir, channel.handle)

    def new_channel(self, config: ChannelConfig) -> Channel:
        """Объект канала до проверки: файл токена и запись паспорта по нику."""
        return Channel(
            config=config,
            token_file=self.token_file(config),
            passport_entry=self._passport.find_by_key(config.key),
        )

    def take_channels(self) -> list[Channel]:
        taken: list[Channel] = list(self._channels)
        self._channels.clear()
        return taken

    def add_warning(self, text: str) -> None:
        self._warnings.append(text)

    def take_warnings(self) -> list[str]:
        """Предупреждения запуска, накопленные с прошлого вызова."""
        taken: list[str] = list(self._warnings)
        self._warnings.clear()
        return taken

    def run(self, config: PlanerConfig) -> tuple[PlanerConfig, list[str]]:
        """Сверка при старте: конфиг после выравнивания и предупреждения для консоли и отчёта.

        Объекты каналов (статус, что прислал YouTube) — take_channels, по значениям конфига после выравнивания.
        """
        known: frozenset[str] = frozenset(channel.key for channel in config.channels)
        claimed: set[str] = set()   # записи паспорта, чей токен уже отдан другому каналу
        checked: list[tuple[Channel, _Alignment | None]] = []
        for channel_config in config.channels:
            channel: Channel = self.new_channel(channel_config)
            checked.append((channel, self._check_channel(channel, known, claimed)))
        applied: list[_Alignment] = self._apply([item for _, item in checked if item is not None], in_run=False)
        for channel, alignment in checked:
            if alignment is not None and alignment in applied:
                channel.config, channel.token_file = alignment.after, alignment.token_target
        self._save_passport()
        if applied:
            config = load_planer_config(self._paths.config_file, self._paths.channels_file)
        self._channels = self._channels_for(config, [channel for channel, _ in checked])
        return config, self.take_warnings()

    def _channels_for(self, config: PlanerConfig, channels: Sequence[Channel]) -> list[Channel]:
        """Объекты в порядке channels.json; конфиг объекта — ровно тот, что прочитан после выравнивания."""
        by_key: dict[str, Channel] = {channel.key: channel for channel in channels}
        result: list[Channel] = []
        for channel_config in config.channels:
            channel: Channel = by_key.get(channel_config.key) or self.new_channel(channel_config)
            channel.config = channel_config
            channel.passport_entry = self._passport.find_by_key(channel_config.key)
            result.append(channel)
        return result

    def confirm(self, channel: ChannelConfig, info: ChannelInfo) -> None:
        """Канал проверен без выравнивания: запись паспорта создать или обновить и сразу сохранить."""
        self._record(channel, channel, info)
        self._save_passport()

    def align_in_run(self, channel: ChannelConfig, info: ChannelInfo) -> bool:
        """Выравнивание по ходу запуска (VerifiedPlatform). True — файлы и паспорт уже обновлены."""
        alignment: _Alignment | None = self._plan(channel, info, self.token_file(channel))
        if alignment is None:
            return False
        applied: list[_Alignment] = self._apply([alignment], in_run=True)
        self._save_passport()
        return bool(applied)

    def _check_channel(self, channel: Channel, known: frozenset[str], claimed: set[str]) -> _Alignment | None:
        """Статус канала — в объект; наружу — что выровнять."""
        if not channel.token_file.is_file():
            alignment: _Alignment | None = self._find_moved_token(channel.config, known, claimed)
            if alignment is not None:
                channel.mark_ready(alignment.info)
            return alignment
        info: ChannelInfo | None = self._describe(channel)
        if info is None:
            return None
        return self._decide(channel, info)

    def _decide(self, channel: Channel, info: ChannelInfo) -> _Alignment | None:
        """Решение — Channel.check: подтверждён — паспорт; тот же канал — выровнять; не тот — токен чужой."""
        check: ChannelCheck = channel.check(info)
        if check.verdict is CheckVerdict.REFUSED and check.code is not None:
            self._refuse(channel, info, check.code)
            return None
        channel.mark_ready(info)
        if check.verdict is CheckVerdict.ALIGN:
            alignment: _Alignment | None = self._plan(channel.config, info, channel.token_file)
            if alignment is not None:
                return alignment
        self._record(channel.config, channel.config, info)
        return None

    def _refuse(self, channel: Channel, info: ChannelInfo, code: str) -> None:
        """Паспорт подтверждает id (у канала пропал ник) — отказ до конца запуска; иначе токен чужой."""
        if not channel.is_confirmed_by_passport(info):
            self._reject_token(channel, info, code)
            return
        channel.log_refused(info, code)
        channel.mark_refused(info, channel.refusal(info, code, self._paths.channels_file))

    def _reject_token(self, channel: Channel, info: ChannelInfo, code: str) -> None:
        """Токен ведёт не на тот канал: файл удаляется, канал входит заново в фазе входов."""
        config: ChannelConfig = channel.config
        LOGGER.warning(
            'token_rejected channel="%s" handle=%s youtube_title="%s" handle_raw=%s youtube_channel_id=%s code=%s',
            config.account_name,
            config.handle,
            info.title,
            info.handle_raw or LOG_MISSING,
            info.youtube_channel_id,
            code,
        )
        self._platform.drop_login(config)
        try:
            drop_token(channel.token_file)
        except AuthError as error:
            channel.mark_failed(PlatformError(ERROR_TOKEN_DROP, f"{error.reason.value}: {error.detail}"))
            return
        channel.mark_needs_login()
        self._warnings.append(
            msg.WARNING_TOKEN_REJECTED.format(
                account_name=config.account_name,
                handle=config.handle,
                youtube_title=info.title,
                youtube_handle=youtube_handle_text(info),
                youtube_channel_id=info.youtube_channel_id,
            )
        )

    def _find_moved_token(
        self,
        channel: ChannelConfig,
        known: frozenset[str],
        claimed: set[str],
    ) -> _Alignment | None:
        """Ник поправили руками: токен лежит под прежним ником из паспорта, которого в channels.json нет."""
        for entry in self._passport.entries:
            source: Path = self._paths.secrets_dir / entry.token_file
            if entry.key in known or entry.key in claimed or not source.is_file():
                continue
            info: ChannelInfo | None = self._describe_moved(_channel_from_entry(entry, channel))
            if info is not None and youtube_handle_key(info) == channel.key:
                claimed.add(entry.key)
                return self._plan(channel, info, source)
        return None

    def _describe(self, channel: Channel) -> ChannelInfo | None:
        """Без браузера: нужен вход — NEEDS_LOGIN, другой сбой — FAILED; None — дальше проверять нечего."""
        config: ChannelConfig = channel.config
        try:
            return self._platform.describe_channel(config, allow_login=False)
        except PlatformError as error:
            LOGGER.info(
                'channel_sync_skipped channel="%s" handle=%s code=%s', config.account_name, config.handle, error.code
            )
            if error.code == LOGIN_REQUIRED_CODE:
                channel.mark_needs_login()
            else:
                channel.mark_failed(error)
            return None

    def _describe_moved(self, channel: ChannelConfig) -> ChannelInfo | None:
        """Канал за токеном под прежним ником; любой сбой — токен не опознан."""
        try:
            return self._platform.describe_channel(channel, allow_login=False)
        except PlatformError as error:
            LOGGER.info(
                'channel_sync_skipped channel="%s" handle=%s code=%s', channel.account_name, channel.handle, error.code
            )
            return None

    def _plan(self, before: ChannelConfig, info: ChannelInfo, token_source: Path) -> _Alignment | None:
        """Значения после выравнивания; ник или название, которые не прошли бы проверку конфига, — не пишутся."""
        youtube_handle: str = handle_from_custom_url(info.handle_raw or "")
        handle: str = before.handle if normalize_handle(youtube_handle) == before.key else youtube_handle
        title: str = normalize_channel_title(info.title)
        problem: str | None = handle_problem(handle) or (account_name_problem(title) if title else None)
        if problem is not None or not title:
            LOGGER.warning(
                'channel_align_skipped channel="%s" handle=%s youtube_channel_id=%s problem="%s"',
                before.account_name,
                before.handle,
                info.youtube_channel_id,
                problem or "empty title",
            )
            self._warnings.append(self._failure_text(before, info, problem or LOG_MISSING))
            return None
        after: ChannelConfig = replace(before, handle=handle, account_name=title)
        return _Alignment(before, after, info, token_source, token_file_for(self._paths.secrets_dir, handle))

    def _apply(self, alignments: Sequence[_Alignment], *, in_run: bool) -> list[_Alignment]:
        """Токены → channels.json (один раз) → паспорт. Не записался channels.json — токены возвращаются."""
        renamed: list[_Alignment] = [item for item in alignments if self._rename_token(item)]
        if not renamed:
            return []
        problem: str | None = self._write_channels(renamed)
        if problem is not None:
            for item in renamed:
                self._restore_token(item)
                self._warnings.append(self._failure_text(item.before, item.info, problem))
            return []
        for item in renamed:
            self._finish(item, in_run=in_run)
        return renamed

    def _rename_token(self, item: _Alignment) -> bool:
        """Без перезаписи: целевой файл — другой и уже есть — файлы не трогаются."""
        if not item.is_token_moved:
            return True
        target: Path = item.token_target
        if target.exists() and not os.path.samefile(item.token_source, target):
            LOGGER.warning(
                'token_rename_skipped channel="%s" handle=%s source=%s target=%s youtube_channel_id=%s',
                item.before.account_name,
                item.before.handle,
                item.token_source.name,
                target.name,
                item.info.youtube_channel_id,
            )
            self._warnings.append(
                msg.WARNING_TOKEN_RENAME_SKIPPED.format(
                    account_name=item.before.account_name,
                    handle=item.before.handle,
                    youtube_channel_id=item.info.youtube_channel_id,
                    handle_after=item.after.handle,
                    target=target,
                )
            )
            return False
        try:
            os.rename(item.token_source, target)   # одинаковые без учёта регистра имена — тот же файл
        except OSError as error:
            self._warnings.append(self._failure_text(item.before, item.info, str(error)))
            return False
        return True

    def _restore_token(self, item: _Alignment) -> None:
        if not item.is_token_moved:
            return
        try:
            os.rename(item.token_target, item.token_source)
        except OSError as error:
            LOGGER.error("token_restore_failed source=%s target=%s error=%s", item.token_target, item.token_source, error)

    def _write_channels(self, alignments: Sequence[_Alignment]) -> str | None:
        """Текущий channels.json с заменой выровненных каналов; None — записан, иначе текст ошибки."""
        replacements: dict[str, ChannelConfig] = {item.before.key: item.after for item in alignments}
        try:
            current: tuple[ChannelConfig, ...] = load_channels(self._paths.channels_file)
            save_channels_file(
                self._paths.channels_file,
                self._paths.channels_previous_file,
                [replacements.get(channel.key, channel) for channel in current],
            )
        except (ConfigError, OSError) as error:
            LOGGER.warning("channels_write_failed path=%s error=%s", self._paths.channels_file, error)
            return str(error)
        return None

    def _finish(self, item: _Alignment, *, in_run: bool) -> None:
        LOGGER.info(
            'channel_aligned handle_before=%s handle_after=%s title_before="%s" title_after="%s" '
            "youtube_channel_id=%s token_before=%s token_after=%s",
            item.before.handle,
            item.after.handle,
            item.before.account_name,
            item.after.account_name,
            item.info.youtube_channel_id,
            item.token_source.name,
            item.token_target.name,
        )
        text: str = msg.WARNING_CHANNEL_ALIGNED.format(
            youtube_channel_id=item.info.youtube_channel_id,
            title_before=item.before.account_name,
            handle_before=item.before.handle,
            title_after=item.after.account_name,
            handle_after=item.after.handle,
        )
        self._warnings.append(text + msg.WARNING_CHANNEL_ALIGNED_IN_RUN if in_run else text)
        self._record(item.before, item.after, item.info)

    def _record(self, before: ChannelConfig, after: ChannelConfig, info: ChannelInfo) -> None:
        self._passport.record_verified(
            before,
            after,
            info,
            token_file=token_file_for(self._paths.secrets_dir, after.handle).name,
            verified_at=self._verified_at,
        )
        self._is_passport_dirty = True

    def _save_passport(self) -> None:
        if not self._is_passport_dirty:
            return
        problem: str | None = self._passport.save()
        if problem is not None:
            self._warnings.append(
                msg.WARNING_PASSPORT_WRITE_FAILED.format(path=self._passport.path, error=problem)
            )
            return
        self._is_passport_dirty = False

    @staticmethod
    def _failure_text(channel: ChannelConfig, info: ChannelInfo, reason: str) -> str:
        return msg.WARNING_CHANNEL_ALIGN_FAILED.format(
            account_name=channel.account_name,
            handle=channel.handle,
            youtube_channel_id=info.youtube_channel_id,
            reason=reason,
        )


def _channel_from_entry(entry: PassportEntry, channel: ChannelConfig) -> ChannelConfig:
    """Временный канал по записи паспорта: ник, название и почта — из записи, остальное — от текущего канала."""
    return replace(
        channel,
        account_name=entry.account_name,
        handle=entry.handle,
        google_account=entry.google_account,
    )
