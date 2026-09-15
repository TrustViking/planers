"""Отправка ключа в Google-форму (ТЗ §7.5 п.3–5).

Названия вопросов и тексты вариантов — только из пакета и из самой формы, никогда из кода.
Железное правило: значение, которого нет среди вариантов вопроса, не отправляется вовсе.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

from app.form.base import (
    FORM_CODE_MISSING_OPTION,
    FORM_CODE_NOT_CONFIRMED,
    FORM_CODE_REQUIRED_MISSING,
    FORM_CODE_TRANSPORT_FAILED,
    FormError,
    FormSendResult,
)
from app.form.discovery import (
    REQUEST_TIMEOUT_SEC,
    FormDiscovery,
    FormQuestion,
    FormStructure,
    HttpResponse,
    HttpSession,
    QuestionKind,
    SectionJump,
)
from app.observability.logging_setup import get_logger, mask_stream_key
from app.package.model import FormSpec
from app.pipeline.plan import PlannedBroadcast

LOGGER = get_logger("form.submitter")

# Поля пакета (form.fields) и откуда берётся значение каждого.
FIELD_LANGUAGE: Final[str] = "language"
FIELD_ACCOUNT_NAME: Final[str] = "account_name"
FIELD_DATE: Final[str] = "date"
FIELD_PLATFORM: Final[str] = "platform"
FIELD_STREAM_KEY: Final[str] = "stream_key"
FIELD_STREAM_URL: Final[str] = "stream_url"
PLATFORM_CODE: Final[str] = "youtube"

# Подтверждение отправки (§7.5 п.5) — структурное, от языка страницы не зависит. Опыт 13-09-2026 16:27
# (app/tools/form_probe.py, страницы — app/tests/data/): страница успеха — заглушка без полей формы,
# в ней нет ни одного entry.<цифры> и нет fbzx; страница отказа — перерисованный раздел формы с полем
# entry.<цифры> и скрытым fbzx (иначе её нельзя было бы дозаполнить), пришла с HTTP 400.
# Успех = HTTP 200 + страница Google Forms (FB_PUBLIC_LOAD_DATA_) + ни entry.<цифры>, ни fbzx:
# пустой или чужой ответ 200 доставкой ключа не считается.
CONFIRMED_HTTP_STATUS: Final[int] = 200
ENTRY_FIELD_PATTERN: Final[re.Pattern[str]] = re.compile(r"entry\.\d+")
FORM_PAGE_MARKER: Final[str] = "FB_PUBLIC_LOAD_DATA_"
# Текстовые маркеры «ответ записан» — только сигнал в лог рядом со структурным признаком: решение они
# не принимают, язык страницы Google выбирает сам. Сравнение — по вхождению в нижнем регистре, без точки.
CONFIRMATION_MARKERS: Final[tuple[str, ...]] = (
    "your response has been recorded",   # опыт 16:27: с hl=en все семь страниц пришли на английском
    "відповідь було записано",           # живой прогон 03:01 (POST ещё без hl=en): «Вашу відповідь було записано.»
    "ответ записан",                     # русская страница Google: «Ваш ответ записан.»
)
# Язык страницы ответа просим английский: hl=en и Accept-Language безвредны, но гарантии Google не даёт —
# в 03:01 (без них) страница пришла на украинском по IP, в опыте 16:27 (с ними) — на английском.
# Поэтому подтверждение от языка не зависит.
RESPONSE_LANGUAGE: Final[str] = "en"
HEADER_ACCEPT_LANGUAGE: Final[str] = "Accept-Language"
QUERY_LANGUAGE: Final[str] = "hl"
QUERY_SEPARATOR: Final[str] = "?"
QUERY_JOINER: Final[str] = "&"
TITLE_PATTERN: Final[re.Pattern[str]] = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
FIELD_FVV: Final[str] = "fvv"
FIELD_PAGE_HISTORY: Final[str] = "pageHistory"
FIELD_FBZX: Final[str] = "fbzx"
FVV_VALUE: Final[str] = "1"
PAGE_SEPARATOR: Final[str] = ","
FIRST_PAGE: Final[int] = 0
RETRY_ATTEMPTS: Final[int] = 3
RETRY_BASE_DELAY_SEC: Final[float] = 1.0
TRANSIENT_STATUS_MINIMUM: Final[int] = 500


@dataclass(frozen=True)
class Confirmation:
    """Оба сигнала страницы ответа: решает структурный, текстовый маркер — только в лог."""

    is_confirmed: bool
    http_status: int
    entry_fields: int          # вхождений entry.<цифры>: у страницы отказа есть, у успеха нет
    has_fbzx: bool
    is_form_page: bool         # это страница Google Forms, а не пустой или чужой ответ
    marker: str | None         # какой из CONFIRMATION_MARKERS совпал; None — ни один

    def log_fields(self) -> str:
        return (
            f"http_status={self.http_status} entry_fields={self.entry_fields} fbzx={self.has_fbzx} "
            f"form_page={self.is_form_page} marker={self.marker!r}"
        )


@dataclass(frozen=True)
class _Answer:
    question: FormQuestion
    value: str


class GoogleFormSender:
    """Один отправитель на запуск: структуры форм кэшируются внутри FormDiscovery."""

    def __init__(self, session: HttpSession, discovery: FormDiscovery) -> None:
        self._session: HttpSession = session
        self._discovery: FormDiscovery = discovery

    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        try:
            structure: FormStructure = self._discovery.structure(planned.form.url)
            answers: list[_Answer] = _collect_answers(planned, structure)
            pages: list[int] = _visited_pages(structure, answers)
            _check_required(structure, answers, pages)
            return self._post(planned, structure, answers, pages)
        except FormError as error:
            LOGGER.warning(
                'form_send_failed slot_id=%s channel="%s" code=%s',
                planned.slot_id,
                planned.channel.account_name,
                error.code,
            )
            return error.as_result()

    def _post(
        self,
        planned: PlannedBroadcast,
        structure: FormStructure,
        answers: list[_Answer],
        pages: list[int],
    ) -> FormSendResult:
        body: dict[str, list[str]] = _build_body(structure, answers, pages)
        url: str = _with_response_language(structure.response_url)
        LOGGER.info(
            "form_post url=%s slot_id=%s pages=%s fields=%d stream_key=%s",
            url,
            planned.slot_id,
            PAGE_SEPARATOR.join(str(page) for page in pages),
            len(answers),
            mask_stream_key(planned.stream_key),
        )
        response: HttpResponse = self._post_with_retry(url, body)
        confirmation: Confirmation = read_confirmation(response.status_code, response.text)
        if confirmation.is_confirmed:
            LOGGER.info(
                'form_confirmed slot_id=%s channel="%s" %s',
                planned.slot_id,
                planned.channel.account_name,
                confirmation.log_fields(),
            )
            return FormSendResult(confirmed=True)
        path: Path | None = self._discovery.save_diagnostic(response.text, planned.form.url, "response")
        LOGGER.warning(
            'form_not_confirmed slot_id=%s channel="%s" %s page_title=%r saved=%s',
            planned.slot_id,
            planned.channel.account_name,
            confirmation.log_fields(),
            _page_title(response.text),
            path,
        )
        raise FormError(FORM_CODE_NOT_CONFIRMED, f"HTTP {response.status_code}", path)

    def _post_with_retry(self, url: str, body: dict[str, list[str]]) -> HttpResponse:
        """3 попытки на сетевые сбои и 5xx; на 4xx повтора нет — он ничего не изменит."""
        last_error: str = ""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response: HttpResponse = self._session.post(
                    url,
                    data=body,
                    timeout=REQUEST_TIMEOUT_SEC,
                    headers={HEADER_ACCEPT_LANGUAGE: RESPONSE_LANGUAGE},
                )
            except OSError as error:
                last_error = str(error)
            else:
                if response.status_code < TRANSIENT_STATUS_MINIMUM:
                    return response
                last_error = f"HTTP {response.status_code}"
            if attempt >= RETRY_ATTEMPTS:
                break
            delay_sec: float = RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
            LOGGER.warning(
                "form_post_retry attempt=%d/%d delay_sec=%.1f reason=%s",
                attempt,
                RETRY_ATTEMPTS,
                delay_sec,
                last_error,
            )
            time.sleep(delay_sec)
        raise FormError(FORM_CODE_TRANSPORT_FAILED, last_error)


def _collect_answers(planned: PlannedBroadcast, structure: FormStructure) -> list[_Answer]:
    form: FormSpec = planned.form
    answers: list[_Answer] = []
    for field, title in form.fields.items():
        if title is None:       # такого вопроса в форме нет: поле не отправляется (§5.1)
            continue
        question: FormQuestion | None = structure.question_by_title(title)
        if question is None:
            continue            # вопрос из пакета в форме не найден: обязательность проверит _check_required
        value: str | None = _value_for(field, planned, question)
        if value is None:
            continue
        answers.append(_Answer(question=question, value=value))
    return answers


def _value_for(field: str, planned: PlannedBroadcast, question: FormQuestion) -> str | None:
    form: FormSpec = planned.form
    if field == FIELD_ACCOUNT_NAME:
        return planned.account_name
    if field == FIELD_STREAM_KEY:
        return planned.stream_key
    if field == FIELD_LANGUAGE:
        return _option_by_text(question, form.values.get(FIELD_LANGUAGE, {}).get(planned.language))
    if field == FIELD_PLATFORM:
        return _option_by_text(question, form.values.get(FIELD_PLATFORM, {}).get(PLATFORM_CODE))
    if field == FIELD_DATE:
        return _option_by_date(question, planned.slot.start, form.date_format)
    if field == FIELD_STREAM_URL:
        return _option_by_url(question, planned.stream_url)
    return None


def _option_by_text(question: FormQuestion, wanted: str | None) -> str:
    if wanted is None:
        raise FormError(FORM_CODE_MISSING_OPTION, _missing(question, "-"))
    if question.kind is QuestionKind.TEXT:
        return wanted
    if wanted not in question.options:
        raise FormError(FORM_CODE_MISSING_OPTION, _missing(question, wanted))
    return wanted


def _option_by_date(question: FormQuestion, start: datetime, date_format: str) -> str:
    """Вариант «13.09.2026 Дата стрима …» опознаётся по началу текста (§7.5 п.3)."""
    wanted: str = start.strftime(date_format)
    if question.kind is QuestionKind.TEXT:
        return wanted
    for option in question.options:
        if option.startswith(wanted):
            return option
    raise FormError(FORM_CODE_MISSING_OPTION, _missing(question, wanted))


def _option_by_url(question: FormQuestion, stream_url: str | None) -> str:
    """Сравнение нормализованное, отправляется текст варианта как он есть в форме."""
    if not stream_url:
        raise FormError(FORM_CODE_MISSING_OPTION, _missing(question, "-"))
    if question.kind is QuestionKind.TEXT:
        return stream_url
    wanted: str = _normalize_url(stream_url)
    for option in question.options:
        if _normalize_url(option) == wanted:
            return option
    raise FormError(FORM_CODE_MISSING_OPTION, _missing(question, stream_url))


def _normalize_url(value: str) -> str:
    return value.strip().rstrip("/").lower()


def _missing(question: FormQuestion, wanted: str) -> str:
    return f"{question.title}: {wanted}"


def _visited_pages(structure: FormStructure, answers: list[_Answer]) -> list[int]:
    """Раздел вопроса «Платформа» и раздел, куда ведёт выбранный вариант (§7.5 п.4)."""
    fork: _Answer | None = _answer_by_options(structure, answers)
    jump: SectionJump | None = _jump_for(structure, fork)
    pages: list[int] = [FIRST_PAGE]
    if fork is not None and jump is not None and _is_page_in_range(jump.page_index, structure):
        _append_page(pages, fork.question.page_index, structure)
        _append_page(pages, jump.page_index, structure)
        LOGGER.info(
            "form_pages_by_navigation pages=%s entry=%s option=%r section_id=%d page=%s page_count=%d",
            pages,
            fork.question.entry_id,
            fork.value,
            jump.section_id,
            jump.page_index,
            structure.page_count,
        )
        return pages
    for answer in answers:
        _append_page(pages, answer.question.page_index, structure)
    LOGGER.info(
        "form_pages_by_answers pages=%s section_id=%s page=%s page_count=%d",
        pages,
        jump.section_id if jump is not None else None,
        jump.page_index if jump is not None else None,
        structure.page_count,
    )
    return pages


def _answer_by_options(structure: FormStructure, answers: list[_Answer]) -> _Answer | None:
    """Вопрос-развилка — тот, у которого есть переходы по вариантам."""
    for answer in answers:
        if answer.question.entry_id in structure.navigation:
            return answer
    return None


def _jump_for(structure: FormStructure, fork: _Answer | None) -> SectionJump | None:
    if fork is None:
        return None
    return structure.navigation.get(fork.question.entry_id, {}).get(fork.value)


def _is_page_in_range(page: int | None, structure: FormStructure) -> bool:
    """pageHistory принимает только номера существующих разделов: всё прочее Google отвергает 400."""
    return page is not None and 0 <= page < structure.page_count


def _append_page(pages: list[int], page: int, structure: FormStructure) -> None:
    if not _is_page_in_range(page, structure):
        LOGGER.warning("form_page_out_of_range page=%s page_count=%d", page, structure.page_count)
        return
    if page not in pages:
        pages.append(page)


def _check_required(structure: FormStructure, answers: list[_Answer], pages: list[int]) -> None:
    """Обязательные вопросы непройденных разделов (Facebook, Rumble) не проверяются."""
    answered: set[str] = {answer.question.entry_id for answer in answers}
    missing: list[str] = [
        question.title
        for question in structure.questions
        if question.is_required and question.page_index in pages and question.entry_id not in answered
    ]
    if missing:
        raise FormError(FORM_CODE_REQUIRED_MISSING, ", ".join(missing))


def _build_body(
    structure: FormStructure,
    answers: list[_Answer],
    pages: list[int],
) -> dict[str, list[str]]:
    body: dict[str, list[str]] = {answer.question.entry_id: [answer.value] for answer in answers}
    body[FIELD_FVV] = [FVV_VALUE]
    body[FIELD_PAGE_HISTORY] = [PAGE_SEPARATOR.join(str(page) for page in pages)]
    if structure.fbzx:
        body[FIELD_FBZX] = [structure.fbzx]
    return body


def _with_response_language(url: str) -> str:
    """hl=en в строке запроса — вместе с Accept-Language просит английскую страницу ответа; гарантии нет (§7.5 п.5)."""
    separator: str = QUERY_JOINER if QUERY_SEPARATOR in url else QUERY_SEPARATOR
    return url + separator + urlencode({QUERY_LANGUAGE: RESPONSE_LANGUAGE})


def read_confirmation(http_status: int, body: str) -> Confirmation:
    """Записан ли ответ (§7.5 п.5): по структуре страницы, а не по её языку."""
    entry_fields: int = len(ENTRY_FIELD_PATTERN.findall(body))
    has_fbzx: bool = FIELD_FBZX in body
    is_form_page: bool = FORM_PAGE_MARKER in body
    lowered: str = body.lower()
    return Confirmation(
        is_confirmed=http_status == CONFIRMED_HTTP_STATUS and is_form_page and entry_fields == 0 and not has_fbzx,
        http_status=http_status,
        entry_fields=entry_fields,
        has_fbzx=has_fbzx,
        is_form_page=is_form_page,
        marker=next((marker for marker in CONFIRMATION_MARKERS if marker in lowered), None),
    )


def _page_title(body: str) -> str:
    """Заголовок страницы ответа — в лог, чтобы разбор не требовал открывать сохранённый файл."""
    match: re.Match[str] | None = TITLE_PATTERN.search(body)
    if match is None:
        return ""
    return " ".join(html.unescape(match.group(1)).split())
