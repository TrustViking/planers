"""Чтение структуры Google-формы (ТЗ §7.5 п.1–2): вопросы, entry-ID, варианты, разделы.

entry-ID нигде не конфигурируются: форма сама говорит, что у неё есть. Структура живёт
в скрипте FB_PUBLIC_LOAD_DATA_ на странице viewform; раскладка подтверждена живым прогоном
13-09-2026 (ТЗ §12), но разбор всё равно терпимый: чего не понял — считает отсутствующим,
а при неудаче сохраняет HTML для разбора.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Final, Protocol

from app.core.dates import FILE_STAMP_FORMAT
from app.core.retry import RetryPolicy
from app.form.base import FORM_CODE_STRUCTURE_UNREADABLE, FORM_CODE_TRANSPORT_FAILED, FormError
from app.observability.logging_setup import get_logger

LOGGER = get_logger("form.discovery")

SCRIPT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\])\s*;\s*</script>",
    re.DOTALL,
)
FBZX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'name="fbzx"\s+value="(-?\d+)"',
)
VIEW_FORM_SUFFIX: Final[str] = "viewform"
RESPONSE_SUFFIX: Final[str] = "formResponse"
ENTRY_TEMPLATE: Final[str] = "entry.{entry_id}"
PAGE_BREAK_TYPE: Final[int] = 8
TEXT_TYPES: Final[frozenset[int]] = frozenset({0, 1})    # короткий ответ и абзац
CHOICE_TYPES: Final[frozenset[int]] = frozenset({2, 3, 4})  # радио, список, флажки
HTML_ENCODING: Final[str] = "utf-8"
URL_HASH_CHARS: Final[int] = 8
DIAGNOSTIC_TEMPLATE: Final[str] = "{stamp}_form_{digest}.html"
REQUEST_TIMEOUT_SEC: Final[float] = 30.0
HTTP_OK: Final[int] = 200
TRANSIENT_STATUS_MINIMUM: Final[int] = 500   # 5xx — сбой на стороне Google: повторяем
RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()


class QuestionKind(str, Enum):
    TEXT = "text"
    RADIO = "radio"


@dataclass(frozen=True)
class FormQuestion:
    title: str                    # название вопроса, как его видит владелец
    entry_id: str                 # "entry.NNN"
    is_required: bool
    kind: QuestionKind
    options: tuple[str, ...]      # тексты вариантов; для TEXT пусто
    page_index: int               # номер раздела, с нуля


@dataclass(frozen=True)
class SectionJump:
    """Переход варианта: в форме он задан идентификатором раздела, а pageHistory ждёт номер (§7.5 п.2)."""

    section_id: int               # как записано у варианта: item[0] разрыва, открывающего раздел
    page_index: int | None        # номер этого раздела с нуля; None — такого разрыва в форме нет


@dataclass(frozen=True)
class FormStructure:
    view_url: str                 # конечный адрес страницы после редиректа forms.gle
    response_url: str             # тот же адрес с formResponse вместо viewform
    fbzx: str
    questions: tuple[FormQuestion, ...]
    # entry_id вопроса → текст варианта → переход на раздел
    navigation: dict[str, dict[str, SectionJump]]
    page_count: int               # разрывов страниц + 1

    def question_by_title(self, title: str) -> FormQuestion | None:
        for question in self.questions:
            if question.title == title:
                return question
        return None


class HttpResponse(Protocol):
    status_code: int
    text: str
    url: str


class HttpSession(Protocol):
    def get(self, url: str, timeout: float, allow_redirects: bool) -> HttpResponse:
        ...

    def post(
        self,
        url: str,
        data: dict[str, list[str]],
        timeout: float,
        headers: Mapping[str, str],
    ) -> HttpResponse:
        ...


class FormDiscovery:
    """Одно чтение на каждый уникальный form.url запуска: два пакета могут вести в разные формы."""

    def __init__(
        self,
        session: HttpSession,
        logs_dir: Path,
        now: datetime,
        rng: random.Random | None = None,
    ) -> None:
        self._session: HttpSession = session
        self._logs_dir: Path = logs_dir
        self._now: datetime = now
        self._rng: random.Random = rng or random.Random()   # добавка к паузам повторов; main передаёт свой
        self._cache: dict[str, FormStructure] = {}

    def structure(self, form_url: str) -> FormStructure:
        cached: FormStructure | None = self._cache.get(form_url)
        if cached is not None:
            return cached
        html, final_url = self._download(form_url)
        structure: FormStructure = self._parse(html, final_url, form_url)
        self._cache[form_url] = structure
        LOGGER.info(
            "form_structure_read url=%s questions=%d pages=%d",
            structure.view_url,
            len(structure.questions),
            structure.page_count,
        )
        return structure

    def save_diagnostic(self, body: str, form_url: str, kind: str) -> Path | None:
        """HTML в logs\\ (инвариант 9): без него открытые [ПРОВЕРИТЬ] §12 нечем закрыть."""
        name: str = DIAGNOSTIC_TEMPLATE.format(
            stamp=self._now.strftime(FILE_STAMP_FORMAT),
            digest=f"{kind}_{_short_hash(form_url)}",
        )
        path: Path = self._logs_dir / name
        try:
            self._logs_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding=HTML_ENCODING)
        except OSError as error:
            LOGGER.warning("form_diagnostic_not_saved path=%s reason=%s", path, error)
            return None
        LOGGER.info("form_diagnostic_saved path=%s", path)
        return path

    def _download(self, form_url: str) -> tuple[str, str]:
        """Сетевые сбои и 5xx повторяются по RETRY_POLICY; прочий ответ не 200 — сразу отказ."""
        retry_number: int = 0
        while True:
            try:
                response: HttpResponse = self._session.get(
                    form_url,
                    timeout=REQUEST_TIMEOUT_SEC,
                    allow_redirects=True,
                )
            except OSError as error:
                reason: str = str(error)
            else:
                if response.status_code == HTTP_OK:
                    return response.text, response.url
                if response.status_code < TRANSIENT_STATUS_MINIMUM:
                    raise FormError(FORM_CODE_TRANSPORT_FAILED, f"HTTP {response.status_code}")
                reason = f"HTTP {response.status_code}"
            retry_number += 1
            if not RETRY_POLICY.has_retry_left(retry_number):
                raise FormError(FORM_CODE_TRANSPORT_FAILED, reason)
            delay_sec: float = RETRY_POLICY.delay_sec(retry_number, self._rng)
            LOGGER.warning(
                "form_get_retry url=%s retry=%d/%d delay_sec=%.2f reason=%s",
                form_url,
                retry_number,
                RETRY_POLICY.max_retries,
                delay_sec,
                reason,
            )
            time.sleep(delay_sec)   # через модуль time: тесты подменяют

    def _parse(self, html: str, final_url: str, form_url: str) -> FormStructure:
        payload: Any = _load_script(html)
        if payload is None:
            raise FormError(
                FORM_CODE_STRUCTURE_UNREADABLE,
                "FB_PUBLIC_LOAD_DATA_ not found or not JSON",
                self.save_diagnostic(html, form_url, "page"),
            )
        parsed: _ParsedItems = _read_items(payload)
        if not parsed.questions:
            raise FormError(
                FORM_CODE_STRUCTURE_UNREADABLE,
                "no questions in FB_PUBLIC_LOAD_DATA_",
                self.save_diagnostic(html, form_url, "page"),
            )
        return FormStructure(
            view_url=final_url,
            response_url=_response_url(final_url),
            fbzx=_read_fbzx(html, payload),
            questions=tuple(parsed.questions),
            navigation=parsed.navigation,
            page_count=parsed.page_count,
        )


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode(HTML_ENCODING)).hexdigest()[:URL_HASH_CHARS]


def _response_url(view_url: str) -> str:
    base: str = view_url.split("?", 1)[0]
    if base.endswith(VIEW_FORM_SUFFIX):
        return base[: -len(VIEW_FORM_SUFFIX)] + RESPONSE_SUFFIX
    return base.rstrip("/") + "/" + RESPONSE_SUFFIX


def _load_script(html: str) -> Any:
    match: re.Match[str] | None = SCRIPT_PATTERN.search(html)
    if match is None:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _read_fbzx(html: str, payload: Any) -> str:
    """Скрытое поле страницы; если его нет — то же значение лежит в самом массиве."""
    match: re.Match[str] | None = FBZX_PATTERN.search(html)
    if match is not None:
        return match.group(1)
    if isinstance(payload, list) and len(payload) > 3 and isinstance(payload[3], (str, int)):
        return str(payload[3])
    return ""


@dataclass(frozen=True)
class _ParsedItems:
    questions: list[FormQuestion]
    navigation: dict[str, dict[str, SectionJump]]
    page_count: int


def _read_items(payload: Any) -> _ParsedItems:
    """payload[1][1] — список элементов формы; разделы считаются по разрывам страниц."""
    items: Any = _dig(payload, 1, 1)
    if not isinstance(items, list):
        return _ParsedItems(questions=[], navigation={}, page_count=1)
    section_ids: dict[int, int] = _read_section_ids(items)
    questions: list[FormQuestion] = []
    navigation: dict[str, dict[str, SectionJump]] = {}
    page_index: int = 0
    for item in items:
        if not _is_form_item(item):
            continue
        if item[3] == PAGE_BREAK_TYPE:
            page_index += 1
            continue
        question: FormQuestion | None = _read_question(item, page_index)
        if question is None:
            continue
        questions.append(question)
        jumps: dict[str, SectionJump] = _read_navigation(item, section_ids)
        if jumps:
            navigation[question.entry_id] = jumps
    return _ParsedItems(questions=questions, navigation=navigation, page_count=page_index + 1)


def _read_section_ids(items: list[Any]) -> dict[int, int]:
    """Идентификатор разрыва (item[0]) → номер раздела, который он открывает; раздел 0 — до первого разрыва.

    Отдельным проходом: переход у варианта может вести на раздел, разрыв которого ещё впереди.
    """
    section_ids: dict[int, int] = {}
    page_index: int = 0
    for item in items:
        if not _is_form_item(item) or item[3] != PAGE_BREAK_TYPE:
            continue
        page_index += 1
        if _is_plain_int(item[0]):
            section_ids[item[0]] = page_index
    return section_ids


def _is_form_item(item: Any) -> bool:
    return isinstance(item, list) and len(item) >= 4


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_question(item: list[Any], page_index: int) -> FormQuestion | None:
    type_code: Any = item[3]
    entries: Any = item[4] if len(item) > 4 else None
    if not isinstance(type_code, int) or not isinstance(entries, list) or not entries:
        return None
    entry: Any = entries[0]
    if not isinstance(entry, list) or not entry or not isinstance(entry[0], int):
        return None
    if type_code in TEXT_TYPES:
        kind: QuestionKind = QuestionKind.TEXT
    elif type_code in CHOICE_TYPES:
        kind = QuestionKind.RADIO
    else:
        return None
    return FormQuestion(
        title=str(item[1] or ""),
        entry_id=ENTRY_TEMPLATE.format(entry_id=entry[0]),
        is_required=bool(entry[2]) if len(entry) > 2 else False,
        kind=kind,
        options=_read_options(entry),
        page_index=page_index,
    )


def _read_options(entry: list[Any]) -> tuple[str, ...]:
    raw: Any = entry[1] if len(entry) > 1 else None
    if not isinstance(raw, list):
        return ()
    return tuple(str(option[0]) for option in raw if isinstance(option, list) and option and option[0] is not None)


def _read_navigation(item: list[Any], section_ids: dict[int, int]) -> dict[str, SectionJump]:
    """Переход «вариант → раздел», если он у варианта указан; иначе вариант остаётся без цели."""
    entries: Any = item[4] if len(item) > 4 else None
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], list):
        return {}
    raw_options: Any = entries[0][1] if len(entries[0]) > 1 else None
    if not isinstance(raw_options, list):
        return {}
    jumps: dict[str, SectionJump] = {}
    for option in raw_options:
        if not isinstance(option, list) or not option or option[0] is None:
            continue
        target: Any = option[2] if len(option) > 2 else None
        # Отрицательные значения — служебные коды Google («следующий раздел», «отправить форму»),
        # а не идентификаторы разделов: перехода на конкретный раздел в них нет.
        if not _is_plain_int(target) or target < 0:
            continue
        jump: SectionJump = SectionJump(section_id=target, page_index=section_ids.get(target))
        if jump.page_index is None:
            LOGGER.warning(
                "form_jump_unknown_section entry=%s option=%r section_id=%d",
                ENTRY_TEMPLATE.format(entry_id=entries[0][0]),
                option[0],
                target,
            )
        jumps[str(option[0])] = jump
    return jumps


def _dig(payload: Any, *path: int) -> Any:
    current: Any = payload
    for index in path:
        if not isinstance(current, list) or len(current) <= index:
            return None
        current = current[index]
    return current
