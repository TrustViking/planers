"""Сверка каналов при старте и выравнивание ника, названия и файла токена по id YouTube.

Id канала на YouTube не меняется никогда, а ник и название владелец может сменить. Если канал
подтверждается по id (паспорт каналов), планер сам переписывает ник и название в channels.json,
переименовывает файл токена и обновляет паспорт — повторный вход владельцу не нужен.

При старте (run) — по каждому каналу channels.json, без браузера (describe_channel(allow_login=False)):
  - токен по нику есть: ник на YouTube совпал — выровнять название (если в паспорте тот же ник не
    с другим id), иначе — выровнять ник, если паспорт по нику подтверждает тот же id;
  - токена по нику нет: токен ищется по записям паспорта, чьих ников нет в channels.json (ник поправили
    руками) — канал за таким токеном с ником из channels.json — тот же канал: токен переименовывается.
Канал без токена браузер не открывает: вход — при первом обращении к нему (VerifiedPlatform).
За старт channels.json переписывается один раз (прежний — в channels.previous.json), затем конфиг перечитывается.
VerifiedPlatform выравнивает тем же кодом (align_in_run), если расхождение нашлось уже по ходу запуска.
"""
from __future__ import annotations

import os
import unicodedata
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
from app.core.text import UNICODE_FORM, handle_from_custom_url, normalize_handle
from app.google.auth import token_file_for
from app.observability.logging_setup import get_logger
from app.paths import PlanerPaths
from app.platforms.base import BroadcastPlatform, ChannelInfo, PlatformError
from app.platforms.passport import ChannelPassport, PassportEntry
from app.ui import messages_ru as msg

LOGGER = get_logger("channel")

LOG_MISSING: Final[str] = "-"


def normalize_channel_title(title: str) -> str:
    """Название канала на YouTube в форме channels.json: NFC, края сняты."""
    return unicodedata.normalize(UNICODE_FORM, title).strip()


def youtube_handle_key(info: ChannelInfo) -> str | None:
    """Ключ ника, который прислал YouTube; ника нет — None."""
    return normalize_handle(handle_from_custom_url(info.handle_raw)) if info.handle_raw else None


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
    """Один экземпляр на запуск: общий паспорт для сверки при старте и для VerifiedPlatform."""

    def __init__(self, platform: BroadcastPlatform, paths: PlanerPaths, now_local: datetime) -> None:
        self._platform: BroadcastPlatform = platform
        self._paths: PlanerPaths = paths
        self._verified_at: str = format_datetime_text(now_local)
        self._warnings: list[str] = []
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

    def take_warnings(self) -> list[str]:
        """Предупреждения запуска, накопленные с прошлого вызова."""
        taken: list[str] = list(self._warnings)
        self._warnings.clear()
        return taken

    def run(self, config: PlanerConfig) -> tuple[PlanerConfig, list[str]]:
        """Сверка при старте: конфиг после выравнивания и предупреждения для консоли и отчёта."""
        known: frozenset[str] = frozenset(channel.key for channel in config.channels)
        claimed: set[str] = set()   # записи паспорта, чей токен уже отдан другому каналу
        alignments: list[_Alignment] = []
        for channel in config.channels:
            alignment: _Alignment | None = self._check_channel(channel, known, claimed)
            if alignment is not None:
                alignments.append(alignment)
        applied: list[_Alignment] = self._apply(alignments, in_run=False)
        self._save_passport()
        if applied:
            config = load_planer_config(self._paths.config_file, self._paths.channels_file)
        return config, self.take_warnings()

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

    def _check_channel(self, channel: ChannelConfig, known: frozenset[str], claimed: set[str]) -> _Alignment | None:
        token: Path = self.token_file(channel)
        if not token.is_file():
            return self._find_moved_token(channel, known, claimed)
        info: ChannelInfo | None = self._describe(channel)
        if info is None:
            return None
        return self._decide(channel, info, token)

    def _decide(self, channel: ChannelConfig, info: ChannelInfo, token: Path) -> _Alignment | None:
        """Ник совпал — выровнять название; ник другой — выровнять ник, только если паспорт подтверждает id."""
        entry: PassportEntry | None = self._passport.find_by_key(channel.key)
        handle_key: str | None = youtube_handle_key(info)
        is_same_id: bool = entry is not None and entry.youtube_channel_id == info.youtube_channel_id
        if handle_key == channel.key and (entry is None or is_same_id):
            if normalize_channel_title(info.title) == channel.account_name:
                self._record(channel, channel, info)
                return None
            return self._plan(channel, info, token)
        if handle_key is not None and handle_key != channel.key and is_same_id:
            return self._plan(channel, info, token)
        LOGGER.info(
            'channel_sync_left channel="%s" handle=%s handle_raw=%s youtube_channel_id=%s passport_channel_id=%s',
            channel.account_name,
            channel.handle,
            info.handle_raw or LOG_MISSING,
            info.youtube_channel_id,
            entry.youtube_channel_id if entry is not None else LOG_MISSING,
        )
        return None

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
            info: ChannelInfo | None = self._describe(_channel_from_entry(entry, channel))
            if info is not None and youtube_handle_key(info) == channel.key:
                claimed.add(entry.key)
                return self._plan(channel, info, source)
        return None

    def _describe(self, channel: ChannelConfig) -> ChannelInfo | None:
        """Без браузера; любой сбой — канал пропускается, его проверит VerifiedPlatform как обычно."""
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
