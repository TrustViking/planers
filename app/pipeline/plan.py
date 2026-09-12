"""Единый объект запланированного эфира (ТЗ §7.2, §7.3).

Одна рабочая единица = один эфир одного слота на одном канале. Объект рождается из
пакета и канала и ничего не знает о прошлых запусках: ключ и ссылка приходят только
с площадки — из найденного эфира или из ответа на создание и привязку потока.
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
from app.platforms.base import (
    BroadcastFacts,
    CreatedBroadcast,
    PlatformLimits,
    StreamInfo,
    UpcomingBroadcast,
    broadcast_url_for,
)
from app.state.registry import FormStatus, Registration, Registry

EMPTY_MARKER: Final[str] = ""
# Шаги, сбой которых не отменяет эфир (ТЗ §7.4 п.4); тексты — в messages_ru.
WARNING_STEP_THUMBNAIL: Final[str] = "thumbnail"
WARNING_STEP_LANGUAGE: Final[str] = "language"
WARNING_STEP_AUDIENCE: Final[str] = "audience"
WARNING_STEP_CATEGORY: Final[str] = "category"
WARNING_STEP_SETTINGS: Final[str] = "settings"
WARNING_STEP_AGE_RESTRICTED: Final[str] = "age_restricted"
WARNING_STEP_FACTS: Final[str] = "facts"


class ChangedField(str, Enum):
    TITLE = "title"
    DESCRIPTION = "description"


class Decision(str, Enum):
    CREATE = "create"          # эфира нет
    MATCH = "match"            # есть, тексты совпадают
    UPDATE = "update"          # есть, название или описание отличаются
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
    facts: BroadcastFacts | None = None   # что лежит на платформе после действий (§5.6)

    # --- решение сверки
    decision: Decision = Decision.CREATE
    changed_fields: tuple[ChangedField, ...] = ()
    is_too_late: bool = False      # слот внутри min_lead_minutes: только чтение площадки, ключ храним
    stream_attached: bool = False  # эфир был без потока, поток привязан этим запуском

    # --- ключ и ссылка: только из ответа площадки (ТЗ §7.3)
    broadcast_id: str | None = None
    broadcast_url: str | None = None
    stream_url: str | None = None
    stream_key: str | None = None
    is_new_key: bool = False       # ключ получен в этом запуске: создание или привязка потока

    # --- форма и сбои этого запуска
    is_form_sent: bool = False
    form_sent_at: datetime | None = None
    last_error: str | None = None
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

    def to_registration(self, recorded_at: datetime) -> Registration:
        """Запись о сделанном (ТЗ §5.4): поля прежние, previous_broadcast_ids унаследовано и всегда пусто."""
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
            created_at=recorded_at,
            form_status=self.form_status,
            form_sent_at=self.form_sent_at,
            previous_broadcast_ids=[],
            last_error=self.last_error,
        )

    @property
    def form_status(self) -> FormStatus:
        if self.is_form_sent:
            return FormStatus.SENT
        return FormStatus.PENDING if self.is_new_key else FormStatus.NOT_SENT

    def take_new_key(self, created: CreatedBroadcast) -> None:
        """Ключ получен в этом запуске — единственное место, где ставится is_new_key."""
        self.broadcast_id = created.broadcast_id
        self.broadcast_url = created.broadcast_url
        self.stream_url = created.stream_url
        self.stream_key = created.stream_key
        self.is_new_key = True

    def take_found_key(self) -> None:
        """Ключ и ссылка найденного эфира — такие, какие сейчас лежат на площадке."""
        if self.found is None or self.found_stream is None:
            return
        self.broadcast_id = self.found.broadcast_id
        self.broadcast_url = broadcast_url_for(self.channel, self.found.broadcast_id)
        self.stream_url = self.found_stream.ingestion_address
        self.stream_key = self.found_stream.stream_name

    def warn(self, warning: OutcomeWarning) -> None:
        """Сбой, который не отменяет эфир и не меняет код выхода (ТЗ §7.4 п.4)."""
        self.warnings.append(warning)

    @property
    def is_new_key_undelivered(self) -> bool:
        """Новый ключ, до формы ещё не дошёл: финальный проход §7.5 и код выхода 1."""
        return self.is_new_key and bool(self.stream_key) and not self.is_form_sent

    @property
    def has_kept_key(self) -> bool:
        """Эфир совпал или исправлен, ключ прежний: в форму не уходит никогда (§7.5)."""
        is_found_decision: bool = self.decision in (Decision.MATCH, Decision.UPDATE)
        return is_found_decision and not self.is_new_key and self.error is None and bool(self.stream_key)
