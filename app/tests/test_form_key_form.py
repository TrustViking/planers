"""Объект-форма на настоящей странице тренировочной формы (app/tests/data/form_response_refusal.html).

Страница отказа — перерисованная форма целиком: в ней тот же FB_PUBLIC_LOAD_DATA_, что у viewform.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import logging
from dataclasses import replace

import pytest

from app.form.base import FORM_CODE_MISSING_OPTION, FORM_CODE_REQUIRED_MISSING
from app.form.discovery import FormDiscovery, FormQuestion, FormStructure, QuestionKind
from app.form.key_form import (
    FIELD_DATE,
    FIELD_LANGUAGE,
    FIELD_STREAM_KEY,
    FIELD_STREAM_URL,
    DateCoverage,
    FormAnswers,
    KeyForm,
)
from app.package.model import FormSpec
from app.tests.conftest import build_form_spec
from app.tests.test_form_discovery import SHORT_URL, _FakeResponse, _FakeSession

KYIV: timezone = timezone(timedelta(hours=3))
PAGE: str = (Path(__file__).parent / "data" / "form_response_refusal.html").read_text(encoding="utf-8")
START: datetime = datetime(2026, 9, 13, 19, 0, tzinfo=KYIV)
KEY: str = "abcd-abcd-abcd-abcd-abcd"
STREAM_URL: str = "rtmp://a.rtmp.youtube.com/live2"
LANGUAGE_ENTRY: str = "entry.1596786721"
NAME_ENTRY: str = "entry.861880560"
DATE_ENTRY: str = "entry.1569784614"
PLATFORM_ENTRY: str = "entry.777814017"
KEY_ENTRY: str = "entry.1403158871"
URL_ENTRY: str = "entry.721693817"


@pytest.fixture
def structure(tmp_path: Path) -> FormStructure:
    session: _FakeSession = _FakeSession(_FakeResponse(PAGE))
    return FormDiscovery(session, tmp_path, datetime(2026, 9, 13, 12, 0)).structure(SHORT_URL)


@pytest.fixture
def form(structure: FormStructure) -> KeyForm:
    return KeyForm.build(build_form_spec(), structure)


def _answers(
    form: KeyForm,
    *,
    language: str = "uk",
    start: datetime = START,
    stream_key: str | None = KEY,
    stream_url: str | None = STREAM_URL,
) -> FormAnswers:
    return form.answers(
        language=language, start=start, account_name="Osvald.X", stream_key=stream_key, stream_url=stream_url
    )


def _values(answers: FormAnswers) -> dict[str, str]:
    return {answer.question.entry_id: answer.value for answer in answers.answers}


def test_fields_are_matched_to_questions_once(form: KeyForm) -> None:
    assert form.questions[FIELD_LANGUAGE] is not None and form.questions[FIELD_LANGUAGE].entry_id == LANGUAGE_ENTRY
    assert set(form.questions) == {"language", "account_name", "date", "platform", "stream_key", "stream_url"}
    assert "13.09.2026" in form.accepted_dates and "11.09.2026" in form.accepted_dates
    assert form.accepted_languages == ("uk", "ru", "en")
    assert len(form.stream_url_options) == 2


def test_full_answer_for_the_youtube_branch(form: KeyForm) -> None:
    answers: FormAnswers = _answers(form)
    assert answers.is_complete and answers.error() is None
    assert _values(answers) == {
        LANGUAGE_ENTRY: "Украинский ( Ukranian)",
        NAME_ENTRY: "Osvald.X",                                  # текстовый вопрос — как есть
        DATE_ENTRY: "13.09.2026 Дата стрима (время стрима указано в объявлении)",
        PLATFORM_ENTRY: "You Tube",
        KEY_ENTRY: KEY,
        URL_ENTRY: "rtmp://A.rtmp.youtube.com/live2/",           # другой регистр и слэш — вариант формы
    }
    assert answers.pages == (0, 1)                               # раздел YouTube; Facebook и прочие — нет


def test_date_without_option_is_missing(form: KeyForm) -> None:
    answers: FormAnswers = _answers(form, start=datetime(2027, 3, 18, 19, 0, tzinfo=KYIV))
    [missing] = answers.missing
    assert (missing.field, missing.code, missing.text) == (
        FIELD_DATE, FORM_CODE_MISSING_OPTION, "Время стрима ( Stream time ): 18.03.2027"
    )
    error = answers.error()
    assert error is not None and error.code == FORM_CODE_MISSING_OPTION
    assert DATE_ENTRY not in _values(answers)


def test_missing_option_keeps_question_and_value_apart(form: KeyForm) -> None:
    """Владельцу вопрос и значение называются по отдельности, не склеенной строкой «вопрос: значение»."""
    [missing] = _answers(form, start=datetime(2027, 3, 18, 19, 0, tzinfo=KYIV)).missing
    assert (missing.question, missing.value) == ("Время стрима ( Stream time )", "18.03.2027")


def test_pages_by_navigation_are_logged_once_per_form(form: KeyForm, caplog: pytest.LogCaptureFixture) -> None:
    """Прогон 18-09-2026 14:04: 57 одинаковых строк за запуск — теперь одна, DEBUG; разделы у всех ответов те же."""
    caplog.set_level(logging.DEBUG, logger="planer")
    pages: set[tuple[int, ...]] = {_answers(form).pages for _ in range(5)}
    pages.add(_answers(form, stream_key=None, stream_url=None).pages)
    assert pages == {(0, 1)}
    records = [record for record in caplog.records if record.getMessage().startswith("form_pages_by_navigation ")]
    assert len(records) == 1 and records[0].levelno == logging.DEBUG


def test_same_url_for_another_package_does_not_repeat_the_pages_line(
    form: KeyForm, caplog: pytest.LogCaptureFixture
) -> None:
    """Другой пакет с той же ссылкой — своя KeyForm (for_spec), но строка разделов — одна на форму за запуск."""
    caplog.set_level(logging.DEBUG, logger="planer")
    other: KeyForm = form.for_spec(replace(form.spec, date_format="%d.%m.%Y"))
    _answers(form)
    _answers(other)
    _answers(form.for_spec(form.spec))
    assert sum(1 for message in caplog.messages if message.startswith("form_pages_by_navigation ")) == 1


def test_time_is_not_a_missing_field(form: KeyForm) -> None:
    """Времени в форме нет: 13.09.2026 23:30 и 00:10 — один вариант даты, незаполненных полей нет."""
    late: FormAnswers = _answers(form, start=datetime(2026, 9, 13, 23, 30, tzinfo=KYIV))
    assert late.is_complete and _values(late)[DATE_ENTRY].startswith("13.09.2026")


def test_language_without_option_is_missing(form: KeyForm) -> None:
    answers: FormAnswers = _answers(form, language="hu")
    assert [(item.field, item.text) for item in answers.missing] == [
        (FIELD_LANGUAGE, "Язык стрима ( Language of stream): -")
    ]


def test_stream_url_not_in_options_is_missing(form: KeyForm) -> None:
    answers: FormAnswers = _answers(form, stream_url="rtmp://b.rtmp.youtube.com/live2")
    assert [(item.field, item.code) for item in answers.missing] == [(FIELD_STREAM_URL, FORM_CODE_MISSING_OPTION)]


def test_key_and_url_before_publication_are_pending_not_missing(form: KeyForm) -> None:
    answers: FormAnswers = _answers(form, stream_key=None, stream_url=None)
    assert answers.missing == ()
    assert answers.pending == ("You Tube Stream Key", "Stream-URL (YT)")
    assert not answers.is_complete
    assert answers.pages == (0, 1)
    assert KEY_ENTRY not in _values(answers) and URL_ENTRY not in _values(answers)


def test_required_question_without_answer(structure: FormStructure) -> None:
    """Вопрос «Название канала» в пакете не назван — в форме он обязательный: requiredMissing."""
    spec = build_form_spec()
    fields = dict(spec.fields)
    fields["account_name"] = None
    form: KeyForm = KeyForm.build(type(spec)(spec.url, fields, spec.values, spec.date_format), structure)
    answers: FormAnswers = _answers(form)
    assert [(item.code, item.text) for item in answers.missing] == [
        (FORM_CODE_REQUIRED_MISSING, "Название канала ( Channel name)")
    ]
    assert FIELD_STREAM_KEY not in {item.field for item in answers.missing}


def training_key_form(tmp_path: Path) -> KeyForm:
    """Настоящая тренировочная форма (сохранённая страница) — для тестов допуска объектов."""
    session: _FakeSession = _FakeSession(_FakeResponse(PAGE))
    structure: FormStructure = FormDiscovery(session, tmp_path, datetime(2026, 9, 13, 12, 0)).structure(SHORT_URL)
    return KeyForm.build(build_form_spec(), structure)


# --- предстартовая проверка дат (задача 5n-D): в форме 11.09.2026, 12.09.2026, 13.09.2026, 17.03.2027

def _start(day: int, month: int = 9, year: int = 2026, hour: int = 19) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=KYIV)


def test_date_coverage_complete_when_every_date_has_an_option(form: KeyForm) -> None:
    coverage: DateCoverage = form.date_coverage([_start(13), _start(11), _start(17, 3, 2027)])
    assert coverage.is_checkable and coverage.is_complete
    assert coverage.wanted == ("11.09.2026", "13.09.2026", "17.03.2027")
    assert coverage.missing == () and coverage.missing_dates == () and coverage.missing_text == ""
    assert coverage.accepted_count == len(form.accepted_dates)
    assert coverage.question_title == build_form_spec().fields[FIELD_DATE]
    assert coverage.form_url == form.url


def test_date_coverage_lists_missing_dates_in_order_in_planer_format(form: KeyForm) -> None:
    coverage: DateCoverage = form.date_coverage([_start(20), _start(13), _start(14)])
    assert not coverage.is_complete
    assert coverage.wanted == ("13.09.2026", "14.09.2026", "20.09.2026")
    assert coverage.missing == ("14.09.2026", "20.09.2026")
    assert coverage.missing_dates == (date(2026, 9, 14), date(2026, 9, 20))
    assert coverage.missing_text == "14-09-2026, 20-09-2026"


def test_date_coverage_counts_one_date_of_several_slots_once(form: KeyForm) -> None:
    coverage: DateCoverage = form.date_coverage([_start(14, hour=19), _start(14, hour=21), _start(13)])
    assert coverage.wanted == ("13.09.2026", "14.09.2026")
    assert coverage.missing == ("14.09.2026",)


def test_date_coverage_of_a_text_question_accepts_any_date(form: KeyForm) -> None:
    date_question: FormQuestion | None = form.questions[FIELD_DATE]
    assert date_question is not None
    text_question: FormQuestion = replace(date_question, kind=QuestionKind.TEXT, options=())
    structure: FormStructure = replace(
        form.structure,
        questions=tuple(text_question if item == date_question else item for item in form.structure.questions),
    )
    coverage: DateCoverage = KeyForm.build(form.spec, structure).date_coverage([_start(20)])
    assert not coverage.is_checkable and coverage.is_complete and coverage.missing == ()
    assert coverage.question_title == date_question.title


def test_date_coverage_without_date_field_checks_nothing(form: KeyForm) -> None:
    spec: FormSpec = replace(form.spec, fields={**form.spec.fields, FIELD_DATE: None})
    coverage: DateCoverage = KeyForm.build(spec, form.structure).date_coverage([_start(20)])
    assert coverage.question_title == "" and not coverage.is_checkable and coverage.missing == ()


def test_date_coverage_agrees_with_answers(form: KeyForm) -> None:
    """Проверка и отправка сопоставляют даты одним кодом: недостающая дата — missingOption, и наоборот."""
    starts: list[datetime] = [_start(11), _start(14), _start(17, 3, 2027), _start(18, 3, 2027)]
    coverage: DateCoverage = form.date_coverage(starts)
    for start in starts:
        answers: FormAnswers = _answers(form, start=start)
        is_missing: bool = any(
            item.field == FIELD_DATE and item.code == FORM_CODE_MISSING_OPTION for item in answers.missing
        )
        assert is_missing == (start.strftime(form.spec.date_format) in coverage.missing)
    assert coverage.missing == ("14.09.2026", "18.03.2027")
