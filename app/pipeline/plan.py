"""Единый объект запланированного эфира (ТЗ §7.2, §7.3).

Одна рабочая единица = один эфир одного слота на одном канале. Объект рождается из
пакета и канала и ничего не знает о прошлых запусках: ключ и ссылка приходят только
с площадки — из найденного эфира или из ответа на создание и привязку потока.
После фазы входов объект получает объект своего канала и объект своей формы и сам решает,
допущен ли он к публикации (admit) — единственное место этого правила.
Сравнение идёт между двумя BroadcastSpec — «как должно быть» и «как есть», — поэтому
нормализация и обрезка применяются к обеим сторонам по построению. Сверяется всё, что планер
диктует площадке; какие расхождения планер исправляет, а о каких только сообщает, — FIXABLE_FIELDS
и REPORTED_FIELDS ниже, больше нигде поля не делятся.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Final

from app.config.loader import ChannelConfig, PlanerSettings
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
from app.platforms.channel import ChannelStatus
from app.ui import messages_ru as msg

if TYPE_CHECKING:   # форма и канал нужны объекту только для аннотаций: их строит runner
    from app.form.base import FormError
    from app.form.key_form import FormAnswers, KeyForm
    from app.platforms.channel import Channel

EMPTY_MARKER: Final[str] = ""
# Шаги, сбой которых не отменяет эфир (ТЗ §7.4 п.4); тексты — в messages_ru.
WARNING_STEP_THUMBNAIL: Final[str] = "thumbnail"
WARNING_STEP_LANGUAGE: Final[str] = "language"
WARNING_STEP_AUDIENCE: Final[str] = "audience"
WARNING_STEP_SETTINGS: Final[str] = "settings"
WARNING_STEP_AGE_RESTRICTED: Final[str] = "age_restricted"
WARNING_STEP_FACTS: Final[str] = "facts"
WARNING_STEP_REPORTED_FIELD: Final[str] = "reported_field"   # расходится, но через API не исправляется
WARNING_STEP_AMBIGUOUS: Final[str] = "ambiguous"             # несколько эфиров без метки на минуту старта


class ChangedField(str, Enum):
    """Поля спеки, которые планер диктует площадке. Время старта сюда не входит: по нему эфир опознают."""

    TITLE = "title"
    DESCRIPTION = "description"
    CATEGORY = "category"
    PRIVACY = "privacy"
    MARKER = "marker"
    THUMBNAIL = "thumbnail"
    AUTO_START = "auto_start"
    AUTO_STOP = "auto_stop"
    LATENCY = "latency"


# Расхождение — решение UPDATE: планер приводит эфир к пакету.
FIXABLE_FIELDS: Final[frozenset[ChangedField]] = frozenset(
    {
        ChangedField.TITLE,
        ChangedField.DESCRIPTION,
        ChangedField.CATEGORY,
        ChangedField.PRIVACY,
        ChangedField.MARKER,
        ChangedField.THUMBNAIL,
    }
)
# Расхождение решения не меняет, но доходит до владельца: лог, «внимание:» в консоли, отчёт.
# Эти поля живут в contentDetails эфира, а liveBroadcasts.update шлёт только snippet
# (BROADCAST_UPDATE_PARTS): contentDetails у update требует monitorStream. Гонять их через UPDATE
# значило бы на каждом запуске «исправлять» неисправимое и слать стримеру ключ заново.
REPORTED_FIELDS: Final[frozenset[ChangedField]] = frozenset(
    {ChangedField.AUTO_START, ChangedField.AUTO_STOP, ChangedField.LATENCY}
)
# Поле спеки для каждого сверяемого поля: diff, лог и отчёт идут по одному списку.
SPEC_ATTRIBUTES: Final[dict[ChangedField, str]] = {
    ChangedField.TITLE: "title",
    ChangedField.DESCRIPTION: "description",
    ChangedField.CATEGORY: "category_id",
    ChangedField.PRIVACY: "privacy",
    ChangedField.MARKER: "marker",
    ChangedField.THUMBNAIL: "has_own_thumbnail",
    ChangedField.AUTO_START: "auto_start",
    ChangedField.AUTO_STOP: "auto_stop",
    ChangedField.LATENCY: "latency_preference",
}

SpecValue = str | bool | None


class Decision(str, Enum):
    CREATE = "create"          # эфира нет
    MATCH = "match"            # есть, все исправимые поля совпадают
    UPDATE = "update"          # есть, исправимое поле отличается — привести к пакету
    NO_STREAM = "no_stream"    # эфир есть, привязанного потока нет — ключ взять неоткуда
    TOO_LATE = "too_late"      # до старта меньше min_lead_minutes — эфир не трогаем
    AMBIGUOUS = "ambiguous"    # несколько эфиров без маркера на эту минуту
    ERROR = "error"            # площадка не ответила по каналу
    NOT_ADMITTED = "not_admitted"   # не допущен к публикации: канал не подтверждён или форма его не примет


class AdmissionKind(str, Enum):
    CHANNEL = "channel"                  # канал не READY
    FORM_UNREADABLE = "form_unreadable"  # форма не прочиталась
    FORM_FIELD = "form_field"            # в форме нет варианта или обязательный вопрос без ответа


@dataclass(frozen=True)
class AdmissionReason:
    """Почему объект не допущен к публикации. text — для владельца, без оформления (его делает отчёт):

    FORM_FIELD — «вопрос: значение» (MissingAnswer.text); FORM_UNREADABLE — сообщение FormError;
    CHANNEL — короткая причина по статусу; полный текст отказа канала остаётся в объекте Channel.
    """

    kind: AdmissionKind
    code: str            # статус канала / код FormError / missingOption / requiredMissing
    field: str | None    # поле пакета (form.fields) или None
    text: str


@dataclass(frozen=True)
class OutcomeWarning:
    """Шаг не удался, но эфир и ключ в силе: в отчёт строкой, код выхода не меняется."""

    step: str          # WARNING_STEP_*
    code: str
    message: str = ""


@dataclass(frozen=True)
class OutcomeError:
    origin: str        # "youtube" (Platform) | "package" | "planer"
    code: str
    message: str = ""


def to_minute(value: datetime) -> datetime:
    """Момент старта с точностью до минуты в UTC — единственный способ сравнивать время."""
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


@dataclass(frozen=True)
class BroadcastSpec:
    """Как эфир должен выглядеть. Единственное место, задающее «одинаковый».

    None у поля «как есть» — площадка это поле не вернула: сравнивать не с чем, расхождением
    это не считается (так YouTube ведёт себя с категорией в списке эфиров — её держит ресурс videos).
    """

    start_minute: datetime   # aware UTC, секунды обнулены
    marker: str              # slot_id в названии привязанного потока (§7.3)
    title: str
    description: str
    privacy: str | None = None             # status.privacyStatus
    category_id: str | None = None         # snippet.categoryId
    auto_start: bool | None = None         # contentDetails.enableAutoStart
    auto_stop: bool | None = None          # contentDetails.enableAutoStop
    latency_preference: str | None = None  # contentDetails.latencyPreference
    # своя обложка: картинка эфира не совпадает с заглушкой канала; None — не сверяется
    has_own_thumbnail: bool | None = None

    @classmethod
    def from_slot(
        cls,
        slot: Slot,
        limits: PlatformLimits,
        channel: ChannelConfig,
        settings: PlanerSettings,
    ) -> BroadcastSpec:
        """Всё, что планер отправляет площадке: тексты — из слота, видимость — из канала, остальное — planer.json и площадка."""
        return cls(
            start_minute=to_minute(slot.start),
            marker=slot.slot_id,
            title=safe_trim(normalize_title(slot.title), limits.title_max_chars),
            description=safe_trim(normalize_description(slot.description), limits.description_max_chars),
            privacy=channel.privacy.value,
            category_id=settings.category_id,
            auto_start=settings.auto_start,
            auto_stop=limits.auto_stop,
            latency_preference=limits.latency_preference,
            # обложку планер ставит, только если так велит planer.json и превью есть в пакете
            has_own_thumbnail=True if settings.set_thumbnail and slot.previews else None,
        )

    @classmethod
    def from_platform(
        cls,
        broadcast: UpcomingBroadcast,
        stream: StreamInfo | None,
        limits: PlatformLimits,
        placeholders: frozenset[str] = frozenset(),
    ) -> BroadcastSpec:
        """placeholders — отпечатки заглушек канала: картинка эфира из них — обложки нет."""
        has_own_thumbnail: bool | None = (
            None if broadcast.thumbnail_sha is None else broadcast.thumbnail_sha not in placeholders
        )
        return cls(
            start_minute=to_minute(broadcast.start_utc),
            marker=stream.title if stream is not None else EMPTY_MARKER,
            title=safe_trim(normalize_title(broadcast.title), limits.title_max_chars),
            description=safe_trim(normalize_description(broadcast.description), limits.description_max_chars),
            privacy=broadcast.privacy_status,
            category_id=broadcast.category_id,
            auto_start=broadcast.auto_start,
            auto_stop=broadcast.auto_stop,
            latency_preference=broadcast.latency_preference,
            has_own_thumbnail=has_own_thumbnail,
        )

    @classmethod
    def from_facts(cls, facts: BroadcastFacts, limits: PlatformLimits, start_minute: datetime) -> BroadcastSpec:
        """Факты после действий в той же форме; start_minute — если площадка времени не вернула.

        Обложка здесь не сверяется: картинка сразу после записи может ещё не смениться — её сверяет
        список эфиров следующего запуска.
        """
        return cls(
            start_minute=to_minute(facts.start_utc) if facts.start_utc is not None else start_minute,
            marker=facts.stream_marker or EMPTY_MARKER,
            title=safe_trim(normalize_title(facts.title), limits.title_max_chars),
            description=safe_trim(normalize_description(facts.description), limits.description_max_chars),
            privacy=facts.privacy_status,
            category_id=facts.category_id,
            auto_start=facts.auto_start,
            auto_stop=facts.auto_stop,
            latency_preference=facts.latency_preference,
        )

    def value(self, changed: ChangedField) -> SpecValue:
        return getattr(self, SPEC_ATTRIBUTES[changed])

    def diff(self, other: BroadcastSpec) -> tuple[ChangedField, ...]:
        """Все диктуемые поля, маркер тоже; по времени эфир опознают, а не исправляют."""
        changed: list[ChangedField] = []
        for candidate in ChangedField:
            mine: SpecValue = self.value(candidate)
            theirs: SpecValue = other.value(candidate)
            if mine is None or theirs is None:
                continue      # площадка поле не вернула — сравнивать не с чем
            if mine != theirs:
                changed.append(candidate)
        return tuple(changed)

    def not_compared(self, other: BroadcastSpec) -> tuple[ChangedField, ...]:
        """Поля, по которым diff промолчал: одна из сторон — None. Пропажа поля не должна быть беззвучной."""
        return tuple(
            candidate
            for candidate in ChangedField
            if self.value(candidate) is None or other.value(candidate) is None
        )


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
    changed_fields: tuple[ChangedField, ...] = ()     # исправимые (FIXABLE_FIELDS)
    reported_fields: tuple[ChangedField, ...] = ()    # только сообщаем (REPORTED_FIELDS)
    ambiguous_urls: tuple[str, ...] = ()              # AMBIGUOUS: ссылки на все эфиры-кандидаты
    is_too_late: bool = False      # слот внутри min_lead_minutes: только чтение площадки, ключ храним
    stream_attached: bool = False  # эфир был без потока, поток привязан этим запуском

    # --- ключ и ссылка: только из ответа площадки (ТЗ §7.3)
    broadcast_id: str | None = None
    broadcast_url: str | None = None
    stream_url: str | None = None
    stream_key: str | None = None
    should_send_key: bool = False  # ключ должен дойти до стримера в этом запуске (require_key_delivery)

    # --- объекты запуска и допуск (admit). channel_object в production передаётся всегда;
    # None — только тесты без ChannelBook. key_form None — отправитель формы не проверяет (тесты, Noop).
    channel_object: Channel | None = None
    key_form: KeyForm | None = None
    form_failure: FormError | None = None
    form_answers: FormAnswers | None = None            # текущие ответы формы этого объекта
    admission_reasons: tuple[AdmissionReason, ...] = ()

    # --- форма и сбои этого запуска
    is_form_sent: bool = False
    form_sent_at: datetime | None = None
    last_error: str | None = None
    error: OutcomeError | None = None
    warnings: list[OutcomeWarning] = field(default_factory=list)   # превью, язык эфира

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

    @property
    def is_admitted(self) -> bool:
        """Объект, построенный напрямую (тесты), допущен: причины появляются только в admit."""
        return not self.admission_reasons

    def admit(self, channel_object: Channel | None, key_form: KeyForm | None, form_failure: FormError | None) -> None:
        """Единственное место правила допуска: канал → форма не прочиталась → незаполненные поля формы.

        Ключ и адрес потока до публикации неизвестны — «ожидаются», причиной не считаются.
        too_late допуск не проходит (у него свой путь только на чтение): поля заполняются, причин нет.
        """
        self.channel_object = channel_object
        self.key_form = key_form
        self.form_failure = form_failure
        self.form_answers = self._answers(stream_key=None, stream_url=None)
        if self.is_too_late:
            self.admission_reasons = ()
            return
        reasons: list[AdmissionReason] = []
        if channel_object is not None and channel_object.status is not ChannelStatus.READY:
            status: str = channel_object.status.value
            reasons.append(AdmissionReason(AdmissionKind.CHANNEL, status, None, msg.ADMISSION_CHANNEL_TEXT[status]))
        if form_failure is not None:
            reasons.append(AdmissionReason(AdmissionKind.FORM_UNREADABLE, form_failure.code, None, form_failure.message))
        if self.form_answers is not None:
            reasons.extend(
                AdmissionReason(AdmissionKind.FORM_FIELD, missing.code, missing.field or None, missing.text)
                for missing in self.form_answers.missing
            )
        self.admission_reasons = tuple(reasons)
        if reasons:
            self.decision = Decision.NOT_ADMITTED

    def refresh_form_answers(self) -> FormAnswers | None:
        """Ответы формы с ключом и адресом, которые объект уже взял с площадки."""
        self.form_answers = self._answers(stream_key=self.stream_key, stream_url=self.stream_url)
        return self.form_answers

    def _answers(self, *, stream_key: str | None, stream_url: str | None) -> FormAnswers | None:
        if self.key_form is None:
            return None
        return self.key_form.answers(
            language=self.language,
            start=self.slot.start,
            account_name=self.account_name,
            stream_key=stream_key,
            stream_url=stream_url,
        )

    @property
    def is_key_ready_to_send(self) -> bool:
        """Ключ должен уйти, он есть, форма его ещё не подтвердила и ответы полные.

        Без объекта формы (тестовые отправители) полноту ответа решает сам отправитель.
        """
        if not self.should_send_key or not self.stream_key or self.is_form_sent:
            return False
        if self.key_form is None:
            return True
        return self.form_answers is not None and self.form_answers.is_complete

    def take_new_key(self, created: CreatedBroadcast) -> None:
        """Ключ получен в этом запуске: эфир создан или поток привязан — стример этого ключа не видел."""
        self.broadcast_id = created.broadcast_id
        self.broadcast_url = created.broadcast_url
        self.stream_url = created.stream_url
        self.stream_key = created.stream_key
        self.require_key_delivery()

    def require_key_delivery(self) -> None:
        """Единственное место, где ставится should_send_key. Ровно четыре случая:

        эфир создан; найденному эфиру без потока поток привязан (оба — через take_new_key);
        найденный эфир исправлен (UPDATE); ручной эфир без метки усыновлён — метка планера
        проставлена, это исправление поля MARKER. Совпавший эфир с нашей меткой ключ не шлёт.
        Задвоение строки у стримера допустимо: он берёт последнюю по дате, каналу и языку.
        """
        self.should_send_key = True

    def split_changed(self, changed: tuple[ChangedField, ...]) -> None:
        """Изменившиеся поля — на исправимые и только сообщаемые; решение по ним принимает вызывающий."""
        self.changed_fields = tuple(name for name in changed if name in FIXABLE_FIELDS)
        self.reported_fields = tuple(name for name in changed if name in REPORTED_FIELDS)

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
    def is_key_undelivered(self) -> bool:
        """Ключ должен дойти до стримера, а до формы ещё не дошёл: финальный проход §7.5 и код выхода 1."""
        return self.should_send_key and bool(self.stream_key) and not self.is_form_sent

    @property
    def has_kept_key(self) -> bool:
        """Эфир с меткой планера совпал с пакетом: ключ уже уходил раньше, повторно не отправляется."""
        is_match: bool = self.decision is Decision.MATCH
        return is_match and not self.should_send_key and self.error is None and bool(self.stream_key)
