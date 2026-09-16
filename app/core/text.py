"""Нормализация и обрезка текстов эфира — единственный источник (ТЗ §7.3, §7.4).

Одни и те же правила применяются к тексту из пакета и к тексту, пришедшему с площадки:
иначе совпадающие эфиры считались бы разными и правились бы на каждом запуске.
safe_trim — по образцу broadcaster/app/core/safe_trim.py (границы предложения, слова, символа).
"""
from __future__ import annotations

import re
from typing import Final

WORD_CHAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"\w", re.UNICODE)
SENTENCE_END_CHARS: Final[str] = ".!?…"
BOUNDARY_CHARS: Final[str] = " \t\r\n,;:)]}\"'»"
SENTENCE_MIN_SHARE: Final[float] = 0.6   # короче — граница предложения не годится
WORD_MIN_SHARE: Final[float] = 0.5       # короче — граница слова не годится
CRLF: Final[str] = "\r\n"
CR: Final[str] = "\r"
LF: Final[str] = "\n"
# Имя файла токена secrets\<имя>.token.json: название канала как на YouTube, приведённое к правилам Windows.
TOKEN_FILE_FORBIDDEN_CHARS: Final[str] = '\\/:*?"<>|'
TOKEN_FILE_EDGE_CHARS: Final[str] = " ."
TOKEN_FILE_RESERVED: Final[re.Pattern[str]] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
TOKEN_FILE_REPLACEMENT: Final[str] = "_"
CONTROL_CHAR_LIMIT: Final[int] = 32


def normalize_title(text: str) -> str:
    """Название: только края."""
    return text.strip()


def normalize_description(text: str) -> str:
    """CRLF и CR → LF, хвостовые пробелы строк снимаются, края текста обрезаются."""
    unified: str = text.replace(CRLF, LF).replace(CR, LF)
    return LF.join(line.rstrip() for line in unified.split(LF)).strip()


def token_file_stem(account_name: str) -> str:
    """Имя файла токена без расширения; account_name — уже в NFC.

    Запрещённые и управляющие символы → «_»; пробелы и точки по краям → «_» каждый (длина не меняется);
    зарезервированное имя Windows получает «_» в конце. Имена без таких символов не меняются.
    """
    stem: str = "".join(
        TOKEN_FILE_REPLACEMENT if char in TOKEN_FILE_FORBIDDEN_CHARS or ord(char) < CONTROL_CHAR_LIMIT else char
        for char in account_name
    )
    body: str = stem.strip(TOKEN_FILE_EDGE_CHARS)
    if not body:
        return TOKEN_FILE_REPLACEMENT * len(stem)
    lead: int = len(stem) - len(stem.lstrip(TOKEN_FILE_EDGE_CHARS))
    tail: int = len(stem) - len(stem.rstrip(TOKEN_FILE_EDGE_CHARS))
    stem = TOKEN_FILE_REPLACEMENT * lead + body + TOKEN_FILE_REPLACEMENT * tail
    if TOKEN_FILE_RESERVED.fullmatch(stem):
        return stem + TOKEN_FILE_REPLACEMENT
    return stem


def safe_trim(text: str, limit: int) -> str:
    """Обрезка до limit символов по границе предложения, слова или символа; слово не рвётся."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    for boundary in (_sentence_boundary, _word_boundary, _symbol_boundary):
        index: int = boundary(text, limit)
        if index <= 0:
            continue
        trimmed: str = text[:index].rstrip()
        if trimmed:
            return trimmed
    return ""   # одно слово длиннее лимита — резать его нечем


def _sentence_boundary(text: str, limit: int) -> int:
    minimum: int = max(1, int(limit * SENTENCE_MIN_SHARE))
    for index in range(min(limit, len(text)), minimum - 1, -1):
        if text[index - 1] in SENTENCE_END_CHARS:
            return index
    return 0


def _word_boundary(text: str, limit: int) -> int:
    minimum: int = max(1, int(limit * WORD_MIN_SHARE))
    for index in range(min(limit, len(text)), minimum - 1, -1):
        character: str = text[index - 1]
        if character.isspace() or character in BOUNDARY_CHARS:
            return index
    return 0


def _symbol_boundary(text: str, limit: int) -> int:
    for index in range(min(limit, len(text)), 0, -1):
        if not _is_word_char(text[index - 1]):
            return index
    return 0


def _is_word_char(value: str) -> bool:
    return bool(value and WORD_CHAR_PATTERN.fullmatch(value))
