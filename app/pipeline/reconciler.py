"""Сверка объектов с эфирами на площадке (ТЗ §7.3). Истина — только на площадке.

Журнал сверка не видит: решение и ключ найденного эфира — только по ответу площадки.
Объект too_late сверяется только на чтение: опознание и ключ, решение остаётся TOO_LATE.
Не допущенный объект (PlannedBroadcast.admit): канал не READY — к площадке по этому каналу не обращаемся
вовсе, объекты остаются NOT_ADMITTED; канал READY, не допущен по форме — только опознание и ключ, как too_late.
Решения и найденные данные записываются в сами объекты; наружу отдаются только эфиры,
у которых есть маркер планера, но нет соответствующего слота (сироты, §12 п.4).
Маркер планера — slot_id в названии привязанного потока. Поток с другим названием
(ручной эфир) считается эфиром без маркера; найденный такой эфир планер усыновляет —
расхождение по MARKER исправимо, как и прочие поля FIXABLE_FIELDS.
Обложка сверяется по заглушкам канала (_channel_placeholders): картинка эфира совпала с заглушкой —
своей обложки нет, это расхождение по THUMBNAIL. Память планера главнее картинки: обложку этому же эфиру
ставил планер (PlannedBroadcast.apply_recorded_thumbnail) — обложка своя, картинка просто ещё не обновилась.
Известные слоты (slot_ids) — будущие и прошедшие: эфир прошедшего, ещё не начавшегося слота — не сирота.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.config.loader import ChannelConfig
from app.core.dates import SLOT_TIME_FORMAT, build_slot_id, format_date, format_time, parse_date
from app.observability.logging_setup import get_logger, mask_stream_key
from app.output.progress import NoProgress, RunProgress
from app.pipeline.plan import (
    WARNING_STEP_AMBIGUOUS,
    WARNING_STEP_REPORTED_FIELD,
    BroadcastSpec,
    ChangedField,
    Decision,
    OutcomeError,
    OutcomeWarning,
    PlannedBroadcast,
    to_minute,
)
from app.platforms.channel import Channel, ChannelStatus
from app.platforms.base import (
    BroadcastPlatform,
    PlatformError,
    StreamInfo,
    UpcomingBroadcast,
    broadcast_url_for,
    placeholder_sha_from_description,
)

LOGGER = get_logger("reconciler")
# Форма маркера планера — единственный источник.
PLANER_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{2}-\d{2}-\d{4}_\d{4}_[a-z]+$")
MARKER_SEPARATOR: Final[str] = "_"  # как в SLOT_ID_TEMPLATE (app/core/dates.py)
# Картинка, одинаковая у стольких эфиров одного канала, — заглушка канала, а не своя обложка.
PLACEHOLDER_DUPLICATE_MIN: Final[int] = 2
LOG_LIST_JOINER: Final[str] = ","


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


@dataclass(frozen=True)
class CandidatePick:
    """Итог опознания на минуту старта: найденный эфир или все неразличимые кандидаты."""

    broadcast: UpcomingBroadcast | None = None
    stream: StreamInfo | None = None
    ambiguous: tuple[UpcomingBroadcast, ...] = ()   # два и более эфира без маркера

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.ambiguous)


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
        channel.key: (channel, []) for channel in channels
    }
    for item in planned:
        groups.setdefault(item.channel.key, (item.channel, []))[1].append(item)
    return list(groups.values())


def _first_channel_object(items: Sequence[PlannedBroadcast]) -> Channel | None:
    """У всех объектов одного канала — один объект канала."""
    return next((item.channel_object for item in items if item.channel_object is not None), None)


def _is_channel_not_ready(items: Sequence[PlannedBroadcast]) -> bool:
    channel_object: Channel | None = _first_channel_object(items)
    return channel_object is not None and channel_object.status is not ChannelStatus.READY


def _channel_status(items: Sequence[PlannedBroadcast]) -> str:
    channel_object: Channel | None = _first_channel_object(items)
    return channel_object.status.value if channel_object is not None else "-"


class Reconciler:
    """list_upcoming — ровно раз на канал; get_stream кешируется по (channel.key, stream_id).

    Чтение каждого канала видно владельцу строками прогресса: до list_upcoming и после успешного ответа.
    """

    def __init__(self, platform: BroadcastPlatform, *, progress: RunProgress = NoProgress()) -> None:
        self._platform: BroadcastPlatform = platform
        self._progress: RunProgress = progress
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
                broadcasts.extend(self._marked_in_channel(channel, self._list_upcoming(channel)))
            except PlatformError as error:
                LOGGER.warning('channel_unavailable channel="%s" handle=%s code=%s', channel.account_name, channel.handle, error.code)
                failures.append(ChannelFailure(channel=channel, error=error))
        broadcasts.sort(key=lambda item: (item.broadcast.start_utc, item.parts.language, item.channel.key))
        return MarkedScan(broadcasts=tuple(broadcasts), failures=tuple(failures))

    def _list_upcoming(self, channel: ChannelConfig) -> list[UpcomingBroadcast]:
        """Строка «запрашиваю» — до обращения; вход и проверка канала прошли раньше, в фазе входов."""
        self._progress.channel_read_started(channel)
        broadcasts: list[UpcomingBroadcast] = self._platform.list_upcoming(channel)
        self._progress.channel_read_done(channel, len(broadcasts))
        return broadcasts

    def _reconcile_channel(
        self,
        channel: ChannelConfig,
        items: list[PlannedBroadcast],
        slot_ids: frozenset[str],
    ) -> list[OrphanBroadcast]:
        if _is_channel_not_ready(items):
            LOGGER.info(
                'channel_skipped_not_ready channel="%s" handle=%s status=%s planned=%d',
                channel.account_name,
                channel.handle,
                _channel_status(items),
                len(items),
            )
            return []
        try:
            broadcasts: list[UpcomingBroadcast] = self._list_upcoming(channel)
        except PlatformError as error:
            LOGGER.warning('channel_unavailable channel="%s" handle=%s code=%s planned=%d', channel.account_name, channel.handle, error.code, len(items))
            for item in items:
                if item.is_too_late or not item.is_admitted:
                    continue      # решения нет: ключа просто не будет; не допущенный остаётся NOT_ADMITTED
                item.decision = Decision.ERROR
                item.error = _platform_error(channel, error)
            return []
        placeholders: frozenset[str] = self._channel_placeholders(channel, broadcasts)
        for item in items:
            if item.is_too_late or not item.is_admitted:
                self._read_key_only(item, broadcasts)
            else:
                self._decide(item, broadcasts, placeholders)
            LOGGER.info(
                'pair_decision slot_id=%s channel="%s" handle=%s decision=%s broadcast_id=%s stream_key=%s fixable=%s reported=%s',
                item.slot_id,
                channel.account_name,
                channel.handle,
                item.decision.value,
                item.found.broadcast_id if item.found else "-",
                mask_stream_key(item.stream_key),
                ",".join(name.value for name in item.changed_fields) or "-",
                ",".join(name.value for name in item.reported_fields) or "-",
            )
        return self._orphans(channel, broadcasts, slot_ids)

    def _channel_placeholders(self, channel: ChannelConfig, broadcasts: list[UpcomingBroadcast]) -> frozenset[str]:
        """Заглушки обложки канала: отпечатки из описаний потоков и картинки, повторённые у нескольких эфиров.

        Потоки читаются тем же кешем, что и опознание и сироты: лишних обращений нет. Одинаковая картинка
        на разных каналах дублем не считается — счёт идёт внутри канала.
        """
        from_streams: set[str] = set()
        for broadcast in broadcasts:
            stream: StreamInfo | None = self._stream(channel, broadcast.stream_id)
            sha: str | None = placeholder_sha_from_description(stream.description) if stream is not None else None
            if sha is not None:
                from_streams.add(sha)
        counts: Counter[str] = Counter(
            broadcast.thumbnail_sha for broadcast in broadcasts if broadcast.thumbnail_sha is not None
        )
        from_duplicates: set[str] = {sha for sha, count in counts.items() if count >= PLACEHOLDER_DUPLICATE_MIN}
        LOGGER.info(
            'channel_placeholders channel="%s" handle=%s from_streams=%s from_duplicates=%s',
            channel.account_name,
            channel.handle,
            LOG_LIST_JOINER.join(sorted(from_streams)) or "-",
            LOG_LIST_JOINER.join(sorted(from_duplicates)) or "-",
        )
        return frozenset(from_streams | from_duplicates)

    def _decide(
        self,
        item: PlannedBroadcast,
        broadcasts: list[UpcomingBroadcast],
        placeholders: frozenset[str] = frozenset(),
    ) -> None:
        """Эфира на площадке нет — CREATE: памяти о прошлых запусках у планера нет, действие одно и то же."""
        pick: CandidatePick = self._find(item, broadcasts)
        if pick.is_ambiguous:
            item.decision = Decision.AMBIGUOUS
            self._warn_ambiguous(item, pick.ambiguous)
            return
        if pick.broadcast is None:
            item.decision = Decision.CREATE
            return
        item.found = pick.broadcast
        item.found_stream = pick.stream
        item.actual = BroadcastSpec.from_platform(pick.broadcast, pick.stream, self._platform.limits, placeholders)
        if item.apply_recorded_thumbnail():
            self._log_thumbnail_from_memory(item)
        changed: tuple[ChangedField, ...] = item.actual.diff(item.expected)
        self._log_not_compared(item)
        if pick.stream is None:
            # поток будет создан уже с меткой планера: чинить метку нечему
            changed = tuple(name for name in changed if name is not ChangedField.MARKER)
        item.split_changed(changed)
        self._warn_reported(item)
        if pick.stream is None:
            item.decision = Decision.NO_STREAM
            return
        item.take_found_key()
        item.decision = Decision.UPDATE if item.changed_fields else Decision.MATCH

    def _read_key_only(self, item: PlannedBroadcast, broadcasts: list[UpcomingBroadcast]) -> None:
        """too_late и не допущенный: только опознать эфир и взять ключ; решение прежнее, AMBIGUOUS не ставится."""
        pick: CandidatePick = self._find(item, broadcasts)
        if pick.is_ambiguous or pick.broadcast is None or pick.stream is None:
            return
        item.found = pick.broadcast
        item.found_stream = pick.stream
        item.take_found_key()

    @staticmethod
    def _log_thumbnail_from_memory(item: PlannedBroadcast) -> None:
        set_at: str | None = item.record.results.thumbnail_set_at if item.record is not None else None
        LOGGER.info(
            'thumbnail_from_memory slot_id=%s channel="%s" handle=%s broadcast_id=%s set_at="%s"',
            item.slot_id,
            item.channel.account_name,
            item.channel.handle,
            item.found.broadcast_id if item.found is not None else "-",
            set_at or "-",
        )

    @staticmethod
    def _log_not_compared(item: PlannedBroadcast) -> None:
        """Диагностика, а не дело владельца: площадка не вернула поле — сверки по нему в этом запуске не было."""
        if item.actual is None:
            return
        skipped: tuple[ChangedField, ...] = item.actual.not_compared(item.expected)
        if not skipped:
            return
        LOGGER.info(
            'spec_fields_not_compared slot_id=%s channel="%s" handle=%s fields=%s',
            item.slot_id,
            item.channel.account_name,
            item.channel.handle,
            ",".join(name.value for name in skipped),
        )

    def _warn_reported(self, item: PlannedBroadcast) -> None:
        """Расходится, но через API не исправляется: лог и предупреждение объекта, решение не меняется."""
        if item.actual is None:
            return
        for name in item.reported_fields:
            LOGGER.warning(
                'broadcast_setting_not_fixable slot_id=%s channel="%s" handle=%s field=%s wanted=%s actual=%s',
                item.slot_id,
                item.channel.account_name,
                item.channel.handle,
                name.value,
                item.expected.value(name),
                item.actual.value(name),
            )
            item.warn(OutcomeWarning(WARNING_STEP_REPORTED_FIELD, name.value))

    def _warn_ambiguous(self, item: PlannedBroadcast, candidates: tuple[UpcomingBroadcast, ...]) -> None:
        """Планер не выбирает и не удаляет (инвариант 8), но обязан сказать, какие эфиры мешают."""
        item.ambiguous_urls = tuple(broadcast_url_for(item.channel, broadcast.broadcast_id) for broadcast in candidates)
        LOGGER.warning(
            'broadcast_ambiguous slot_id=%s channel="%s" handle=%s candidates=%s',
            item.slot_id,
            item.channel.account_name,
            item.channel.handle,
            ",".join(broadcast.broadcast_id for broadcast in candidates),
        )
        item.warn(OutcomeWarning(WARNING_STEP_AMBIGUOUS, str(len(candidates))))

    def _find(self, item: PlannedBroadcast, broadcasts: list[UpcomingBroadcast]) -> CandidatePick:
        candidates: list[UpcomingBroadcast] = [
            broadcast for broadcast in broadcasts if to_minute(broadcast.start_utc) == item.expected.start_minute
        ]
        return self._pick_candidate(item.channel, item.slot_id, candidates)

    def _pick_candidate(
        self,
        channel: ChannelConfig,
        slot_id: str,
        candidates: list[UpcomingBroadcast],
    ) -> CandidatePick:
        """Свой маркер → он; маркер другого слота — мимо; без маркера — только один, иначе все они неразличимы."""
        unmarked: list[tuple[UpcomingBroadcast, StreamInfo | None]] = []
        for candidate in candidates:
            stream: StreamInfo | None = self._stream(channel, candidate.stream_id)
            marker: str | None = stream.title if stream is not None else None
            if marker == slot_id:
                return CandidatePick(broadcast=candidate, stream=stream)
            if marker is not None and PLANER_MARKER_PATTERN.fullmatch(marker):
                continue
            unmarked.append((candidate, stream))
        if len(unmarked) > 1:
            return CandidatePick(ambiguous=tuple(broadcast for broadcast, _ in unmarked))
        if unmarked:
            return CandidatePick(broadcast=unmarked[0][0], stream=unmarked[0][1])
        return CandidatePick()

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
        cache_key: tuple[str, str] = (channel.key, stream_id)
        if cache_key not in self._streams:
            self._streams[cache_key] = self._platform.get_stream(channel, stream_id)
        return self._streams[cache_key]
