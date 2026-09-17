"""Объект-канал: всё, что планер знает о канале за запуск, и правило его проверки (ТЗ §5.3).

Channel — один канал channels.json: конфиг, файл токена, запись паспорта, что прислал YouTube, статус и
готовая ошибка для отчёта. Правило «ник → id по паспорту → название» — метод Channel.check; выравнивание
файлов по его решению делает ChannelSync (app/platforms/channel_sync.py).

ChannelBook — все каналы запуска. Порядок запуска: проверка без браузера (check_without_login, статусы
READY / NEEDS_LOGIN / REFUSED / FAILED) → фаза входов (log_in_needed: каналы с объектами и статусом
NEEDS_LOGIN входят подряд) → работа с площадкой только по каналам READY (VerifiedPlatform). После фазы
входов браузер в этом запуске не открывается. Токен нового входа пишется только после подтверждения канала.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from app.config.loader import ChannelConfig, PlanerConfig
from app.core.text import UNICODE_FORM, handle_from_custom_url, normalize_handle
from app.observability.logging_setup import get_logger
from app.platforms.base import LOGIN_REQUIRED_CODE, BroadcastPlatform, ChannelInfo, PlatformError
from app.platforms.passport import PassportEntry
from app.ui import messages_ru as msg

if TYPE_CHECKING:   # ChannelSync импортирует этот модуль; здесь он нужен только для аннотаций
    from app.platforms.channel_sync import ChannelSync

LOGGER = get_logger("channel")

LOGIN_MAX_ATTEMPTS: Final[int] = 2   # входов в браузере на канал за запуск
LOG_MISSING: Final[str] = "-"
ERROR_CHANNEL_HANDLE_MISSING: Final[str] = "channelHandleMissing"
ERROR_CHANNEL_HANDLE_MISMATCH: Final[str] = "channelHandleMismatch"
ERROR_CHANNEL_ID_MISMATCH: Final[str] = "channelIdMismatch"
REFUSAL_TEMPLATES: Final[dict[str, str]] = {
    ERROR_CHANNEL_HANDLE_MISSING: msg.AUTH_CHANNEL_HANDLE_MISSING,
    ERROR_CHANNEL_HANDLE_MISMATCH: msg.AUTH_CHANNEL_HANDLE_MISMATCH,
    ERROR_CHANNEL_ID_MISMATCH: msg.AUTH_CHANNEL_ID_MISMATCH,
}
LOGIN_REQUIRED_REASON: Final[str] = "login_required"   # ключ AUTH_REASON_TEXT


def normalize_channel_title(title: str) -> str:
    """Название канала на YouTube в форме channels.json: NFC, края сняты."""
    return unicodedata.normalize(UNICODE_FORM, title).strip()


def youtube_handle_key(info: ChannelInfo) -> str | None:
    """Ключ ника, который прислал YouTube; ника нет — None."""
    return normalize_handle(handle_from_custom_url(info.handle_raw)) if info.handle_raw else None


def youtube_handle_text(info: ChannelInfo) -> str:
    """Ник, который прислал YouTube, — для людей; ника нет — «без ника»."""
    return handle_from_custom_url(info.handle_raw) if info.handle_raw else msg.AUTH_YOUTUBE_HANDLE_MISSING


class ChannelStatus(str, Enum):
    READY = "ready"              # канал подтверждён: к площадке можно
    NEEDS_LOGIN = "needs_login"  # токена нет, он отозван или убран как чужой
    REFUSED = "refused"          # вход был, но канал не тот — после всех попыток
    FAILED = "failed"            # сбой площадки или входа


class CheckVerdict(str, Enum):
    CONFIRMED = "confirmed"   # ник и id подтверждены, название совпало
    ALIGN = "align"           # тот же канал (по нику или по id), ник или название выровнять
    REFUSED = "refused"       # не тот канал: code — почему


@dataclass(frozen=True)
class ChannelCheck:
    verdict: CheckVerdict
    code: str | None = None


class ChannelBindingError(PlatformError):
    """Канал за токеном не тот: message — готовый текст для владельца."""


@dataclass
class Channel:
    """Изменяемый намеренно: заполняется по ходу запуска. config — значения channels.json этого запуска."""

    config: ChannelConfig
    token_file: Path
    passport_entry: PassportEntry | None = None
    info: ChannelInfo | None = None        # что прислал YouTube
    status: ChannelStatus = ChannelStatus.NEEDS_LOGIN
    error: PlatformError | None = None     # готовый текст для отчёта: REFUSED и FAILED
    login_attempts: int = 0

    @property
    def key(self) -> str:
        return self.config.key

    @property
    def can_try_login(self) -> bool:
        return self.login_attempts < LOGIN_MAX_ATTEMPTS

    def check(self, info: ChannelInfo) -> ChannelCheck:
        """Ник → id по паспорту → название. Название разное при совпавшем нике — выравнивание, не отказ."""
        handle_key: str | None = youtube_handle_key(info)
        if handle_key is None:
            return ChannelCheck(CheckVerdict.REFUSED, ERROR_CHANNEL_HANDLE_MISSING)
        is_same_id: bool = self.is_confirmed_by_passport(info)
        if handle_key != self.key:
            if is_same_id:
                return ChannelCheck(CheckVerdict.ALIGN)
            return ChannelCheck(CheckVerdict.REFUSED, ERROR_CHANNEL_HANDLE_MISMATCH)
        if self.passport_entry is not None and not is_same_id:
            return ChannelCheck(CheckVerdict.REFUSED, ERROR_CHANNEL_ID_MISMATCH)
        if normalize_channel_title(info.title) != self.config.account_name:
            return ChannelCheck(CheckVerdict.ALIGN)
        return ChannelCheck(CheckVerdict.CONFIRMED)

    def is_confirmed_by_passport(self, info: ChannelInfo) -> bool:
        """Паспорт по нику из channels.json знает этот же id YouTube."""
        return self.passport_entry is not None and self.passport_entry.youtube_channel_id == info.youtube_channel_id

    def refusal(self, info: ChannelInfo, code: str, channels_file: Path) -> ChannelBindingError:
        """Отказ с текстом для владельца: значение из channels.json и то, что прислал YouTube."""
        entry: PassportEntry | None = self.passport_entry
        return ChannelBindingError(
            code,
            REFUSAL_TEMPLATES[code].format(
                account_name=self.config.account_name,
                handle=self.config.handle,
                youtube_title=info.title,
                youtube_handle=youtube_handle_text(info),
                youtube_channel_id=info.youtube_channel_id,
                passport_channel_id=entry.youtube_channel_id if entry is not None else LOG_MISSING,
                channels_file=channels_file,
            ),
        )

    def access_error(self) -> PlatformError | None:
        """Почему к площадке по этому каналу нельзя; None — можно."""
        if self.status is ChannelStatus.READY:
            return None
        if self.error is not None:
            return self.error
        return PlatformError(LOGIN_REQUIRED_CODE, msg.AUTH_REASON_TEXT[LOGIN_REQUIRED_REASON])

    def mark_ready(self, info: ChannelInfo) -> None:
        self.status, self.info, self.error = ChannelStatus.READY, info, None

    def mark_needs_login(self) -> None:
        self.status, self.info, self.error = ChannelStatus.NEEDS_LOGIN, None, None

    def mark_refused(self, info: ChannelInfo, error: ChannelBindingError) -> None:
        self.status, self.info, self.error = ChannelStatus.REFUSED, info, error

    def mark_failed(self, error: PlatformError) -> None:
        self.status, self.error = ChannelStatus.FAILED, error

    def log_refused(self, info: ChannelInfo, code: str) -> None:
        LOGGER.error(
            'channel_refused code=%s channel="%s" handle=%s youtube_title="%s" handle_raw=%s '
            "youtube_channel_id=%s passport_channel_id=%s login_attempts=%d",
            code,
            self.config.account_name,
            self.config.handle,
            info.title,
            info.handle_raw or LOG_MISSING,
            info.youtube_channel_id,
            self.passport_entry.youtube_channel_id if self.passport_entry is not None else LOG_MISSING,
            self.login_attempts,
        )


class LoginConsole(Protocol):
    """Вход глазами владельца: печатает main.py, тексты — messages_ru."""

    def on_login(self, channel: ChannelConfig) -> None:
        """Ровно перед браузером: какой аккаунт и канал выбирать."""
        ...

    def on_wrong_channel(self, channel: ChannelConfig, info: ChannelInfo, will_retry: bool) -> None:
        """В браузере выбран не тот канал."""
        ...

    def on_login_failed(self, channel: ChannelConfig, error: PlatformError) -> None:
        """Вход не удался: браузер закрыт, сбой Google."""
        ...

    def on_channel_ready(self, channel: ChannelConfig, info: ChannelInfo) -> None:
        """Вход выполнен, канал подтверждён."""
        ...


class ChannelBook:
    """Все каналы запуска — один экземпляр; канал ищется по ChannelConfig.key."""

    def __init__(
        self,
        platform: BroadcastPlatform,
        sync: ChannelSync,
        console: LoginConsole | None = None,
    ) -> None:
        self._platform: BroadcastPlatform = platform
        self._sync: ChannelSync = sync
        self._console: LoginConsole | None = console
        self._channels: dict[str, Channel] = {}

    def check_without_login(self, config: PlanerConfig) -> tuple[PlanerConfig, list[str]]:
        """Проверка всех каналов без браузера: конфиг после выравнивания и предупреждения запуска."""
        synced, warnings = self._sync.run(config)
        self._channels = {channel.key: channel for channel in self._sync.take_channels()}
        return synced, warnings

    def channel(self, config: ChannelConfig) -> Channel:
        """Объект канала; без проверки при старте (--auth) — новый, со статусом NEEDS_LOGIN."""
        found: Channel | None = self._channels.get(config.key)
        if found is None:
            found = self._sync.new_channel(config)
            self._channels[config.key] = found
        return found

    def log_in_needed(self, channels: Sequence[ChannelConfig]) -> None:
        """Фаза входов: каждый канал из списка со статусом NEEDS_LOGIN входит, один за другим."""
        seen: set[str] = set()
        for config in channels:
            if config.key in seen:
                continue
            seen.add(config.key)
            if self.channel(config).status is ChannelStatus.NEEDS_LOGIN:
                self.log_in(config)
        LOGGER.info("login_phase_done channels=%d", len(seen))

    def log_in(self, config: ChannelConfig, *, force: bool = False) -> Channel:
        """Вход одного канала — одно место для запуска, --check и --auth; попыток — LOGIN_MAX_ATTEMPTS.

        force (--auth) — вход при любом статусе; прежний токен остаётся, пока новый вход не подтверждён.
        """
        channel: Channel = self.channel(config)
        if not force and channel.status is not ChannelStatus.NEEDS_LOGIN:
            return channel
        while channel.can_try_login and self._attempt(channel):
            pass
        return channel

    def take_warnings(self) -> list[str]:
        return self._sync.take_warnings()

    def _attempt(self, channel: Channel) -> bool:
        """Одна попытка входа; True — нужна ещё одна (в браузере выбран не тот канал)."""
        config: ChannelConfig = channel.config
        channel.login_attempts += 1
        self._platform.drop_login(config)
        LOGGER.info(
            'login_started channel="%s" handle=%s google_account="%s" attempt=%d/%d',
            config.account_name,
            config.handle,
            config.google_account,
            channel.login_attempts,
            LOGIN_MAX_ATTEMPTS,
        )
        if self._console is not None:
            self._console.on_login(config)
        try:
            info: ChannelInfo = self._platform.describe_channel(config, allow_login=True)
        except PlatformError as error:
            self._fail(channel, error)
            return False
        channel.passport_entry = self._sync.passport.find_by_key(channel.key)
        check: ChannelCheck = channel.check(info)
        if check.verdict is CheckVerdict.REFUSED and check.code is not None:
            return self._wrong_channel(channel, info, check.code)
        self._confirm(channel, info, check)
        return False

    def _fail(self, channel: Channel, error: PlatformError) -> None:
        LOGGER.error(
            'login_failed channel="%s" handle=%s code=%s message="%s"',
            channel.config.account_name,
            channel.config.handle,
            error.code,
            error.message,
        )
        self._platform.drop_login(channel.config)
        channel.mark_failed(error)
        if self._console is not None:
            self._console.on_login_failed(channel.config, error)

    def _wrong_channel(self, channel: Channel, info: ChannelInfo, code: str) -> bool:
        """Не тот канал: токен не пишется; попытки остались — ещё вход, иначе REFUSED."""
        self._platform.drop_login(channel.config)
        will_retry: bool = channel.can_try_login
        LOGGER.warning(
            'login_wrong_channel channel="%s" handle=%s youtube_title="%s" handle_raw=%s youtube_channel_id=%s '
            "code=%s attempt=%d/%d",
            channel.config.account_name,
            channel.config.handle,
            info.title,
            info.handle_raw or LOG_MISSING,
            info.youtube_channel_id,
            code,
            channel.login_attempts,
            LOGIN_MAX_ATTEMPTS,
        )
        if self._console is not None:
            self._console.on_wrong_channel(channel.config, info, will_retry)
        if will_retry:
            return True
        channel.log_refused(info, code)
        channel.mark_refused(info, channel.refusal(info, code, self._sync.paths.channels_file))
        return False

    def _confirm(self, channel: Channel, info: ChannelInfo, check: ChannelCheck) -> None:
        """Токен — до выравнивания: выравнивание ника переименовывает уже записанный файл."""
        config: ChannelConfig = channel.config
        self._keep_login(config)
        is_aligned: bool = check.verdict is CheckVerdict.ALIGN and self._sync.align_in_run(config, info)
        if not is_aligned:
            self._sync.confirm(config, info)
        channel.mark_ready(info)
        LOGGER.info(
            'login_confirmed channel="%s" handle=%s youtube_channel_id=%s verdict=%s',
            config.account_name,
            config.handle,
            info.youtube_channel_id,
            check.verdict.value,
        )
        if self._console is not None:
            self._console.on_channel_ready(config, info)

    def _keep_login(self, config: ChannelConfig) -> None:
        """Не записался файл — канал в этом запуске всё равно работает: учётные данные в памяти."""
        try:
            self._platform.keep_login(config)
        except PlatformError as error:
            LOGGER.warning(
                'token_save_failed channel="%s" handle=%s code=%s message="%s"',
                config.account_name,
                config.handle,
                error.code,
                error.message,
            )
            self._sync.add_warning(
                msg.WARNING_TOKEN_SAVE_FAILED.format(
                    account_name=config.account_name, handle=config.handle, error=error.message
                )
            )
