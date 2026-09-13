"""Пробник Google-формы (задача 4f): подтверждение отправки и поведение при повторах.

Запуск из корня репо: python -m app.tools.form_probe
Опыт E, закрытая форма: python -m app.tools.form_probe --closed-form — только чтение структуры, ни одной отправки.

Только тренировочная форма: адрес берётся из манифеста самого свежего пакета в promo\\, а после
редиректа идентификатор формы сверяется с TRAINING_FORM_ID — при несовпадении ничего не отправляется.
Структура формы читается production-кодом (FormDiscovery), тело и разделы собираются production-функциями
submitter; постит пробник сам, потому что ему нужно сырое тело ответа. Пишет только в logs\\.
К YouTube не обращается. Вспомогательный инструмент разработки, в поставку не входит.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import requests

from app.core.dates import FILE_STAMP_FORMAT
from app.form.base import FormError
from app.form.discovery import REQUEST_TIMEOUT_SEC, FormDiscovery, FormQuestion, FormStructure, HttpSession
from app.form.submitter import (
    CONFIRMATION_MARKERS,
    FIELD_ACCOUNT_NAME,
    FIELD_DATE,
    FIELD_LANGUAGE,
    FIELD_PLATFORM,
    FIELD_STREAM_KEY,
    FIELD_STREAM_URL,
    HEADER_ACCEPT_LANGUAGE,
    PLATFORM_CODE,
    RESPONSE_LANGUAGE,
    _Answer,
    _build_body,
    _page_title,
    _visited_pages,
    _with_response_language,
)
from app.package.model import FormSpec, Package, PackageError
from app.package.promo import list_package_files
from app.package.reader import read_package
from app.paths import PlanerPaths, build_paths, resolve_root

# Тренировочная форма «Test Регистрация стрима» (CLAUDE.md, инвариант 12). Боевую пробник не трогает.
TRAINING_FORM_ID: Final[str] = "1FAIpQLSePX_pFlocchwfXCSax3ueqgKePumwkjG-bVZUynJtaPITYNQ"
FORM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"/forms/d/e/([^/?#]+)/")
ENTRY_PATTERN: Final[re.Pattern[str]] = re.compile(r"entry\.\d+")
SCRIPT_LANGUAGE_PATTERN: Final[re.Pattern[str]] = re.compile(r"/js/k=freebird\.v\.([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)?)\.")
LANG_ATTRIBUTE_PATTERN: Final[re.Pattern[str]] = re.compile(r"<html[^>]*\blang=\"([^\"]+)\"", re.IGNORECASE)
FBZX_NAME: Final[str] = "fbzx"
CLOSED_FORM_FLAG: Final[str] = "--closed-form"
PAUSE_SEC: Final[float] = 2.0
EXIT_OK: Final[int] = 0
EXIT_REFUSED: Final[int] = 2
HTML_ENCODING: Final[str] = "utf-8"
BODY_TEMPLATE: Final[str] = "{stamp}_probe_{code}{number}.html"
MISSING: Final[str] = "-"
TITLE_MAX_CHARS: Final[int] = 48


@dataclass(frozen=True)
class Submission:
    """Одна отправка опыта: название канала, ключ и — для D — какой вопрос не отправлять."""

    code: str
    number: int
    account_name: str
    stream_key: str
    is_fresh_structure: bool = False    # B: структура (и fbzx) читается заново перед отправкой
    is_required_dropped: bool = False   # D: один обязательный вопрос пройденного раздела не отправляется


EXPERIMENTS: Final[tuple[Submission, ...]] = (
    Submission("A", 1, "PROBE-A", "aaaa-aaaa-aaaa-aaaa-aaaa"),
    Submission("A", 2, "PROBE-A", "aaaa-aaaa-aaaa-aaaa-aaaa"),
    Submission("B", 1, "PROBE-B", "bbbb-bbbb-bbbb-bbbb-bbbb", is_fresh_structure=True),
    Submission("B", 2, "PROBE-B", "bbbb-bbbb-bbbb-bbbb-bbbb", is_fresh_structure=True),
    Submission("C", 1, "PROBE-C", "cccc-cccc-cccc-cccc-0001"),
    Submission("C", 2, "PROBE-C", "cccc-cccc-cccc-cccc-0002"),
    Submission("D", 1, "PROBE-D", "dddd-dddd-dddd-dddd-dddd", is_required_dropped=True),
)
# Сколько строк ждать в таблице ответов, если форма не схлопывает повторы. Сверено владельцем 13-09-2026
# по отправкам 16:27: PROBE-A 2, PROBE-B 2, PROBE-C 2, PROBE-D 0 — форма повторы не схлопывает (ТЗ §7.5).
EXPECTED_ROWS: Final[tuple[tuple[str, str], ...]] = (
    ("PROBE-A", "2 строки, ключ aaaa-aaaa-aaaa-aaaa-aaaa (1 строка — Google схлопнул одинаковые ответы с общим fbzx)"),
    ("PROBE-B", "2 строки, ключ bbbb-bbbb-bbbb-bbbb-bbbb (у каждой отправки свой fbzx)"),
    ("PROBE-C", "2 строки, ключи cccc-cccc-cccc-cccc-0001 и ...-0002 (1 строка — потерян ответ второго канала)"),
    ("PROBE-D", "0 строк (обязательный вопрос пропущен — ответ не должен записаться)"),
)


class ProbeRefused(Exception):
    """Пробник отказывается работать: не та форма, нет пакета, не собрать ответ."""


@dataclass(frozen=True)
class Observation:
    submission: Submission
    http_status: int
    body_length: int
    title: str
    script_language: str
    lang_attribute: str
    entry_count: int
    has_fbzx: bool
    marker: str
    fbzx_sent: str
    saved: Path


@dataclass(frozen=True)
class _Context:
    session: requests.Session
    discovery: FormDiscovery
    form: FormSpec
    logs_dir: Path


class ReadOnlySession:
    """Опыт E: сессия, которая умеет только GET — отправка невозможна при любом исходе чтения."""

    def __init__(self, session: requests.Session) -> None:
        self._session: requests.Session = session

    def get(self, url: str, timeout: float, allow_redirects: bool) -> requests.Response:
        return self._session.get(url, timeout=timeout, allow_redirects=allow_redirects)

    def post(self, url: str, data: dict[str, list[str]], timeout: float, headers: Mapping[str, str]) -> requests.Response:
        raise ProbeRefused(f"режим --closed-form ничего не отправляет: POST {url} запрещён")


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(prog="form_probe", description=__doc__)
    parser.add_argument(
        CLOSED_FORM_FLAG,
        action="store_true",
        help="опыт E: форма закрыта владельцем — только прочитать структуру, ничего не отправлять",
    )
    args: argparse.Namespace = parser.parse_args(argv)
    paths: PlanerPaths = build_paths(resolve_root())
    if args.closed_form:
        return _probe_closed_form(paths)
    return _run_experiments(paths)


def _probe_closed_form(paths: PlanerPaths) -> int:
    """Опыт E: закрытая форма не должна выглядеть как доставка — до POST дело доходить не должно (§7.5)."""
    try:
        form: FormSpec = _latest_form(paths)
        session: ReadOnlySession = ReadOnlySession(requests.Session())
        final: requests.Response = session.get(form.url, timeout=REQUEST_TIMEOUT_SEC, allow_redirects=True)
        _require_training_url(final.url)
        print(f"E: страница формы HTTP {final.status_code}, адрес после редиректа {final.url}")
        structure: FormStructure = _new_discovery(session, paths.logs_dir).structure(form.url)
        _require_training_form(structure)
    except FormError as error:
        print(f"E: структура НЕ прочитана — код {error.code}: {error.message}")
        print(f"E: страница сохранена: {error.diagnostic_path or MISSING}")
        print("E: POST не выполнялся")
        return EXIT_OK
    except (ProbeRefused, OSError) as error:
        print(f"ОТКАЗ: {error}")
        return EXIT_REFUSED
    print(f"E: структура прочитана — вопросов {len(structure.questions)}, разделов {structure.page_count}")
    print("E: POST не выполнялся (режим только чтения)")
    return EXIT_OK


def _run_experiments(paths: PlanerPaths) -> int:
    try:
        form: FormSpec = _latest_form(paths)
        session: requests.Session = requests.Session()
        context: _Context = _Context(session, _new_discovery(session, paths.logs_dir), form, paths.logs_dir)
        _require_training_form(context.discovery.structure(form.url))
    except (ProbeRefused, FormError) as error:
        print(f"ОТКАЗ: {error}")
        return EXIT_REFUSED
    observations: list[Observation] = []
    for index, submission in enumerate(EXPERIMENTS):
        if index:
            time.sleep(PAUSE_SEC)
        try:
            observation: Observation = _submit(context, submission)
        except (ProbeRefused, FormError, OSError) as error:
            print(f"ОТКАЗ на {submission.code}{submission.number}: {error}. Остальные опыты не отправлялись.")
            break
        observations.append(observation)
        print(_line(observation))
    _print_summary(observations)
    return EXIT_OK


def _latest_form(paths: PlanerPaths) -> FormSpec:
    packages: list[Package] = []
    for path in list_package_files(paths):
        try:
            packages.append(read_package(path))
        except PackageError as error:
            print(f"пакет пропущен: {path.name} — {error}")
    if not packages:
        raise ProbeRefused(f"в {paths.promo_dir} нет читаемых пакетов — адрес формы брать неоткуда")
    latest: Package = max(packages, key=lambda package: package.generated_at)
    print(f"форма из пакета {latest.path.name}: {latest.form.url}")
    return latest.form


def _new_discovery(session: HttpSession, logs_dir: Path) -> FormDiscovery:
    return FormDiscovery(session, logs_dir, datetime.now().astimezone())


def _require_training_form(structure: FormStructure) -> None:
    """Предохранитель: после редиректа — только тренировочная форма, иначе ни одной отправки."""
    for url in (structure.view_url, structure.response_url):
        _require_training_url(url)


def _require_training_url(url: str) -> None:
    match: re.Match[str] | None = FORM_ID_PATTERN.search(url)
    form_id: str = match.group(1) if match else MISSING
    if form_id != TRAINING_FORM_ID:
        raise ProbeRefused(f"форма {form_id} ({url}) — не тренировочная {TRAINING_FORM_ID}; ничего не отправлено")


def _submit(context: _Context, submission: Submission) -> Observation:
    discovery: FormDiscovery = (
        _new_discovery(context.session, context.logs_dir) if submission.is_fresh_structure else context.discovery
    )
    structure: FormStructure = discovery.structure(context.form.url)
    _require_training_form(structure)
    answers: list[_Answer] = _answers(context.form, structure, submission)
    pages: list[int] = _visited_pages(structure, answers)
    if submission.is_required_dropped:
        answers = _drop_required(structure, answers, pages)
    body: dict[str, list[str]] = _build_body(structure, answers, pages)
    response: requests.Response = context.session.post(
        _with_response_language(structure.response_url),
        data=body,
        timeout=REQUEST_TIMEOUT_SEC,
        headers={HEADER_ACCEPT_LANGUAGE: RESPONSE_LANGUAGE},
    )
    saved: Path = _save(context.logs_dir, submission, response.text)
    return _observe(submission, response, structure.fbzx, saved)


def _answers(form: FormSpec, structure: FormStructure, submission: Submission) -> list[_Answer]:
    values: dict[str, str] = {
        FIELD_ACCOUNT_NAME: submission.account_name,
        FIELD_STREAM_KEY: submission.stream_key,
    }
    answers: list[_Answer] = []
    for field, title in form.fields.items():
        if title is None:
            continue
        question: FormQuestion | None = structure.question_by_title(title)
        if question is None:
            raise ProbeRefused(f"в форме нет вопроса «{title}» из пакета")
        answers.append(_Answer(question=question, value=_value(field, form, question, values)))
    return answers


def _value(field: str, form: FormSpec, question: FormQuestion, values: dict[str, str]) -> str:
    if field in values:
        return values[field]
    if field == FIELD_PLATFORM:
        wanted: str | None = form.values.get(FIELD_PLATFORM, {}).get(PLATFORM_CODE)
        if wanted is None or (question.options and wanted not in question.options):
            raise ProbeRefused(f"нет варианта площадки {PLATFORM_CODE!r} в вопросе «{question.title}»")
        return wanted
    if field in (FIELD_LANGUAGE, FIELD_DATE, FIELD_STREAM_URL):
        if not question.options:
            raise ProbeRefused(f"у вопроса «{question.title}» нет вариантов")
        return question.options[0]
    raise ProbeRefused(f"поле пакета {field!r} пробнику неизвестно")


def _drop_required(structure: FormStructure, answers: list[_Answer], pages: list[int]) -> list[_Answer]:
    """Последний обязательный вопрос пройденных разделов, не развилка: навигация остаётся прежней."""
    candidates: list[_Answer] = [
        answer
        for answer in answers
        if answer.question.is_required
        and answer.question.page_index in pages
        and answer.question.entry_id not in structure.navigation
    ]
    if not candidates:
        raise ProbeRefused("в пройденных разделах нет обязательного вопроса, который можно пропустить")
    dropped: _Answer = candidates[-1]
    print(f"D: не отправляется обязательный вопрос «{dropped.question.title}» ({dropped.question.entry_id})")
    return [answer for answer in answers if answer is not dropped]


def _save(logs_dir: Path, submission: Submission, text: str) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    name: str = BODY_TEMPLATE.format(
        stamp=datetime.now().strftime(FILE_STAMP_FORMAT),
        code=submission.code,
        number=submission.number,
    )
    path: Path = logs_dir / name
    path.write_text(text, encoding=HTML_ENCODING)
    return path


def _observe(submission: Submission, response: requests.Response, fbzx_sent: str, saved: Path) -> Observation:
    text: str = response.text
    lowered: str = text.lower()
    script: re.Match[str] | None = SCRIPT_LANGUAGE_PATTERN.search(text)
    lang: re.Match[str] | None = LANG_ATTRIBUTE_PATTERN.search(text)
    return Observation(
        submission=submission,
        http_status=response.status_code,
        body_length=len(text),
        title=_page_title(text),
        script_language=script.group(1) if script else MISSING,
        lang_attribute=lang.group(1) if lang else MISSING,
        entry_count=len(ENTRY_PATTERN.findall(text)),
        has_fbzx=FBZX_NAME in text,
        marker=next((marker for marker in CONFIRMATION_MARKERS if marker in lowered), MISSING),
        fbzx_sent=fbzx_sent,
        saved=saved,
    )


def _line(item: Observation) -> str:
    return (
        f"{item.submission.code}{item.submission.number} {item.submission.account_name} "
        f"key={item.submission.stream_key} fbzx_sent={item.fbzx_sent} http={item.http_status} "
        f"len={item.body_length} title={item.title!r} script_lang={item.script_language} "
        f"lang={item.lang_attribute} entry={item.entry_count} fbzx={'да' if item.has_fbzx else 'нет'} "
        f"marker={item.marker!r} saved={item.saved}"
    )


def _print_summary(observations: list[Observation]) -> None:
    header: tuple[str, ...] = ("опыт", "HTTP", "длина", "title", "язык скрипта", "lang", "entry.", "fbzx", "маркер", "fbzx отправки", "файл")
    rows: list[tuple[str, ...]] = [header]
    rows.extend(
        (
            f"{item.submission.code}{item.submission.number}",
            str(item.http_status),
            str(item.body_length),
            item.title[:TITLE_MAX_CHARS],
            item.script_language,
            item.lang_attribute,
            str(item.entry_count),
            "да" if item.has_fbzx else "нет",
            item.marker,
            item.fbzx_sent,
            item.saved.name,
        )
        for item in observations
    )
    widths: list[int] = [max(len(row[column]) for row in rows) for column in range(len(header))]
    print()
    print("Итог опытов:")
    for row in rows:
        print(" | ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    print()
    print("Сверьте с таблицей ответов тренировочной формы (ищите по названию канала):")
    for account_name, expected in EXPECTED_ROWS:
        print(f"  {account_name}: {expected}")


if __name__ == "__main__":
    sys.exit(main())
