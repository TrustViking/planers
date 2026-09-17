"""Единый объект запланированного эфира (ТЗ §7.2, §7.3).

Одна рабочая единица = один эфир одного слота на одном канале. Объект рождается из
пакета и канала; ключ и ссылка приходят только с площадки — из найденного эфира или из ответа
на создание и привязку потока. Из памяти планера (app/records) объект получает только результаты
прошлых запусков — главное, какую тройку «ключ, форма, ответы» форма уже подтвердила; задания
из памяти не берутся. Должен ли ключ уйти в форму, решает одно правило — decide_key_delivery.
После фазы входов объект получает объект своего канала и объект своей формы и сам решает,
допущен ли он к публикации (admit) — единственное место этого правила.
Сравнение идёт между двумя BroadcastSpec — «как должно быть» и «как есть», — поэтому
нормализация и обрезка применяются к обеим сторонам по построению. Сверяется всё, что планер
диктует площадке; какие расхождения планер исправляет, а о каких только сообщает, — FIXABLE_FIELDS
и REPORTED_FIELDS ниже, больше нигде поля не делятся.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Final

from app.config.loader import ChannelConfig, PlanerSettings
from app.core.dates import format_datetime_text
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
from app.records.slot_record import ConfirmedAnswer, RecordResults, RecordSnapshot, SlotRecord, SlotStage
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
    stream_id: str | None = None
    stream_url: str | None = None
    stream_key: str | None = None
    should_send_key: bool = False  # ключ должен дойти до стримера в этом запуске (decide_key_delivery)

    # --- память планера: запись прошлых запусков (из неё читаются только результаты)
    record: SlotRecord | None = None
    confirmed_results: RecordResults | None = None   # подтверждение этого запуска (confirm_key); иначе — из записи
    is_bootstrap_confirmed: bool = False             # первый запуск с памятью записал подтверждение без отправки

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
        """Ключ получен в этом запуске: эфир создан или поток привязан."""
        self.broadcast_id = created.broadcast_id
        self.broadcast_url = created.broadcast_url
        self.stream_id = created.stream_id
        self.stream_url = created.stream_url
        self.stream_key = created.stream_key

    # --- память планера и правило отправки

    @property
    def record_channel_id(self) -> str | None:
        """Id канала на YouTube — ключ записи. Канал не READY или без ответа YouTube — записи нет.

        Без объекта канала (только тесты без ChannelBook) — ключ канала из channels.json.
        """
        if self.channel_object is None:
            return self.channel.key
        if self.channel_object.status is not ChannelStatus.READY or self.channel_object.info is None:
            return None
        return self.channel_object.info.youtube_channel_id

    @property
    def results(self) -> RecordResults:
        """Результаты, которые знает объект: подтверждение этого запуска, иначе — из записи."""
        if self.confirmed_results is not None:
            return self.confirmed_results
        return self.record.results if self.record is not None else RecordResults()

    @property
    def form_response_url(self) -> str:
        """Адрес формы, куда уходит ответ; без объекта формы (тесты) — адрес из пакета."""
        return self.key_form.structure.response_url if self.key_form is not None else self.form.url

    @property
    def answers_record(self) -> tuple[ConfirmedAnswer, ...]:
        """Текущие ответы формы в каноническом виде (entry_id, вопрос, значение)."""
        if self.form_answers is None:
            return ()
        return tuple(ConfirmedAnswer(*answer) for answer in self.form_answers.as_record())

    def decide_key_delivery(self) -> None:
        """ЕДИНСТВЕННОЕ правило отправки ключа в форму (инвариант 1a).

        Ключ должен уйти, если объект допущен, не too_late, ключ и адрес потока взяты с площадки в этом
        запуске и память не хранит подтверждение ровно этой тройки: тот же ключ, тот же адрес формы,
        те же ответы. Вид действия (создан, привязан, исправлен, совпал, усыновлён) не важен; ошибка
        другого шага отправку не отменяет. Полны ли ответы, решает is_key_ready_to_send: неполные —
        ключ «должен был уйти и не ушёл».
        """
        self.refresh_form_answers()
        self.should_send_key = (
            self.is_admitted
            and not self.is_too_late
            and bool(self.stream_key)
            and bool(self.stream_url)
            and not self.results.has_confirmed(self.stream_key or "", self.form_response_url, self.answers_record)
        )

    def bootstrap_confirmation(self, now: datetime) -> bool:
        """Первый запуск с памятью: эфир с меткой планера этого слота уже стоял — ключ считается переданным.

        Только MATCH и UPDATE с найденным потоком, чьё название — slot_id; ручной эфир без метки
        и созданные этим запуском идут по общему правилу. Ответы должны быть полными: их и запоминаем.
        """
        is_marked: bool = self.found_stream is not None and self.found_stream.title == self.slot_id
        if not (self.is_admitted and not self.is_too_late and self.record is None and is_marked):
            return False
        if self.decision not in (Decision.MATCH, Decision.UPDATE) or not self.stream_key or not self.stream_url:
            return False
        answers: FormAnswers | None = self.refresh_form_answers()
        if answers is not None and not answers.is_complete:
            return False
        self.confirm_key(now, is_bootstrap=True)
        self.is_bootstrap_confirmed = True
        return True

    def confirm_key(self, now: datetime, *, is_bootstrap: bool = False) -> None:
        """Форма подтвердила текущую тройку: ключ, адрес формы, ответы."""
        self.confirmed_results = replace(
            self.results,
            confirmed_stream_key=self.stream_key,
            confirmed_form_url=self.form_response_url,
            confirmed_answers=self.answers_record,
            confirmed_at=format_datetime_text(now.astimezone()),
            is_bootstrap=is_bootstrap,
        )

    def to_record(self, now: datetime, stage: SlotStage) -> SlotRecord:
        """Запись объекта: снимок для людей и результаты. Стадия не откатывается ниже достигнутого для этого ключа."""
        channel_id: str | None = self.record_channel_id
        if channel_id is None:
            raise ValueError(f"no YouTube channel id for {self.slot_id}")
        results: RecordResults = self._published_results(now)
        reached: list[SlotStage] = [stage]
        if results.stream_key:
            reached.append(SlotStage.PUBLISHED)
        if results.confirms_key(results.stream_key):
            reached.append(SlotStage.KEY_CONFIRMED)
        return SlotRecord(
            slot_id=self.slot_id,
            youtube_channel_id=channel_id,
            slot_start_utc=self.slot.start.astimezone(timezone.utc).isoformat(),
            stage=max(reached, key=lambda item: item.rank),
            updated_at=format_datetime_text(now.astimezone()),
            results=results,
            snapshot=self._snapshot(),
        )

    def _published_results(self, now: datetime) -> RecordResults:
        """Эфир, поток и ключ — взятые этим запуском; не взяты — прежние из записи."""
        results: RecordResults = self.results
        if not self.stream_key:
            return results
        is_same_key: bool = results.stream_key == self.stream_key and results.published_at is not None
        return replace(
            results,
            broadcast_id=self.broadcast_id,
            broadcast_url=self.broadcast_url,
            stream_id=self.stream_id,
            stream_url=self.stream_url,
            stream_key=self.stream_key,
            published_at=results.published_at if is_same_key else format_datetime_text(now.astimezone()),
        )

    def _snapshot(self) -> RecordSnapshot:
        return RecordSnapshot(
            date=self.date,
            time=self.time,
            language=self.language,
            account_name=self.account_name,
            handle=self.channel.handle,
            title=self.expected.title,
            form_url=self.form.url,
            decision=self.decision.value,
            admission_reasons=tuple(reason.text for reason in self.admission_reasons),
            last_error=self.last_error,
            warnings=tuple(f"{warning.step}:{warning.code}" for warning in self.warnings),
        )

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
        self.stream_id = self.found_stream.stream_id
        self.stream_url = self.found_stream.ingestion_address
        self.stream_key = self.found_stream.stream_name

    def warn(self, warning: OutcomeWarning) -> None:
        """Сбой, который не отменяет эфир и не меняет код выхода (ТЗ §7.4 п.4)."""
        self.warnings.append(warning)

    @property
    def is_key_undelivered(self) -> bool:
        """Ключ должен дойти до стримера, а до формы ещё не дошёл: код выхода 1 и «НЕ отправлен» в keys.txt."""
        return self.should_send_key and bool(self.stream_key) and not self.is_form_sent

    @property
    def has_kept_key(self) -> bool:
        """Память хранит подтверждение текущего ключа, и в этом запуске он повторно не отправлялся."""
        if self.should_send_key or self.is_form_sent or self.error is not None:
            return False
        return self.results.confirms_key(self.stream_key)
