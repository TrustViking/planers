"""Запись объекта-слота в памяти планера (secrets\\planer.sqlite3): что уже сделано на YouTube и в форме.

Одна запись на (slot_id, youtube_channel_id): запись привязана к id канала на YouTube, а не к нику.
Колонки поиска — slot_start_utc (ISO-8601 UTC, только для сравнения моментов), stage, updated_at
(DD-MM-YYYY HH:MM, местное время); поля объекта — одним JSON:

    {"schema": 1, "snapshot": {…}, "results": {…}}

snapshot — для людей и разбора, обратно не читается. results — единственное, что планер читает обратно:
эфир, поток и ключ, взятые с площадки, и подтверждение формы. Задания из записи не берутся — что делать,
каждый запуск решают свежие пакеты, форма и channels.json; истина о существовании эфира и его ключе — YouTube.

Чтение терпимое, чтобы структура объекта менялась без перестройки таблицы: неизвестные ключи results
игнорируются, отсутствующие — None; другой номер schema не ошибка — читаются те же имена полей.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Final

RECORD_SCHEMA: Final[int] = 1
KEY_SCHEMA: Final[str] = "schema"
KEY_SNAPSHOT: Final[str] = "snapshot"
KEY_RESULTS: Final[str] = "results"
KEY_CONFIRMED_ANSWERS: Final[str] = "confirmed_answers"
KEY_IS_BOOTSTRAP: Final[str] = "is_bootstrap"
RECORD_ENCODING_OPTIONS: Final[dict[str, Any]] = {"ensure_ascii": False, "sort_keys": False}


class SlotStage(str, Enum):
    """Докуда дошёл объект; порядок — ADMITTED < PUBLISHED < KEY_CONFIRMED."""

    ADMITTED = "admitted"            # допущен, записан до действий
    PUBLISHED = "published"          # эфир и ключ взяты с площадки
    KEY_CONFIRMED = "key_confirmed"  # форма подтвердила текущий ключ

    @property
    def rank(self) -> int:
        """Чтобы запись не откатывалась ниже уже достигнутого для того же ключа."""
        return list(SlotStage).index(self)


@dataclass(frozen=True)
class ConfirmedAnswer:
    """Ответ формы, который она подтвердила: сравнивается entry_id → значение; вопрос — для человека."""

    entry_id: str
    title: str
    value: str


@dataclass(frozen=True)
class RecordResults:
    """Что база знает о результатах объекта; все поля необязательные."""

    broadcast_id: str | None = None
    broadcast_url: str | None = None
    stream_id: str | None = None
    stream_url: str | None = None
    stream_key: str | None = None
    published_at: str | None = None            # DD-MM-YYYY HH:MM
    confirmed_stream_key: str | None = None
    confirmed_form_url: str | None = None      # formResponse формы, которая подтвердила
    confirmed_answers: tuple[ConfirmedAnswer, ...] = ()
    confirmed_at: str | None = None            # DD-MM-YYYY HH:MM
    is_bootstrap: bool = False                 # эфир старше памяти: подтверждение записано без отправки
    thumbnail_broadcast_id: str | None = None  # эфир, которому планер поставил обложку
    thumbnail_set_at: str | None = None        # DD-MM-YYYY HH:MM — когда

    def has_confirmed(self, stream_key: str, form_url: str, answers: Sequence[ConfirmedAnswer]) -> bool:
        """Единственное сравнение «эта тройка уже подтверждена»: ключ, адрес формы и ответы (entry_id → значение)."""
        if not self.confirmed_stream_key:
            return False
        return (
            self.confirmed_stream_key == stream_key
            and self.confirmed_form_url == form_url
            and _answer_values(self.confirmed_answers) == _answer_values(answers)
        )

    def confirms_key(self, stream_key: str | None) -> bool:
        """Подтверждение есть и оно про этот ключ (адрес и ответы не сравниваются)."""
        return bool(stream_key) and self.confirmed_stream_key == stream_key

    def as_json(self) -> dict[str, Any]:
        raw: dict[str, Any] = asdict(self)
        raw[KEY_CONFIRMED_ANSWERS] = [asdict(answer) for answer in self.confirmed_answers]
        return raw

    @classmethod
    def from_json(cls, raw: Any) -> RecordResults:
        """Неизвестные ключи игнорируются, отсутствующие и не той формы — None."""
        if not isinstance(raw, Mapping):
            return cls()
        values: dict[str, Any] = {}
        for item in fields(cls):
            if item.name == KEY_CONFIRMED_ANSWERS:
                values[item.name] = _answers_from_json(raw.get(item.name))
            elif item.name == KEY_IS_BOOTSTRAP:
                values[item.name] = raw.get(item.name) is True
            else:
                value: Any = raw.get(item.name)
                values[item.name] = value if isinstance(value, str) else None
        return cls(**values)


@dataclass(frozen=True)
class RecordSnapshot:
    """Для людей и разбора: пишется, обратно не читается. Даты DD-MM-YYYY, время HH:MM по Киеву."""

    date: str
    time: str
    language: str
    account_name: str
    handle: str
    title: str
    form_url: str
    decision: str
    admission_reasons: tuple[str, ...] = ()
    last_error: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlotRecord:
    slot_id: str
    youtube_channel_id: str
    slot_start_utc: str          # ISO-8601 UTC — только для сравнения моментов
    stage: SlotStage
    updated_at: str              # DD-MM-YYYY HH:MM
    results: RecordResults
    snapshot: RecordSnapshot | None = None   # None — запись прочитана из базы (снимок обратно не читается)
    stored_json: str | None = field(default=None, compare=False, repr=False)   # JSON, как он лежит в базе

    def has_same_content(self, other: SlotRecord | None) -> bool:
        """Запись не изменилась: всё, кроме updated_at, как у последней записанной (из базы или этим запуском).

        У записи из базы снимка нет — сравнивается JSON, как он лежит в базе.
        """
        if other is None:
            return False
        own: tuple[str, ...] = (self.slot_id, self.youtube_channel_id, self.slot_start_utc, self.stage.value)
        theirs: tuple[str, ...] = (other.slot_id, other.youtube_channel_id, other.slot_start_utc, other.stage.value)
        return own == theirs and self.record_json() == other.saved_json()

    def saved_json(self) -> str:
        """JSON записи в базе: прочитанный — как прочитан, построенный — как будет записан."""
        return self.stored_json if self.stored_json is not None else self.record_json()

    def record_json(self) -> str:
        payload: dict[str, Any] = {
            KEY_SCHEMA: RECORD_SCHEMA,
            KEY_SNAPSHOT: asdict(self.snapshot) if self.snapshot is not None else {},
            KEY_RESULTS: self.results.as_json(),
        }
        return json.dumps(payload, **RECORD_ENCODING_OPTIONS)

    @classmethod
    def from_row(
        cls,
        slot_id: str,
        youtube_channel_id: str,
        slot_start_utc: str,
        stage: str,
        updated_at: str,
        record_json: str,
    ) -> SlotRecord:
        """Из строки таблицы: обратно — только results; непонятный JSON — пустые результаты."""
        try:
            payload: Any = json.loads(record_json)
        except (TypeError, ValueError):
            payload = {}
        results: Any = payload.get(KEY_RESULTS) if isinstance(payload, Mapping) else None
        return cls(
            slot_id=slot_id,
            youtube_channel_id=youtube_channel_id,
            slot_start_utc=slot_start_utc,
            stage=_stage(stage),
            updated_at=updated_at,
            results=RecordResults.from_json(results),
            stored_json=record_json,
        )


def _stage(value: str) -> SlotStage:
    try:
        return SlotStage(value)
    except ValueError:
        return SlotStage.ADMITTED


def _answer_values(answers: Sequence[ConfirmedAnswer]) -> dict[str, str]:
    return {answer.entry_id: answer.value for answer in answers}


def _answers_from_json(raw: Any) -> tuple[ConfirmedAnswer, ...]:
    if not isinstance(raw, list):
        return ()
    answers: list[ConfirmedAnswer] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        entry_id: Any = item.get("entry_id")
        value: Any = item.get("value")
        title: Any = item.get("title")
        if isinstance(entry_id, str) and isinstance(value, str):
            answers.append(ConfirmedAnswer(entry_id, title if isinstance(title, str) else "", value))
    return tuple(answers)
