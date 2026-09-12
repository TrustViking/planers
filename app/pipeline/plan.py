"""Единый объект запланированного эфира (ТЗ §7.2, §7.3).

Одна рабочая единица = один эфир одного слота на одном канале. Объект рождается из
пакета, дозаполняется тем, что нашлось на площадке, и результатами действий.
Сравнение идёт между двумя BroadcastSpec — «как должно быть» и «как есть», — поэтому
нормализация и обрезка применяются к обеим сторонам по построению.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Final

from app.config.loader import ChannelConfig
from app.core.text import normalize_description, normalize_title, safe_trim
from app.package.model import FormSpec, Package, Slot
from app.platforms.base import PlatformLimits, StreamInfo, UpcomingBroadcast, broadcast_url_for
from app.state.registry import FormStatus, Registration, Registry

EMPTY_MARKER: Final[str] = ""
# Шаги, сбой которых не отменяет эфир (ТЗ §7.4 п.4); тексты — в messages_ru.
WARNING_STEP_THUMBNAIL: Final[str] = "thumbnail"
WARNING_STEP_LANGUAGE: Final[str] = "language"


class ChangedField(str, Enum):
    TITLE = "title"
    DESCRIPTION = "description"


class Decision(str, Enum):
    CREATE = "create"          # эфира нет
    MATCH = "match"            # есть, тексты совпадают
    UPDATE = "update"          # есть, название или описание отличаются
    RECREATE = "recreate"      # журнал помнит эфир, на площадке его нет — владелец удалил
    NO_STREAM = "no_stream"    # эфир есть, привязанного потока нет — ключ взять неоткуда
    TOO_LATE = "too_late"      # до старта меньше min_lead_minutes — эфир не трогаем
    AMBIGUOUS = "ambiguous"    # несколько эфиров без маркера на эту минуту
    ERROR = "error"            # площадка не ответила по каналу


@dataclass(frozen=True)
class OutcomeWarning:
    """Шаг не удался, но эфир и ключ в силе: в отчёт строкой, код выхода не меняется."""

    step: str          # WARNING_STEP_*
    code: str
    message: str = ""


@dataclass(frozen=True)
class OutcomeError:
    origin: str        # "youtube" (Platform) | "registry" | "package" | "planer"
    code: str
    message: str = ""


def to_minute(value: datetime) -> datetime:
    """Момент старта с точностью до минуты в UTC — единственный способ сравнивать время."""
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


@dataclass(frozen=True)
class BroadcastSpec:
    """Как эфир должен выглядеть. Единственное место, задающее «одинаковый»."""

    start_minute: datetime   # aware UTC, секунды обнулены
    marker: str              # slot_id в названии привязанного потока (§7.3)
    title: str
    description: str

    @classmethod
    def from_slot(cls, slot: Slot, limits: PlatformLimits) -> BroadcastSpec:
        return cls(
            start_minute=to_minute(slot.start),
            marker=slot.slot_id,
            title=safe_trim(normalize_title(slot.title), limits.title_max_chars),
            description=safe_trim(normalize_description(slot.description), limits.description_max_chars),
        )

    @classmethod
    def from_platform(
        cls,
        broadcast: UpcomingBroadcast,
        stream: StreamInfo | None,
        limits: PlatformLimits,
    ) -> BroadcastSpec:
        return cls(
            start_minute=to_minute(broadcast.start_utc),
            marker=stream.title if stream is not None else EMPTY_MARKER,
            title=safe_trim(normalize_title(broadcast.title), limits.title_max_chars),
            description=safe_trim(normalize_description(broadcast.description), limits.description_max_chars),
        )

    def diff(self, other: BroadcastSpec) -> tuple[ChangedField, ...]:
        """Только тексты: по времени и маркеру эфир опознают, а не исправляют."""
        changed: list[ChangedField] = []
        if self.title != other.title:
            changed.append(ChangedField.TITLE)
        if self.description != other.description:
            changed.append(ChangedField.DESCRIPTION)
        return tuple(changed)


@dataclass
class PlannedBroadcast:
    """Изменяемый намеренно: заполняется по частям. Поля из пакета после конструктора не трогаются."""

    # --- из пакета: задаётся в конструкторе и больше не меняется
    slot: Slot
    source_package: Package        # пакет-победитель этого slot_id: превью и package_id
    channel: ChannelConfig
    expected: BroadcastSpec

    # --- найдено на площадке
    found: UpcomingBroadcast | None = None
    found_stream: StreamInfo | None = None
    actual: BroadcastSpec | None = None

    # --- решение сверки
    decision: Decision = Decision.CREATE
    changed_fields: tuple[ChangedField, ...] = ()
    is_rebind: bool = False        # журнал надо переписать на найденный эфир
    is_too_late: bool = False      # слот внутри min_lead_minutes: не планируем, но ключ храним
    stream_attached: bool = False  # эфир был без потока, поток привязан этим запуском

    # --- результат действий и память журнала
    broadcast_id: str | None = None
    broadcast_url: str | None = None
    stream_url: str | None = None
    stream_key: str | None = None
    form_status: FormStatus = FormStatus.PENDING
    form_sent_at: datetime | None = None
    previous_broadcast_ids: list[str] = field(default_factory=list)
    last_error: str | None = None
    created_at: datetime | None = None
    error: OutcomeError | None = None
    warnings: list[OutcomeWarning] = field(default_factory=list)   # превью, язык эфира

    @property
    def key(self) -> str:
        """Ключ журнала — только отсюда."""
        return Registry.key(self.slot.slot_id, self.channel.id)

    @property
    def slot_id(self) -> str:
        return self.slot.slot_id

    @property
    def language(self) -> str:
        return self.slot.language

    @property
    def date(self) -> str:
        return self.slot.date

    @property
    def time(self) -> str:
        return self.slot.time

    @property
    def account_name(self) -> str:
        return self.channel.account_name

    @property
    def form(self) -> FormSpec:
        """Форма своего пакета: у каждого слота она своя (ТЗ §7.5 п.1)."""
        return self.slot.form

    @property
    def found_url(self) -> str | None:
        if self.found is None:
            return None
        return broadcast_url_for(self.channel, self.found.broadcast_id)

    def apply_registration(self, registration: Registration) -> None:
        """Что помнит журнал: ключи и статус отправки формы (ТЗ §5.4)."""
        self.broadcast_id = registration.broadcast_id
        self.broadcast_url = registration.broadcast_url
        self.stream_url = registration.stream_url
        self.stream_key = registration.stream_key
        self.form_status = registration.form_status
        self.form_sent_at = registration.form_sent_at
        self.previous_broadcast_ids = list(registration.previous_broadcast_ids)
        self.last_error = registration.last_error
        self.created_at = registration.created_at

    def to_registration(self) -> Registration:
        """Формат записи — как в ТЗ §5.4, без изменений."""
        return Registration(
            slot_id=self.slot.slot_id,
            channel_id=self.channel.id,
            account_name=self.channel.account_name,
            language=self.slot.language,
            date=self.slot.date,
            time=self.slot.time,
            broadcast_id=self.broadcast_id,
            broadcast_url=self.broadcast_url,
            stream_url=self.stream_url,
            stream_key=self.stream_key,
            package_id=self.source_package.package_id,
            created_at=self.created_at,
            form_status=self.form_status,
            form_sent_at=self.form_sent_at,
            previous_broadcast_ids=list(self.previous_broadcast_ids),
            last_error=self.last_error,
        )

    def remember_broadcast(
        self,
        *,
        broadcast_id: str,
        broadcast_url: str,
        stream_url: str,
        stream_key: str,
        created_at: datetime,
        is_recreate: bool,
    ) -> None:
        """Новый эфир: прежний id — в историю, форма снова ждёт отправки (ТЗ §5.4, §7.3)."""
        if is_recreate and self.broadcast_id:
            self.previous_broadcast_ids.append(self.broadcast_id)
        self.broadcast_id = broadcast_id
        self.broadcast_url = broadcast_url
        self.stream_url = stream_url
        self.stream_key = stream_key
        self.created_at = created_at
        self.form_status = FormStatus.PENDING
        self.form_sent_at = None

    def warn(self, warning: OutcomeWarning) -> None:
        """Сбой, который не отменяет эфир и не меняет код выхода (ТЗ §7.4 п.4)."""
        self.warnings.append(warning)

    @property
    def needs_form(self) -> bool:
        """Финальный проход §7.5: ключ есть, а подтверждения отправки нет."""
        return bool(self.stream_key) and self.form_status is not FormStatus.SENT
