from __future__ import annotations

import pytest

from app.core.text import normalize_description, normalize_title, safe_trim, token_file_stem


TOKEN_FILE_STEMS: list[tuple[str, str]] = [
    ("Новини: Україна", "Новини_ Україна"),
    ("News | UA", "News _ UA"),
    ("Канал.", "Канал_"),
    ("CON", "CON_"),
    ("lpt9", "lpt9_"),
    (' a\\b*c?"d"<e>/f. ', '_a_b_c__d__e__f__'),
    ("Osv\tald", "Osv_ald"),
    ("Osvald.X", "Osvald.X"),
    ("Oktavian.X", "Oktavian.X"),
    ("Maria Kamenskay", "Maria Kamenskay"),
    ("CONSOLE", "CONSOLE"),
]


@pytest.mark.parametrize(("name", "stem"), TOKEN_FILE_STEMS, ids=[pair[0] for pair in TOKEN_FILE_STEMS])
def test_token_file_stem(name: str, stem: str) -> None:
    assert token_file_stem(name) == stem


def test_token_file_stem_keeps_length_at_edges() -> None:
    assert token_file_stem("..Канал..") == "__Канал__"
    assert token_file_stem(" . ") == "___"


def test_normalize_title_trims_edges_only() -> None:
    assert normalize_title("  Эфир недели  ") == "Эфир недели"
    assert normalize_title("Эфир  недели") == "Эфир  недели"


def test_normalize_description_unifies_line_endings_and_edges() -> None:
    assert normalize_description("  Текст  \r\n\r\nещё  \r\n") == "Текст\n\nещё"
    assert normalize_description("Строка\rвторая") == "Строка\nвторая"
    assert normalize_description("") == ""


def test_normalize_description_keeps_inner_blank_lines() -> None:
    assert normalize_description("Первый\n\n\nВторой") == "Первый\n\n\nВторой"


def test_short_text_is_not_trimmed() -> None:
    assert safe_trim("Короткое название", 100) == "Короткое название"


def test_trim_prefers_sentence_boundary() -> None:
    text: str = "Первое предложение достаточно длинное. Второе уже не влезает"
    trimmed: str = safe_trim(text, 45)
    assert trimmed == "Первое предложение достаточно длинное."
    assert len(trimmed) <= 45


def test_too_early_sentence_boundary_is_skipped() -> None:
    """Граница предложения в самом начале обрезала бы текст почти целиком — берётся слово."""
    text: str = "Коротко. Дальше идёт длинный текст без точек до самого конца строки"
    trimmed: str = safe_trim(text, 40)
    assert trimmed != "Коротко."
    assert len(trimmed) <= 40
    assert text.startswith(trimmed)


def test_trim_falls_back_to_word_boundary() -> None:
    text: str = "Слова без единого знака препинания идут подряд и не влезают"
    trimmed: str = safe_trim(text, 30)
    assert len(trimmed) <= 30
    assert trimmed == trimmed.rstrip()
    assert text.startswith(trimmed)
    assert text[len(trimmed)] in " "   # слово не разорвано


def test_trim_never_leaves_trailing_space() -> None:
    assert safe_trim("Раз два три четыре пять", 8) == "Раз два"


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_gives_empty(limit: int) -> None:
    assert safe_trim("Текст", limit) == ""


def test_single_long_word_is_dropped_rather_than_cut() -> None:
    assert safe_trim("Длинноесловобезпробелов", 5) == ""


def test_trim_is_idempotent() -> None:
    """Второй проход ничего не меняет — иначе эфир правился бы на каждом запуске."""
    text: str = "Очень длинное название эфира " * 10
    once: str = safe_trim(text, 100)
    assert safe_trim(once, 100) == once
