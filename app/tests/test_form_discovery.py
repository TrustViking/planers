from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.form.base import FORM_CODE_STRUCTURE_UNREADABLE, FORM_CODE_TRANSPORT_FAILED, FormError
from app.form.discovery import FormDiscovery, FormStructure, QuestionKind

VIEW_URL: str = "https://docs.google.com/forms/d/e/ABC/viewform"
SHORT_URL: str = "https://forms.gle/UjVo2gftZdHsEdpZ7"
FBZX: str = "-1234567890"


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200, url: str = VIEW_URL) -> None:
        self.text: str = text
        self.status_code: int = status_code
        self.url: str = url


class _FakeSession:
    """Ни один тест не ходит в сеть: и get, и post — записи в списках."""

    def __init__(self, *responses: _FakeResponse) -> None:
        self._responses: list[_FakeResponse] = list(responses)
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict[str, list[str]]]] = []

    def get(self, url: str, timeout: float, allow_redirects: bool) -> _FakeResponse:
        self.get_calls.append(url)
        if isinstance(self._responses[0], Exception):
            raise self._responses.pop(0)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def post(self, url: str, data: dict[str, list[str]], timeout: float) -> _FakeResponse:
        self.post_calls.append((url, data))
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


def _question(entry_id: int, title: str, type_code: int, options: list[Any] | None = None) -> list[Any]:
    """Элемент формы в раскладке FB_PUBLIC_LOAD_DATA_: [id, title, descr, type, [[entry, options, required]]]."""
    return [entry_id, title, None, type_code, [[entry_id, options, 1]]]


def _page_break(title: str) -> list[Any]:
    return [999, title, None, 8, None]


def build_payload(items: list[Any] | None = None) -> list[Any]:
    return [None, [None, items if items is not None else default_items()], None, FBZX]


def default_items() -> list[Any]:
    """Тренировочная форма: раздел 1 (общий) + раздел YouTube (§6.1 п.2)."""
    return [
        _question(1, "Язык стрима ( Language of stream)", 2, [["Русский ( Russian)"], ["Английский ( English)"]]),
        _question(2, "Название канала ( Channel name)", 0),
        _question(
            3,
            "Время стрима ( Stream time )",
            2,
            [["17.03.2027 Дата стрима (время стрима указано в объявлении)"], ["18.03.2027 Дата стрима"]],
        ),
        _question(4, "Платформа (Platform)", 2, [["You Tube", None, 1], ["Facebook", None, 2]]),
        _page_break("YouTube"),
        _question(5, "You Tube Stream Key", 0),
        _question(6, "Stream-URL (YT)", 2, [["rtmp://a.rtmp.youtube.com/live2/"], ["rtmp://x.rtmp.youtube.com/live2/"]]),
        _page_break("Facebook"),
        _question(7, "Facebook Stream Key", 0),
    ]


def build_html(payload: list[Any] | None = None, with_fbzx: bool = True) -> str:
    body: str = json.dumps(payload if payload is not None else build_payload())
    hidden: str = f'<input type="hidden" name="fbzx" value="{FBZX}">' if with_fbzx else ""
    return f"<html><body>{hidden}<script>var FB_PUBLIC_LOAD_DATA_ = {body};</script></body></html>"


@pytest.fixture
def now() -> datetime:
    return datetime(2027, 3, 16, 12, 0)


def _discovery(session: _FakeSession, tmp_path: Path, now: datetime) -> FormDiscovery:
    return FormDiscovery(session, tmp_path / "logs", now)


def test_structure_is_read_from_the_page(tmp_path: Path, now: datetime) -> None:
    session: _FakeSession = _FakeSession(_FakeResponse(build_html()))
    structure: FormStructure = _discovery(session, tmp_path, now).structure(SHORT_URL)

    assert structure.view_url == VIEW_URL
    assert structure.response_url == "https://docs.google.com/forms/d/e/ABC/formResponse"
    assert structure.fbzx == FBZX
    assert structure.page_count == 3
    language = structure.question_by_title("Язык стрима ( Language of stream)")
    assert language is not None
    assert (language.entry_id, language.kind, language.is_required) == ("entry.1", QuestionKind.RADIO, True)
    assert language.options == ("Русский ( Russian)", "Английский ( English)")
    key = structure.question_by_title("You Tube Stream Key")
    assert key is not None and (key.kind, key.page_index) == (QuestionKind.TEXT, 1)
    assert structure.question_by_title("Facebook Stream Key").page_index == 2   # type: ignore[union-attr]


def test_navigation_map_is_collected(tmp_path: Path, now: datetime) -> None:
    session: _FakeSession = _FakeSession(_FakeResponse(build_html()))
    structure: FormStructure = _discovery(session, tmp_path, now).structure(SHORT_URL)
    assert structure.navigation["entry.4"] == {"You Tube": 1, "Facebook": 2}


def test_structure_is_read_once_per_url(tmp_path: Path, now: datetime) -> None:
    session: _FakeSession = _FakeSession(_FakeResponse(build_html()))
    discovery: FormDiscovery = _discovery(session, tmp_path, now)
    discovery.structure(SHORT_URL)
    discovery.structure(SHORT_URL)
    assert session.get_calls == [SHORT_URL]


def test_two_urls_are_read_separately(tmp_path: Path, now: datetime) -> None:
    """Два пакета рядом могут вести в разные формы — это нормально."""
    session: _FakeSession = _FakeSession(_FakeResponse(build_html()), _FakeResponse(build_html()))
    discovery: FormDiscovery = _discovery(session, tmp_path, now)
    discovery.structure(SHORT_URL)
    discovery.structure("https://forms.gle/OtherForm")
    assert session.get_calls == [SHORT_URL, "https://forms.gle/OtherForm"]


def test_page_without_script_is_saved_for_analysis(tmp_path: Path, now: datetime) -> None:
    """Раскладка FB_PUBLIC_LOAD_DATA_ — открытая [ПРОВЕРИТЬ]: без файла её нечем закрыть."""
    session: _FakeSession = _FakeSession(_FakeResponse("<html>нет скрипта</html>"))
    with pytest.raises(FormError) as raised:
        _discovery(session, tmp_path, now).structure(SHORT_URL)
    assert raised.value.code == FORM_CODE_STRUCTURE_UNREADABLE
    saved: Path | None = raised.value.diagnostic_path
    assert saved is not None and saved.exists()
    assert saved.name.startswith("16-03-2027_120000_form_page_")
    assert "нет скрипта" in saved.read_text(encoding="utf-8")


def test_broken_json_is_also_structure_unreadable(tmp_path: Path, now: datetime) -> None:
    html: str = "<html><script>var FB_PUBLIC_LOAD_DATA_ = [не json];</script></html>"
    session: _FakeSession = _FakeSession(_FakeResponse(html))
    with pytest.raises(FormError) as raised:
        _discovery(session, tmp_path, now).structure(SHORT_URL)
    assert raised.value.code == FORM_CODE_STRUCTURE_UNREADABLE


def test_empty_question_list_is_unreadable(tmp_path: Path, now: datetime) -> None:
    session: _FakeSession = _FakeSession(_FakeResponse(build_html(build_payload([]))))
    with pytest.raises(FormError) as raised:
        _discovery(session, tmp_path, now).structure(SHORT_URL)
    assert raised.value.code == FORM_CODE_STRUCTURE_UNREADABLE


def test_fbzx_falls_back_to_the_payload(tmp_path: Path, now: datetime) -> None:
    session: _FakeSession = _FakeSession(_FakeResponse(build_html(with_fbzx=False)))
    structure: FormStructure = _discovery(session, tmp_path, now).structure(SHORT_URL)
    assert structure.fbzx == FBZX


def test_http_error_is_transport_failure(tmp_path: Path, now: datetime) -> None:
    session: _FakeSession = _FakeSession(_FakeResponse("", status_code=503))
    with pytest.raises(FormError) as raised:
        _discovery(session, tmp_path, now).structure(SHORT_URL)
    assert raised.value.code == FORM_CODE_TRANSPORT_FAILED
