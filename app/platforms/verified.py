"""Площадка с проверкой привязки канала при первом обращении (ТЗ §5.3).

Обёртка над любой BroadcastPlatform. Первое обращение к каналу за запуск — describe_channel:
у YouTube это же и вход (токена нет — браузер), затем сверка с app\\state\\bindings.json.
Новый канал — привязка записывается. Токен ведёт не на тот канал или этот YouTube-канал
уже записан под другим именем — ChannelBindingError: это PlatformError, и сверка изолирует
его как любой сбой канала (все объекты канала — ошибка, остальные каналы работают).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from app.config.loader import ChannelConfig
from app.observability.logging_setup import get_logger
from app.platforms.base import (
    BroadcastFacts,
    BroadcastPlatform,
    ChannelInfo,
    CreatedBroadcast,
    PlatformError,
    PlatformLimits,
    StreamInfo,
    UpcomingBroadcast,
    VideoFixes,
)
from app.state.channels import BindingVerdict, ChannelBinding, ChannelBindings
from app.ui import messages_ru as msg

if TYPE_CHECKING:   # спека живёт в pipeline; здесь она нужна только для аннотаций
    from app.pipeline.plan import BroadcastSpec

LOGGER = get_logger("binding")

ERROR_BINDING_MISMATCH: Final[str] = "channelBindingMismatch"
ERROR_CHANNEL_TAKEN: Final[str] = "channelTaken"
ERROR_BINDINGS_WRITE: Final[str] = "bindingsWriteFailed"


class ChannelBindingError(PlatformError):
    """Канал за токеном не тот: message — готовый текст для владельца."""


class ChannelListener(Protocol):
    def on_channel_ready(self, channel: ChannelConfig, info: ChannelInfo, *, is_new_binding: bool) -> None:
        """Канал проверен и привязан; is_new_binding — привязка только что записана."""
        ...


def _local_now() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


class VerifiedPlatform:
    """Каждый метод площадки сначала проверяет канал; проверка — один раз на канал за запуск."""

    def __init__(
        self,
        platform: BroadcastPlatform,
        bindings: ChannelBindings,
        bindings_file: Path,
        listener: ChannelListener | None = None,
        clock: Callable[[], datetime] = _local_now,
    ) -> None:
        self._platform: BroadcastPlatform = platform
        self._bindings: ChannelBindings = bindings
        self._bindings_file: Path = bindings_file
        self._listener: ChannelListener | None = listener
        self._clock: Callable[[], datetime] = clock
        self._verified: dict[str, ChannelInfo] = {}
        self._refused: dict[str, ChannelBindingError] = {}

    @property
    def limits(self) -> PlatformLimits:
        return self._platform.limits

    def verify(self, channel: ChannelConfig) -> ChannelInfo:
        """Вход и привязка; сбой площадки не запоминается, отказ привязки — до конца запуска."""
        name: str = channel.account_name
        if name in self._verified:
            return self._verified[name]
        if name in self._refused:
            raise self._refused[name]
        info: ChannelInfo = self._platform.describe_channel(channel)
        verdict: BindingVerdict = self._bindings.verdict(name, info.youtube_channel_id)
        if verdict in (BindingVerdict.MISMATCH, BindingVerdict.TAKEN):
            error: ChannelBindingError = self._refusal(channel, info, verdict)
            self._refused[name] = error
            raise error
        if verdict is BindingVerdict.NEW:
            self._remember(channel, info)
        self._verified[name] = info
        if self._listener is not None:
            self._listener.on_channel_ready(channel, info, is_new_binding=verdict is BindingVerdict.NEW)
        return info

    def describe_channel(self, channel: ChannelConfig) -> ChannelInfo:
        return self.verify(channel)

    def list_upcoming(self, channel: ChannelConfig) -> list[UpcomingBroadcast]:
        self.verify(channel)
        return self._platform.list_upcoming(channel)

    def get_stream(self, channel: ChannelConfig, stream_id: str) -> StreamInfo | None:
        self.verify(channel)
        return self._platform.get_stream(channel, stream_id)

    def create_broadcast(self, channel: ChannelConfig, spec: BroadcastSpec) -> CreatedBroadcast:
        self.verify(channel)
        return self._platform.create_broadcast(channel, spec)

    def update_broadcast(self, channel: ChannelConfig, broadcast_id: str, spec: BroadcastSpec) -> None:
        self.verify(channel)
        self._platform.update_broadcast(channel, broadcast_id, spec)

    def attach_stream(self, channel: ChannelConfig, broadcast_id: str, spec: BroadcastSpec) -> CreatedBroadcast:
        self.verify(channel)
        return self._platform.attach_stream(channel, broadcast_id, spec)

    def apply_video_settings(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        language: str,
        category_id: str,
        privacy: str,
    ) -> VideoFixes:
        self.verify(channel)
        return self._platform.apply_video_settings(channel, broadcast_id, language, category_id, privacy)

    def set_stream_marker(self, channel: ChannelConfig, stream_id: str, marker: str) -> None:
        self.verify(channel)
        self._platform.set_stream_marker(channel, stream_id, marker)

    def set_thumbnail(self, channel: ChannelConfig, broadcast_id: str, preview: bytes) -> None:
        self.verify(channel)
        self._platform.set_thumbnail(channel, broadcast_id, preview)

    def read_facts(self, channel: ChannelConfig, broadcast_id: str) -> BroadcastFacts:
        self.verify(channel)
        return self._platform.read_facts(channel, broadcast_id)

    def _refusal(self, channel: ChannelConfig, info: ChannelInfo, verdict: BindingVerdict) -> ChannelBindingError:
        name: str = channel.account_name
        LOGGER.error(
            'binding_refused channel="%s" verdict=%s youtube_channel_id=%s',
            name,
            verdict.value,
            info.youtube_channel_id,
        )
        if verdict is BindingVerdict.MISMATCH:
            known: ChannelBinding | None = self._bindings.get(name)
            return ChannelBindingError(
                ERROR_BINDING_MISMATCH,
                msg.AUTH_BINDING_MISMATCH.format(
                    account_name=name,
                    expected_title=known.title if known else "",
                    expected_id=known.youtube_channel_id if known else "",
                    actual_title=info.title,
                    actual_id=info.youtube_channel_id,
                ),
            )
        other: ChannelBinding | None = self._bindings.find_by_youtube_channel_id(info.youtube_channel_id)
        return ChannelBindingError(
            ERROR_CHANNEL_TAKEN,
            msg.AUTH_BINDING_TAKEN.format(
                account_name=name,
                actual_title=info.title,
                actual_id=info.youtube_channel_id,
                other_account_name=other.account_name if other else "",
            ),
        )

    def _remember(self, channel: ChannelConfig, info: ChannelInfo) -> None:
        """Сначала файл, потом память: незаписанная привязка не должна считаться записанной."""
        binding: ChannelBinding = ChannelBinding(
            account_name=channel.account_name,
            youtube_channel_id=info.youtube_channel_id,
            title=info.title,
            authorized_at=self._clock(),
        )
        candidate: ChannelBindings = ChannelBindings(self._bindings.bindings)
        candidate.upsert(binding)
        try:
            candidate.save(self._bindings_file)
        except OSError as error:
            LOGGER.error(
                'bindings_write_failed channel="%s" path=%s reason=%s',
                channel.account_name,
                self._bindings_file,
                error,
            )
            raise PlatformError(ERROR_BINDINGS_WRITE, f"{self._bindings_file}: {error}") from error
        self._bindings.upsert(binding)
        LOGGER.info(
            'binding_saved channel="%s" youtube_channel_id=%s path=%s',
            channel.account_name,
            info.youtube_channel_id,
            self._bindings_file,
        )
