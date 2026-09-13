from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import PlanerConfig
from app.form.base import (
    FORM_CODE_MISSING_OPTION,
    FORM_CODE_NOT_CONFIRMED,
    FORM_CODE_REQUIRED_MISSING,
    FORM_CODE_TRANSPORT_FAILED,
    FormSendResult,
)
from app.form.discovery import FormDiscovery, FormStructure, SectionJump
from app.form.submitter import CONFIRMATION_MARKERS, Confirmation, GoogleFormSender, read_confirmation
from app.package.model import Slot
from app.pipeline.plan import PlannedBroadcast
from app.tests.conftest import build_planned
from app.tests.test_form_discovery import (
    SHORT_URL,
    YOUTUBE_SECTION_ID,
    _FakeResponse,
    _FakeSession,
    build_html,
    build_payload,
    default_items,
)

ConfigFactory = Callable[..., PlanerConfig]
SlotFactory = Callable[..., Slot]
KYIV: timezone = timezone(timedelta(hours=2))
RESPONSE_URL: str = "https://docs.google.com/forms/d/e/ABC/formResponse?hl=en"
FORM_TITLE: str = "TEST_Регистрация стрима (Stream registration)"
# Настоящие страницы ответа тренировочной формы, опыт app/tools/form_probe.py 13-09-2026 16:27:
# успех — отправка A1 (HTTP 200), отказ — опыт D1 без обязательного «Stream-URL (YT)» (HTTP 400).
DATA_DIR: Path = Path(__file__).parent / "data"
SUCCESS_PAGE: str = (DATA_DIR / "form_response_success.html").read_text(encoding="utf-8")
REFUSAL_PAGE: str = (DATA_DIR / "form_response_refusal.html").read_text(encoding="utf-8")
ENGLISH_CONFIRMATION: str = "Your response has been recorded."
CONFIRMED_BODY: str = SUCCESS_PAGE
LEGACY_CLASS_BODY: str = '<html><div class="freebirdFormviewerViewResponseConfirmationMessage"></div></html>'
STREAM_URL: str = "rtmp://A.rtmp.youtube.com/live2"
STREAM_KEY: str = "abcd-abcd-abcd-abcd-abcd"


def _planned(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    *,
    start: datetime | None = None,
    language: str = "ru",
) -> PlannedBroadcast:
    slot: Slot = make_slot_object(start or datetime(2027, 3, 17, 19, 0, tzinfo=KYIV), language)
    item: PlannedBroadcast = build_planned(slot, make_config().channels[1])
    item.stream_key = STREAM_KEY
    item.stream_url = STREAM_URL
    return item


def _sender(session: _FakeSession, tmp_path: Path) -> GoogleFormSender:
    now: datetime = datetime(2027, 3, 16, 12, 0)
    return GoogleFormSender(session, FormDiscovery(session, tmp_path / "logs", now))


class _FixedDiscovery(FormDiscovery):
    """Структура задана тестом: так проверяется защита submitter независимо от разбора формы."""

    def __init__(self, session: _FakeSession, tmp_path: Path, structure: FormStructure) -> None:
        super().__init__(session, tmp_path / "logs", datetime(2027, 3, 16, 12, 0))
        self._structure: FormStructure = structure

    def structure(self, form_url: str) -> FormStructure:
        return self._structure


def _session(*bodies: str, status_code: int = 200) -> _FakeSession:
    return _FakeSession(
        _FakeResponse(build_html()),
        *[_FakeResponse(body, status_code=status_code) for body in bodies],
    )


def test_body_contains_expected_entries(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    session: _FakeSession = _session(CONFIRMED_BODY)
    result: FormSendResult = _sender(session, tmp_path).send(_planned(make_config, make_slot_object))

    assert result.confirmed is True
    [(url, body)] = session.post_calls
    assert url == RESPONSE_URL                                             # язык ответа задаёт планер
    assert body["entry.1"] == ["Русский ( Russian)"]                       # язык из form.values
    assert body["entry.2"] == ["Account yt_ru"]                            # название канала
    assert body["entry.3"] == ["17.03.2027 Дата стрима (время стрима указано в объявлении)"]
    assert body["entry.4"] == ["You Tube"]
    assert body["entry.5"] == [STREAM_KEY]
    assert body["entry.6"] == ["rtmp://a.rtmp.youtube.com/live2/"]         # вариант как он есть в форме
    assert body["fvv"] == ["1"]
    assert body["fbzx"] == ["-1234567890"]
    assert body["pageHistory"] == ["0,1"]
    assert "entry.7" not in body                                          # раздел Facebook не проходим


def test_null_fields_are_not_sent(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    """time, broadcast_url и slot_id в пакете null — таких вопросов в форме нет (§5.1)."""
    session: _FakeSession = _session(CONFIRMED_BODY)
    item: PlannedBroadcast = _planned(make_config, make_slot_object)
    _sender(session, tmp_path).send(item)
    [(_, body)] = session.post_calls
    assert sorted(key for key in body if key.startswith("entry.")) == [
        "entry.1",
        "entry.2",
        "entry.3",
        "entry.4",
        "entry.5",
        "entry.6",
    ]


def test_stream_url_matches_ignoring_case_and_slash(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    session: _FakeSession = _session(CONFIRMED_BODY)
    item: PlannedBroadcast = _planned(make_config, make_slot_object)
    item.stream_url = "RTMP://a.RTMP.youtube.com/live2/"
    assert _sender(session, tmp_path).send(item).confirmed is True
    [(_, body)] = session.post_calls
    assert body["entry.6"] == ["rtmp://a.rtmp.youtube.com/live2/"]


def test_missing_date_option_stops_the_send(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    """Нет варианта на дату — не отправляем ничего: значение вне вариантов запрещено (§7.5)."""
    session: _FakeSession = _session(CONFIRMED_BODY)
    item: PlannedBroadcast = _planned(
        make_config,
        make_slot_object,
        start=datetime(2027, 3, 25, 19, 0, tzinfo=KYIV),
    )
    result: FormSendResult = _sender(session, tmp_path).send(item)
    assert result.confirmed is False
    assert result.code == FORM_CODE_MISSING_OPTION
    assert "25.03.2027" in (result.error or "")
    assert session.post_calls == []


def test_required_question_without_value_stops_the_send(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    items: list[Any] = default_items()
    # обязательный вопрос в разделе YouTube, который планер не заполняет
    items.insert(7, [8, "Ещё один обязательный", None, 0, [[8, None, 1]]])
    session: _FakeSession = _FakeSession(_FakeResponse(build_html(build_payload(items))), _FakeResponse(CONFIRMED_BODY))
    result: FormSendResult = _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    assert result.code == FORM_CODE_REQUIRED_MISSING
    assert "Ещё один обязательный" in (result.error or "")
    assert session.post_calls == []


def test_response_without_marker_is_not_confirmed(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    """Ответ 200, но это не страница формы — доставкой ключа не считается."""
    session: _FakeSession = _session("<html>что-то пошло не так</html>")
    result: FormSendResult = _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    assert result.confirmed is False
    assert result.code == FORM_CODE_NOT_CONFIRMED
    assert result.diagnostic_path is not None and result.diagnostic_path.exists()
    assert "что-то пошло не так" in result.diagnostic_path.read_text(encoding="utf-8")


def test_server_error_is_retried_three_times(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.form.submitter as submitter_module

    monkeypatch.setattr(submitter_module.time, "sleep", lambda seconds: None)
    session: _FakeSession = _FakeSession(
        _FakeResponse(build_html()),
        _FakeResponse("", status_code=500),
        _FakeResponse("", status_code=500),
        _FakeResponse("", status_code=500),
    )
    result: FormSendResult = _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    assert result.code == FORM_CODE_TRANSPORT_FAILED
    assert len(session.post_calls) == 3


def test_client_error_is_not_retried(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    session: _FakeSession = _session("<html>нет</html>", status_code=400)
    result: FormSendResult = _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    assert (result.confirmed, result.code) == (False, FORM_CODE_NOT_CONFIRMED)
    assert result.error == "HTTP 400"
    assert len(session.post_calls) == 1


def test_structure_is_read_once_for_two_objects(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    session: _FakeSession = _session(CONFIRMED_BODY)
    sender: GoogleFormSender = _sender(session, tmp_path)
    sender.send(_planned(make_config, make_slot_object))
    sender.send(_planned(make_config, make_slot_object, start=datetime(2027, 3, 18, 19, 0, tzinfo=KYIV)))
    assert len(session.get_calls) == 1
    assert len(session.post_calls) == 2


def test_page_history_uses_page_index_not_section_id(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    """Регрессия живого прогона 13-09-2026: было pageHistory=0,1281939289 и HTTP 400."""
    session: _FakeSession = _session(CONFIRMED_BODY)
    _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    [(_, body)] = session.post_calls
    assert str(YOUTUBE_SECTION_ID) not in body["pageHistory"][0]
    assert body["pageHistory"] == ["0,1"]


def test_unknown_section_id_falls_back_to_answered_pages(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    items: list[Any] = default_items()
    items[3][4][0][1] = [["You Tube", None, 555], ["Facebook", None, 777]]
    session: _FakeSession = _FakeSession(_FakeResponse(build_html(build_payload(items))), _FakeResponse(CONFIRMED_BODY))
    assert _sender(session, tmp_path).send(_planned(make_config, make_slot_object)).confirmed is True
    [(_, body)] = session.post_calls
    pages: list[int] = [int(page) for page in body["pageHistory"][0].split(",")]
    assert pages == [0, 1]
    assert all(0 <= page < 3 for page in pages)


def test_out_of_range_page_is_never_sent(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    """Даже если разбор формы ошибся, номер вне 0..page_count-1 в pageHistory не попадает."""
    html_session: _FakeSession = _FakeSession(_FakeResponse(build_html()))
    now: datetime = datetime(2027, 3, 16, 12, 0)
    parsed: FormStructure = FormDiscovery(html_session, tmp_path / "logs", now).structure(SHORT_URL)
    broken: FormStructure = FormStructure(
        view_url=parsed.view_url,
        response_url=parsed.response_url,
        fbzx=parsed.fbzx,
        questions=parsed.questions,
        navigation={"entry.4": {"You Tube": SectionJump(section_id=YOUTUBE_SECTION_ID, page_index=YOUTUBE_SECTION_ID)}},
        page_count=parsed.page_count,
    )
    session: _FakeSession = _FakeSession(_FakeResponse(CONFIRMED_BODY))
    sender: GoogleFormSender = GoogleFormSender(session, _FixedDiscovery(session, tmp_path, broken))
    assert sender.send(_planned(make_config, make_slot_object)).confirmed is True
    [(_, body)] = session.post_calls
    assert body["pageHistory"] == ["0,1"]


def test_real_success_page_is_confirmed() -> None:
    confirmation: Confirmation = read_confirmation(200, SUCCESS_PAGE)
    assert confirmation.is_confirmed is True
    assert (confirmation.entry_fields, confirmation.has_fbzx, confirmation.is_form_page) == (0, False, True)
    assert confirmation.marker == "your response has been recorded"


def test_real_refusal_page_is_not_confirmed() -> None:
    """Страница отказа — перерисованный раздел формы: в ней есть поле entry. и скрытый fbzx."""
    confirmation: Confirmation = read_confirmation(400, REFUSAL_PAGE)
    assert confirmation.is_confirmed is False
    assert confirmation.entry_fields >= 1 and confirmation.has_fbzx is True
    assert confirmation.marker is None


def test_refusal_page_is_not_confirmed_even_with_http_200() -> None:
    """Решает структура страницы: отказ с кодом 200 тоже не подтверждение."""
    assert read_confirmation(200, REFUSAL_PAGE).is_confirmed is False


@pytest.mark.parametrize(
    "visible_text",
    ["Вашу відповідь було записано.", "Ваш ответ записан.", "Válaszát rögzítettük."],
    ids=["ukrainian_live_03_01", "russian", "no_marker_matches"],
)
def test_success_page_is_confirmed_whatever_its_language(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    caplog: pytest.LogCaptureFixture,
    visible_text: str,
) -> None:
    """Регрессия 13-09-2026 03:01: форма ответ записала, а страница пришла не на том языке."""
    assert SUCCESS_PAGE.count(ENGLISH_CONFIRMATION) == 1
    page: str = SUCCESS_PAGE.replace(ENGLISH_CONFIRMATION, visible_text)
    session: _FakeSession = _session(page)
    with caplog.at_level("INFO"):
        result: FormSendResult = _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    assert (result.confirmed, result.diagnostic_path) == (True, None)
    [line] = [message for message in caplog.messages if message.startswith("form_confirmed")]
    assert "entry_fields=0 fbzx=False form_page=True" in line


def test_success_page_without_any_marker_is_confirmed() -> None:
    """Ни один текстовый маркер не совпал — подтверждение всё равно по структуре."""
    page: str = SUCCESS_PAGE.replace(ENGLISH_CONFIRMATION, "Válaszát rögzítettük.")
    confirmation: Confirmation = read_confirmation(200, page)
    assert (confirmation.is_confirmed, confirmation.marker) == (True, None)


def test_marker_is_a_log_signal_only_and_ignores_case() -> None:
    confirmation: Confirmation = read_confirmation(200, "<div>YOUR RESPONSE HAS BEEN RECORDED</div>")
    assert confirmation.marker == "your response has been recorded"
    assert confirmation.is_confirmed is False                    # не страница формы — не доставка
    assert all(marker == marker.lower() and not marker.endswith(".") for marker in CONFIRMATION_MARKERS)


def test_error_page_is_not_confirmed_and_logged_with_both_signals(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session: _FakeSession = _session(REFUSAL_PAGE, status_code=400)
    with caplog.at_level("WARNING"):
        result: FormSendResult = _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    assert (result.confirmed, result.code, result.error) == (False, FORM_CODE_NOT_CONFIRMED, "HTTP 400")
    assert result.diagnostic_path is not None and result.diagnostic_path.exists()
    assert "This is a required question" in result.diagnostic_path.read_text(encoding="utf-8")
    [line] = [message for message in caplog.messages if message.startswith("form_not_confirmed")]
    assert "http_status=400" in line and "fbzx=True" in line and "marker=None" in line and FORM_TITLE in line


def test_success_is_logged_with_http_status(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session: _FakeSession = _session(CONFIRMED_BODY)
    with caplog.at_level("INFO"):
        _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    [line] = [message for message in caplog.messages if message.startswith("form_confirmed")]
    assert "http_status=200 entry_fields=0 fbzx=False form_page=True marker='your response has been recorded'" in line


def test_legacy_confirmation_class_is_not_a_confirmation(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    """Класса freebirdFormviewerViewResponseConfirmationMessage в вёрстке Google больше нет."""
    session: _FakeSession = _session(LEGACY_CLASS_BODY)
    result: FormSendResult = _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    assert (result.confirmed, result.code) == (False, FORM_CODE_NOT_CONFIRMED)


def test_post_asks_for_english_response_and_get_is_untouched(
    tmp_path: Path,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
) -> None:
    session: _FakeSession = _session(CONFIRMED_BODY)
    _sender(session, tmp_path).send(_planned(make_config, make_slot_object))
    [(url, _)] = session.post_calls
    assert url == RESPONSE_URL
    assert session.post_headers == [{"Accept-Language": "en"}]
    assert session.get_calls == [SHORT_URL]                  # страница формы читается как раньше
