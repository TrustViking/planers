"""Чтение структуры Google-формы (ТЗ §7.5 п.1–2): вопросы, entry-ID, варианты, разделы.

entry-ID нигде не конфигурируются: форма сама говорит, что у неё есть. Структура живёт
в скрипте FB_PUBLIC_LOAD_DATA_ на странице viewform. Точная раскладка этого массива —
открытая **[ПРОВЕРИТЬ]** в ТЗ §12, поэтому разбор нарочно терпимый: чего не понял —
считает отсутствующим, а при неудаче сохраняет HTML, иначе проверку нечем закрыть.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Final, Protocol

from app.core.dates import FILE_STAMP_FORMAT
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
class FormStructure:
    view_url: str                 # конечный адрес страницы после редиректа forms.gle
    response_url: str             # тот же адрес с formResponse вместо viewform
    fbzx: str
    questions: tuple[FormQuestion, ...]
    # entry_id вопроса → текст варианта → индекс раздела, на который ведёт вариант
    navigation: dict[str, dict[str, int]]

    def question_by_title(self, title: str) -> FormQuestion | None:
        for question in self.questions:
            if question.title == title:
                return question
        return None

    @property
    def page_count(self) -> int:
        return max((question.page_index for question in self.questions), default=0) + 1


class HttpResponse(Protocol):
    status_code: int
    text: str
    url: str


class HttpSession(Protocol):
    def get(self, url: str, timeout: float, allow_redirects: bool) -> HttpResponse:
        ...

    def post(self, url: str, data: dict[str, list[str]], timeout: float) -> HttpResponse:
        ...


class FormDiscovery:
    """Одно чтение на каждый уникальный form.url запуска: два пакета могут вести в разные формы."""

    def __init__(self, session: HttpSession, logs_dir: Path, now: datetime) -> None:
        self._session: HttpSession = session
        self._logs_dir: Path = logs_dir
        self._now: datetime = now
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
        try:
            response: HttpResponse = self._session.get(
                form_url,
                timeout=REQUEST_TIMEOUT_SEC,
                allow_redirects=True,
            )
        except OSError as error:
            raise FormError(FORM_CODE_TRANSPORT_FAILED, str(error)) from error
        if response.status_code != 200:
            raise FormError(FORM_CODE_TRANSPORT_FAILED, f"HTTP {response.status_code}")
        return response.text, response.url

    def _parse(self, html: str, final_url: str, form_url: str) -> FormStructure:
        payload: Any = _load_script(html)
        if payload is None:
            raise FormError(
                FORM_CODE_STRUCTURE_UNREADABLE,
                "FB_PUBLIC_LOAD_DATA_ not found or not JSON",
                self.save_diagnostic(html, form_url, "page"),
            )
        questions, navigation = _read_items(payload)
        if not questions:
            raise FormError(
                FORM_CODE_STRUCTURE_UNREADABLE,
                "no questions in FB_PUBLIC_LOAD_DATA_",
                self.save_diagnostic(html, form_url, "page"),
            )
        return FormStructure(
            view_url=final_url,
            response_url=_response_url(final_url),
            fbzx=_read_fbzx(html, payload),
            questions=tuple(questions),
            navigation=navigation,
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


def _read_items(payload: Any) -> tuple[list[FormQuestion], dict[str, dict[str, int]]]:
    """payload[1][1] — список элементов формы; разделы считаются по разрывам страниц."""
    items: Any = _dig(payload, 1, 1)
    if not isinstance(items, list):
        return [], {}
    questions: list[FormQuestion] = []
    navigation: dict[str, dict[str, int]] = {}
    page_index: int = 0
    for item in items:
        if not isinstance(item, list) or len(item) < 4:
            continue
        if item[3] == PAGE_BREAK_TYPE:
            page_index += 1
            continue
        question: FormQuestion | None = _read_question(item, page_index)
        if question is None:
            continue
        questions.append(question)
        targets: dict[str, int] = _read_navigation(item)
        if targets:
            navigation[question.entry_id] = targets
    return questions, navigation


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


def _read_navigation(item: list[Any]) -> dict[str, int]:
    """Переход «вариант → раздел», если он у варианта указан; иначе вариант остаётся без цели."""
    entries: Any = item[4] if len(item) > 4 else None
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], list):
        return {}
    raw_options: Any = entries[0][1] if len(entries[0]) > 1 else None
    if not isinstance(raw_options, list):
        return {}
    targets: dict[str, int] = {}
    for option in raw_options:
        if not isinstance(option, list) or not option or option[0] is None:
            continue
        target: Any = option[2] if len(option) > 2 else None
        if isinstance(target, int) and target >= 0:
            targets[str(option[0])] = target
    return targets


def _dig(payload: Any, *path: int) -> Any:
    current: Any = payload
    for index in path:
        if not isinstance(current, list) or len(current) <= index:
            return None
        current = current[index]
    return current
