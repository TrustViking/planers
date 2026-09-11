"""Сверка пар (слот, канал) с эфирами на площадке (ТЗ §7.3). Истина — на площадке, журнал — память.

Маркер планера — slot_id в названии привязанного потока. Поток с другим названием
(ручной эфир) считается эфиром без маркера.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Final

from app.config.loader import ChannelConfig
from app.core.dates import SLOT_TIME_FORMAT, build_slot_id, format_date, format_time, parse_date
from app.observability.logging_setup import get_logger
from app.package.model import Slot
from app.pipeline.selection import SlotChannelPair
from app.platforms.base import BroadcastPlatform, PlatformError, StreamInfo, UpcomingBroadcast
from app.state.registry import Registration, Registry

LOGGER = get_logger("reconciler")
# Форма маркера планера — единственный источник.
PLANER_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{2}-\d{2}-\d{4}_\d{4}_[a-z]+$")
MARKER_SEPARATOR: Final[str] = "_"  # как в SLOT_ID_TEMPLATE (app/core/dates.py)


class Decision(str, Enum):
    CREATE = "create"          # эфира нет
    MATCH = "match"            # есть, тексты совпадают
    UPDATE = "update"          # есть, название или описание отличаются
    RECREATE = "recreate"      # журнал помнит эфир, на площадке его нет — владелец удалил
    AMBIGUOUS = "ambiguous"    # несколько эфиров без маркера на эту минуту
    ERROR = "error"            # площадка не ответила по каналу


class ChangedField(str, Enum):
    TITLE = "title"
    DESCRIPTION = "description"


@dataclass(frozen=True)
class ReconciledPair:
    pair: SlotChannelPair
    decision: Decision
    broadcast: UpcomingBroadcast | None = None
    stream: StreamInfo | None = None
    rebind: bool = False                              # журнал надо переписать на найденный эфир
    changed_fields: tuple[ChangedField, ...] = ()
    error: PlatformError | None = None


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
class Reconciliation:
    pairs: tuple[ReconciledPair, ...]   # в порядке входных пар
    orphans: tuple[OrphanBroadcast, ...]


@dataclass(frozen=True)
class MarkedScan:
    broadcasts: tuple[MarkedBroadcast, ...]
    failures: tuple[ChannelFailure, ...]


def normalize_description(text: str) -> str:
    """CRLF→LF, пробелы в конце строк и по краям текста не различаются.

    Правило уточняется на этапе 3 после [ПРОВЕРИТЬ] §7.3 (как YouTube хранит описание).
    """
    lines: list[str] = text.replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


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


def _minute(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _changed_fields(broadcast: UpcomingBroadcast, slot: Slot) -> tuple[ChangedField, ...]:
    changed: list[ChangedField] = []
    if broadcast.title.strip() != slot.title.strip():
        changed.append(ChangedField.TITLE)
    if normalize_description(broadcast.description) != normalize_description(slot.description):
        changed.append(ChangedField.DESCRIPTION)
    return tuple(changed)


def _group_by_channel(
    pairs: Sequence[SlotChannelPair],
    channels: Sequence[ChannelConfig],
) -> list[tuple[ChannelConfig, list[tuple[int, SlotChannelPair]]]]:
    groups: dict[str, tuple[ChannelConfig, list[tuple[int, SlotChannelPair]]]] = {
        channel.id: (channel, []) for channel in channels
    }
    for index, pair in enumerate(pairs):
        groups.setdefault(pair.channel.id, (pair.channel, []))[1].append((index, pair))
    return list(groups.values())


class Reconciler:
    """list_upcoming — ровно раз на канал; get_stream кешируется по (channel.id, stream_id)."""

    def __init__(self, platform: BroadcastPlatform) -> None:
        self._platform: BroadcastPlatform = platform
        self._streams: dict[tuple[str, str], StreamInfo | None] = {}

    def reconcile(
        self,
        pairs: Sequence[SlotChannelPair],
        registry: Registry,
        slot_ids: frozenset[str],
        channels: Sequence[ChannelConfig] = (),
    ) -> Reconciliation:
        """slot_ids — все будущие слоты карты; channels — ещё и каналы без пар (поиск потерянных)."""
        decided: dict[int, ReconciledPair] = {}
        orphans: list[OrphanBroadcast] = []
        for channel, indexed_pairs in _group_by_channel(pairs, channels):
            channel_pairs: list[SlotChannelPair] = [pair for _, pair in indexed_pairs]
            results, channel_orphans = self._reconcile_channel(channel, channel_pairs, registry, slot_ids)
            decided.update(zip((index for index, _ in indexed_pairs), results))
            orphans.extend(channel_orphans)
        return Reconciliation(pairs=tuple(decided[index] for index in range(len(pairs))), orphans=tuple(orphans))

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
        pairs: list[SlotChannelPair],
        registry: Registry,
        slot_ids: frozenset[str],
    ) -> tuple[list[ReconciledPair], list[OrphanBroadcast]]:
        try:
            broadcasts: list[UpcomingBroadcast] = self._platform.list_upcoming(channel)
            results: list[ReconciledPair] = [self._decide(channel, pair, broadcasts, registry) for pair in pairs]
            orphans: list[OrphanBroadcast] = self._orphans(channel, broadcasts, slot_ids)
        except PlatformError as error:
            LOGGER.warning("channel_unavailable channel=%s code=%s pairs=%d", channel.id, error.code, len(pairs))
            return [ReconciledPair(pair=pair, decision=Decision.ERROR, error=error) for pair in pairs], []
        for result in results:
            LOGGER.info(
                "pair_decision slot_id=%s channel=%s decision=%s broadcast_id=%s rebind=%s",
                result.pair.slot.slot_id,
                channel.id,
                result.decision.value,
                result.broadcast.broadcast_id if result.broadcast else "-",
                result.rebind,
            )
        return results, orphans

    def _decide(
        self,
        channel: ChannelConfig,
        pair: SlotChannelPair,
        broadcasts: list[UpcomingBroadcast],
        registry: Registry,
    ) -> ReconciledPair:
        slot: Slot = pair.slot
        candidates: list[UpcomingBroadcast] = [
            broadcast for broadcast in broadcasts if _minute(broadcast.start_utc) == _minute(slot.start)
        ]
        found, stream, is_ambiguous = self._pick_candidate(channel, slot.slot_id, candidates)
        if is_ambiguous:
            return ReconciledPair(pair=pair, decision=Decision.AMBIGUOUS)
        registration: Registration | None = registry.get(Registry.key(slot.slot_id, channel.id))
        if found is None:
            is_deleted: bool = registration is not None and bool(registration.broadcast_id)
            return ReconciledPair(pair=pair, decision=Decision.RECREATE if is_deleted else Decision.CREATE)
        changed: tuple[ChangedField, ...] = _changed_fields(found, slot)
        return ReconciledPair(
            pair=pair,
            decision=Decision.UPDATE if changed else Decision.MATCH,
            broadcast=found,
            stream=stream,
            rebind=registration is None or registration.broadcast_id != found.broadcast_id,
            changed_fields=changed,
        )

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
