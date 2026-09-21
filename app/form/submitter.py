"""Отправка ключа в Google-форму (ТЗ §7.5 п.3–5).

Формы читаются в начале запуска (prepare) — один раз на форму; правила ответа — у объекта-формы
(app/form/key_form.py::KeyForm), готовые ответы и решение «полные ли» — у объекта-слота
(PlannedBroadcast.form_answers). Здесь — только тело запроса, POST и подтверждение.
Железное правило: значение, которого нет среди вариантов вопроса, не отправляется вовсе.
"""
from __future__ import annotations

import html
import random
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

from app.core.retry import RetryPolicy
from app.form.base import (
    FORM_CODE_NOT_CONFIRMED,
    FORM_CODE_STRUCTURE_UNREADABLE,
    FORM_CODE_TRANSPORT_FAILED,
    FormError,
    FormSendResult,
)
from app.form.discovery import (
    REQUEST_TIMEOUT_SEC,
    TRANSIENT_STATUS_MINIMUM,
    FormDiscovery,
    FormStructure,
    HttpResponse,
    HttpSession,
)
from app.form.key_form import FormAnswers, KeyForm
from app.observability.logging_setup import get_logger, mask_stream_key
from app.observability.run_stats import RunStats
from app.package.model import FormSpec
from app.pipeline.plan import PlannedBroadcast

LOGGER = get_logger("form.submitter")

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
RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()


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


class GoogleFormSender:
    """Один отправитель на запуск: формы читаются в prepare, send берёт готовую KeyForm."""

    def __init__(
        self,
        session: HttpSession,
        discovery: FormDiscovery,
        rng: random.Random | None = None,
        stats: RunStats | None = None,
    ) -> None:
        self._stats: RunStats = stats if stats is not None else RunStats()   # отправки формы: число и секунды
        self._session: HttpSession = session
        self._discovery: FormDiscovery = discovery
        self._rng: random.Random = rng or random.Random()   # добавка к паузам повторов; main передаёт свой
        self._forms: dict[str, KeyForm] = {}
        self._failures: dict[str, FormError] = {}

    def prepare(self, forms: Sequence[FormSpec]) -> None:
        """Каждая уникальная форма запуска читается один раз; не прочиталась — отправка по ней вернёт ту же ошибку."""
        for spec in forms:
            if spec.url in self._forms or spec.url in self._failures:
                continue
            try:
                form: KeyForm = KeyForm.build(spec, self._discovery.structure(spec.url))
            except FormError as error:
                LOGGER.warning("form_unreadable url=%s code=%s message=%s", spec.url, error.code, error.message)
                self._failures[spec.url] = error
                continue
            form.log_ready()
            self._forms[spec.url] = form

    def form_for(self, spec: FormSpec) -> KeyForm:
        """Готовая форма; другой пакет с той же ссылкой — та же структура, свои названия и варианты.

        Не прочиталась — та же FormError, что при чтении; второго чтения нет.
        """
        if spec.url not in self._forms and spec.url not in self._failures:
            self.prepare([spec])
        failure: FormError | None = self._failures.get(spec.url)
        if failure is not None:
            raise failure
        form: KeyForm = self._forms[spec.url]
        return form if form.spec == spec else form.for_spec(spec)

    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        """Ответы не строятся здесь: берутся готовые у объекта (key_form, form_answers)."""
        try:
            form, answers = _ready_answers(planned)
            return self._post(planned, form.structure, answers)
        except FormError as error:
            LOGGER.warning(
                'form_send_failed slot_id=%s channel="%s" handle=%s code=%s',
                planned.slot_id,
                planned.channel.account_name,
                planned.channel.handle,
                error.code,
            )
            return error.as_result()

    def _post(self, planned: PlannedBroadcast, structure: FormStructure, answers: FormAnswers) -> FormSendResult:
        body: dict[str, list[str]] = build_body(structure, answers)
        url: str = with_response_language(structure.response_url)
        LOGGER.info(
            "form_post url=%s slot_id=%s pages=%s fields=%d stream_key=%s",
            url,
            planned.slot_id,
            PAGE_SEPARATOR.join(str(page) for page in answers.pages),
            len(answers.answers),
            mask_stream_key(planned.stream_key),
        )
        response: HttpResponse = self._post_with_retry(url, body)
        confirmation: Confirmation = read_confirmation(response.status_code, response.text)
        if confirmation.is_confirmed:
            LOGGER.info(
                'form_confirmed slot_id=%s channel="%s" handle=%s %s',
                planned.slot_id,
                planned.channel.account_name,
                planned.channel.handle,
                confirmation.log_fields(),
            )
            return FormSendResult(confirmed=True)
        path: Path | None = self._discovery.save_diagnostic(response.text, planned.form.url, "response")
        LOGGER.warning(
            'form_not_confirmed slot_id=%s channel="%s" handle=%s %s page_title=%r saved=%s',
            planned.slot_id,
            planned.channel.account_name,
            planned.channel.handle,
            confirmation.log_fields(),
            page_title(response.text),
            path,
        )
        raise FormError(FORM_CODE_NOT_CONFIRMED, f"HTTP {response.status_code}", path)

    def _post_with_retry(self, url: str, body: dict[str, list[str]]) -> HttpResponse:
        """Сетевые сбои и 5xx повторяются по RETRY_POLICY; на 4xx повтора нет — он ничего не изменит."""
        retry_number: int = 0
        while True:
            try:
                response: HttpResponse = self._timed_post(url, body)
            except OSError as error:
                reason: str = str(error)
            else:
                if response.status_code < TRANSIENT_STATUS_MINIMUM:
                    return response
                reason = f"HTTP {response.status_code}"
            retry_number += 1
            if not RETRY_POLICY.has_retry_left(retry_number):
                raise FormError(FORM_CODE_TRANSPORT_FAILED, reason)
            delay_sec: float = RETRY_POLICY.delay_sec(retry_number, self._rng)
            LOGGER.warning(
                "form_post_retry retry=%d/%d delay_sec=%.2f reason=%s",
                retry_number,
                RETRY_POLICY.max_retries,
                delay_sec,
                reason,
            )
            time.sleep(delay_sec)   # через модуль time: тесты подменяют

    def _timed_post(self, url: str, body: dict[str, list[str]]) -> HttpResponse:
        """Один POST ответа формы; попытка и её секунды — в статистику при любом исходе."""
        started: float = self._stats.now()
        try:
            return self._session.post(
                url,
                data=body,
                timeout=REQUEST_TIMEOUT_SEC,
                headers={HEADER_ACCEPT_LANGUAGE: RESPONSE_LANGUAGE},
            )
        finally:
            self._stats.form_posted(self._stats.now() - started)


def _ready_answers(planned: PlannedBroadcast) -> tuple[KeyForm, FormAnswers]:
    """Защита: в штатном пути объект сюда приходит с полными ответами (PlannedBroadcast.is_key_ready_to_send)."""
    if planned.key_form is None or planned.form_answers is None:
        raise planned.form_failure or FormError(FORM_CODE_STRUCTURE_UNREADABLE, planned.form.url)
    error: FormError | None = planned.form_answers.error()
    if error is not None:
        raise error
    return planned.key_form, planned.form_answers


def build_body(structure: FormStructure, answers: FormAnswers) -> dict[str, list[str]]:
    """Тело POST formResponse: ответы пройденных разделов, fvv, pageHistory, fbzx."""
    body: dict[str, list[str]] = {answer.question.entry_id: [answer.value] for answer in answers.answers}
    body[FIELD_FVV] = [FVV_VALUE]
    body[FIELD_PAGE_HISTORY] = [PAGE_SEPARATOR.join(str(page) for page in answers.pages)]
    if structure.fbzx:
        body[FIELD_FBZX] = [structure.fbzx]
    return body


def with_response_language(url: str) -> str:
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


def page_title(body: str) -> str:
    """Заголовок страницы ответа — в лог, чтобы разбор не требовал открывать сохранённый файл."""
    match: re.Match[str] | None = TITLE_PATTERN.search(body)
    if match is None:
        return ""
    return " ".join(html.unescape(match.group(1)).split())
