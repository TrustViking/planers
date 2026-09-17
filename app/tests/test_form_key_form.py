"""Объект-форма на настоящей странице тренировочной формы (app/tests/data/form_response_refusal.html).

Страница отказа — перерисованная форма целиком: в ней тот же FB_PUBLIC_LOAD_DATA_, что у viewform.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.form.base import FORM_CODE_MISSING_OPTION, FORM_CODE_REQUIRED_MISSING
from app.form.discovery import FormDiscovery, FormStructure
from app.form.key_form import FIELD_DATE, FIELD_LANGUAGE, FIELD_STREAM_KEY, FIELD_STREAM_URL, FormAnswers, KeyForm
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
