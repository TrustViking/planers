"""Сверка объектов с эфирами на площадке (ТЗ §7.3). Истина — на площадке, журнал — память.

Решения и найденные данные записываются в сами объекты; наружу отдаются только эфиры,
у которых есть маркер планера, но нет соответствующего слота (сироты, §12 п.4).
Маркер планера — slot_id в названии привязанного потока. Поток с другим названием
(ручной эфир) считается эфиром без маркера.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.config.loader import ChannelConfig
from app.core.dates import SLOT_TIME_FORMAT, build_slot_id, format_date, format_time, parse_date
from app.observability.logging_setup import get_logger
from app.pipeline.plan import BroadcastSpec, Decision, OutcomeError, PlannedBroadcast, to_minute
from app.platforms.base import BroadcastPlatform, PlatformError, StreamInfo, UpcomingBroadcast

LOGGER = get_logger("reconciler")
# Форма маркера планера — единственный источник.
PLANER_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{2}-\d{2}-\d{4}_\d{4}_[a-z]+$")
MARKER_SEPARATOR: Final[str] = "_"  # как в SLOT_ID_TEMPLATE (app/core/dates.py)


@dataclass(frozen=True)
class OrphanBroadcast:
    """Эфир с маркером планера, чьего слота нет среди будущих слотов (§12 п.4)."""

    channel: ChannelConfig
    broadcast: UpcomingBroadcast
    marker: str


@dataclass(frozen=True)
class MarkerParts:
    date: str
    time: str
    language: str


@dataclass(frozen=True)
class MarkedBroadcast:
    channel: ChannelConfig
    broadcast: UpcomingBroadcast
    stream: StreamInfo
    parts: MarkerParts


@dataclass(frozen=True)
class ChannelFailure:
    channel: ChannelConfig
    error: PlatformError


@dataclass(frozen=True)
class MarkedScan:
    broadcasts: tuple[MarkedBroadcast, ...]
    failures: tuple[ChannelFailure, ...]


def split_marker(marker: str) -> MarkerParts | None:
    """Маркер slot_id → дата, время, язык для отчёта; не маркер планера — None."""
    if not PLANER_MARKER_PATTERN.fullmatch(marker):
        return None
    date_part, time_part, language = marker.split(MARKER_SEPARATOR, 2)
    try:
        date_text: str = format_date(parse_date(date_part))
        time_text: str = format_time(datetime.strptime(time_part, SLOT_TIME_FORMAT).time())
    except ValueError:
        return None
    if build_slot_id(date_text, time_text, language) != marker:
        return None
    return MarkerParts(date=date_text, time=time_text, language=language)


def _platform_error(channel: ChannelConfig, error: PlatformError) -> OutcomeError:
    return OutcomeError(origin=channel.platform.value, code=error.code, message=error.message)


def _group_by_channel(
    planned: Sequence[PlannedBroadcast],
    channels: Sequence[ChannelConfig],
) -> list[tuple[ChannelConfig, list[PlannedBroadcast]]]:
    groups: dict[str, tuple[ChannelConfig, list[PlannedBroadcast]]] = {
        channel.id: (channel, []) for channel in channels
    }
    for item in planned:
        groups.setdefault(item.channel.id, (item.channel, []))[1].append(item)
    return list(groups.values())


class Reconciler:
    """list_upcoming — ровно раз на канал; get_stream кешируется по (channel.id, stream_id)."""

    def __init__(self, platform: BroadcastPlatform) -> None:
        self._platform: BroadcastPlatform = platform
        self._streams: dict[tuple[str, str], StreamInfo | None] = {}

    def reconcile(
        self,
        planned: Sequence[PlannedBroadcast],
        slot_ids: frozenset[str],
        channels: Sequence[ChannelConfig] = (),
    ) -> tuple[OrphanBroadcast, ...]:
        """Решения пишутся в объекты; наружу — только сироты (§12 п.4)."""
        orphans: list[OrphanBroadcast] = []
        for channel, items in _group_by_channel(planned, channels):
            orphans.extend(self._reconcile_channel(channel, items, slot_ids))
        return tuple(orphans)

    def marked_broadcasts(self, channels: Sequence[ChannelConfig]) -> MarkedScan:
        """Все эфиры с маркером планера на каналах (--status); сбой канала не валит остальные."""
        broadcasts: list[MarkedBroadcast] = []
        failures: list[ChannelFailure] = []
        for channel in channels:
            try:
                broadcasts.extend(self._marked_in_channel(channel, self._platform.list_upcoming(channel)))
            except PlatformError as error:
                LOGGER.warning("channel_unavailable channel=%s code=%s", channel.id, error.code)
                failures.append(ChannelFailure(channel=channel, error=error))
        broadcasts.sort(key=lambda item: (item.broadcast.start_utc, item.parts.language, item.channel.id))
        return MarkedScan(broadcasts=tuple(broadcasts), failures=tuple(failures))

    def _reconcile_channel(
        self,
        channel: ChannelConfig,
        items: list[PlannedBroadcast],
        slot_ids: frozenset[str],
    ) -> list[OrphanBroadcast]:
        try:
            broadcasts: list[UpcomingBroadcast] = self._platform.list_upcoming(channel)
        except PlatformError as error:
            LOGGER.warning("channel_unavailable channel=%s code=%s planned=%d", channel.id, error.code, len(items))
            for item in items:
                item.decision = Decision.ERROR
                item.error = _platform_error(channel, error)
            return []
        for item in items:
            self._decide(item, broadcasts)
            LOGGER.info(
                "pair_decision slot_id=%s channel=%s decision=%s broadcast_id=%s rebind=%s",
                item.slot_id,
                channel.id,
                item.decision.value,
                item.found.broadcast_id if item.found else "-",
                item.is_rebind,
            )
        return self._orphans(channel, broadcasts, slot_ids)

    def _decide(self, item: PlannedBroadcast, broadcasts: list[UpcomingBroadcast]) -> None:
        candidates: list[UpcomingBroadcast] = [
            broadcast for broadcast in broadcasts if to_minute(broadcast.start_utc) == item.expected.start_minute
        ]
        found, stream, is_ambiguous = self._pick_candidate(item.channel, item.slot_id, candidates)
        if is_ambiguous:
            item.decision = Decision.AMBIGUOUS
            return
        if found is None:
            item.decision = Decision.RECREATE if item.broadcast_id else Decision.CREATE
            return
        item.found = found
        item.found_stream = stream
        item.actual = BroadcastSpec.from_platform(found, stream, self._platform.limits)
        item.is_rebind = item.broadcast_id != found.broadcast_id
        if stream is None:
            item.decision = Decision.NO_STREAM
            return
        item.changed_fields = item.actual.diff(item.expected)
        item.decision = Decision.UPDATE if item.changed_fields else Decision.MATCH

    def _pick_candidate(
        self,
        channel: ChannelConfig,
        slot_id: str,
        candidates: list[UpcomingBroadcast],
    ) -> tuple[UpcomingBroadcast | None, StreamInfo | None, bool]:
        """(эфир, поток, неоднозначно): свой маркер → он; маркер другого слота — мимо; без маркера — только один."""
        unmarked: list[tuple[UpcomingBroadcast, StreamInfo | None]] = []
        for candidate in candidates:
            stream: StreamInfo | None = self._stream(channel, candidate.stream_id)
            marker: str | None = stream.title if stream is not None else None
            if marker == slot_id:
                return candidate, stream, False
            if marker is not None and PLANER_MARKER_PATTERN.fullmatch(marker):
                continue
            unmarked.append((candidate, stream))
        if len(unmarked) > 1:
            return None, None, True
        if unmarked:
            return unmarked[0][0], unmarked[0][1], False
        return None, None, False

    def _orphans(
        self,
        channel: ChannelConfig,
        broadcasts: list[UpcomingBroadcast],
        slot_ids: frozenset[str],
    ) -> list[OrphanBroadcast]:
        return [
            OrphanBroadcast(channel=channel, broadcast=item.broadcast, marker=item.stream.title)
            for item in self._marked_in_channel(channel, broadcasts)
            if item.stream.title not in slot_ids
        ]

    def _marked_in_channel(
        self,
        channel: ChannelConfig,
        broadcasts: list[UpcomingBroadcast],
    ) -> list[MarkedBroadcast]:
        marked: list[MarkedBroadcast] = []
        for broadcast in broadcasts:
            stream: StreamInfo | None = self._stream(channel, broadcast.stream_id)
            parts: MarkerParts | None = split_marker(stream.title) if stream is not None else None
            if stream is not None and parts is not None:
                marked.append(MarkedBroadcast(channel=channel, broadcast=broadcast, stream=stream, parts=parts))
        return marked

    def _stream(self, channel: ChannelConfig, stream_id: str | None) -> StreamInfo | None:
        if stream_id is None:
            return None
        cache_key: tuple[str, str] = (channel.id, stream_id)
        if cache_key not in self._streams:
            self._streams[cache_key] = self._platform.get_stream(channel, stream_id)
        return self._streams[cache_key]
