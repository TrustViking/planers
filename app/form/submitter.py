"""Отправка ключа в Google-форму (ТЗ §7.5 п.3–5).

Названия вопросов и тексты вариантов — только из пакета и из самой формы, никогда из кода.
Железное правило: значение, которого нет среди вариантов вопроса, не отправляется вовсе.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

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

# Маркеры «ответ записан» в теле ответа — единственный источник (§7.5 п.5).
CONFIRMATION_MARKERS: Final[tuple[str, ...]] = (
    "freebirdFormviewerViewResponseConfirmationMessage",
    "Ваш ответ записан",
    "Your response has been recorded",
)
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
                "form_send_failed slot_id=%s channel=%s code=%s",
                planned.slot_id,
                planned.channel.id,
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
        LOGGER.info(
            "form_post url=%s slot_id=%s pages=%s fields=%d stream_key=%s",
            structure.response_url,
            planned.slot_id,
            PAGE_SEPARATOR.join(str(page) for page in pages),
            len(answers),
            mask_stream_key(planned.stream_key),
        )
        response: HttpResponse = self._post_with_retry(structure.response_url, body)
        if _is_confirmed(response.text):
            LOGGER.info("form_confirmed slot_id=%s channel=%s", planned.slot_id, planned.channel.id)
            return FormSendResult(confirmed=True)
        path: Path | None = self._discovery.save_diagnostic(response.text, planned.form.url, "response")
        raise FormError(FORM_CODE_NOT_CONFIRMED, f"HTTP {response.status_code}", path)

    def _post_with_retry(self, url: str, body: dict[str, list[str]]) -> HttpResponse:
        """3 попытки на сетевые сбои и 5xx; на 4xx повтора нет — он ничего не изменит."""
        last_error: str = ""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response: HttpResponse = self._session.post(url, data=body, timeout=REQUEST_TIMEOUT_SEC)
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
    platform_answer: _Answer | None = _answer_by_options(structure, answers)
    pages: list[int] = [FIRST_PAGE]
    if platform_answer is not None:
        target: int | None = structure.navigation.get(platform_answer.question.entry_id, {}).get(
            platform_answer.value
        )
        if target is not None:
            _append_page(pages, platform_answer.question.page_index)
            _append_page(pages, target)
            LOGGER.info("form_pages_by_navigation pages=%s", pages)
            return pages
    for answer in answers:
        _append_page(pages, answer.question.page_index)
    LOGGER.info("form_pages_by_answers pages=%s", pages)
    return pages


def _answer_by_options(structure: FormStructure, answers: list[_Answer]) -> _Answer | None:
    """Вопрос-развилка — тот, у которого есть переходы по вариантам."""
    for answer in answers:
        if answer.question.entry_id in structure.navigation:
            return answer
    return None


def _append_page(pages: list[int], page: int) -> None:
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


def _is_confirmed(body: str) -> bool:
    return any(marker in body for marker in CONFIRMATION_MARKERS)
