from __future__ import annotations

import logging
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.observability.logging_setup import close_logging, get_logger, setup_logging

THIRD_PARTY_LOGGER: str = "googleapiclient.discovery_cache"


@pytest.fixture(autouse=True)
def _closed_logging() -> Iterator[None]:
    yield
    close_logging()


def _emit() -> None:
    get_logger("runner").warning("slot_not_admitted slot_id=17-03-2027_1900_uk")
    get_logger("runner").info("run_report mode=full")
    logging.getLogger(THIRD_PARTY_LOGGER).warning("file_cache is only supported with oauth2client<4.0.0")
    logging.getLogger(THIRD_PARTY_LOGGER).info("third party chatter")


def test_without_debug_nothing_goes_to_the_terminal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Терминал — только тексты владельца: ни WARNING планера, ни чужой WARNING, ни lastResort."""
    log_path: Path = setup_logging(tmp_path, debug=False)
    _emit()
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        logging.captureWarnings(True)          # catch_warnings вернул свой showwarning — повторяем то, что делает setup
        warnings.warn("library deprecation", DeprecationWarning, stacklevel=1)
    captured: pytest.CaptureResult[str] = capsys.readouterr()
    assert captured.err == "" and captured.out == ""
    close_logging()
    text: str = log_path.read_text(encoding="utf-8")
    assert " | WARNING | planer.runner | slot_not_admitted" in text
    assert " | INFO | planer.runner | run_report" in text          # файл планера — DEBUG
    assert f" | WARNING | {THIRD_PARTY_LOGGER} | file_cache" in text
    assert "third party chatter" not in text                      # чужие — от WARNING
    assert "library deprecation" in text


def test_debug_prints_the_log_to_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(tmp_path, debug=True)
    _emit()
    err: str = capsys.readouterr().err
    assert " | WARNING | planer.runner | slot_not_admitted" in err
    assert " | INFO | planer.runner | run_report" in err
    assert f" | WARNING | {THIRD_PARTY_LOGGER} | file_cache" in err


def test_close_removes_only_own_handlers_from_the_python_root(tmp_path: Path) -> None:
    python_root: logging.Logger = logging.getLogger()
    before: list[logging.Handler] = list(python_root.handlers)
    setup_logging(tmp_path, debug=False)
    assert len(python_root.handlers) == len(before) + 1
    close_logging()
    assert python_root.handlers == before
    assert logging.getLogger("planer").handlers == []
