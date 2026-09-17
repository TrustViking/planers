"""Отправка ключа в Google-форму (ТЗ §7.5): Protocol FormSender, результат и коды ошибок."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from app.package.model import FormSpec

if TYPE_CHECKING:   # объект-слот и объект-форма сами импортируют этот модуль: здесь — только аннотации
    from app.form.key_form import KeyForm
    from app.pipeline.plan import PlannedBroadcast

# Коды исходов отправки; тексты для владельца — в messages_ru по этим же ключам.
FORM_CODE_STRUCTURE_UNREADABLE: Final[str] = "structureUnreadable"
FORM_CODE_MISSING_OPTION: Final[str] = "missingOption"
FORM_CODE_REQUIRED_MISSING: Final[str] = "requiredMissing"
FORM_CODE_TRANSPORT_FAILED: Final[str] = "transportFailed"
FORM_CODE_NOT_CONFIRMED: Final[str] = "notConfirmed"
FORM_CODE_OK: Final[str] = ""


@dataclass(frozen=True)
class FormSendResult:
    confirmed: bool                      # ответ формы подтверждён (§7.5 п.5)
    code: str = FORM_CODE_OK             # код исхода; пусто при успехе
    error: str | None = None             # причина неудачи для лога и отчёта
    diagnostic_path: Path | None = None  # сохранённый HTML: без него [ПРОВЕРИТЬ] не закрыть


class FormError(Exception):
    """Сбой отправки: code — из констант выше, message — пояснение для владельца."""

    def __init__(self, code: str, message: str, diagnostic_path: Path | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code: str = code
        self.message: str = message
        self.diagnostic_path: Path | None = diagnostic_path

    def as_result(self) -> FormSendResult:
        return FormSendResult(
            confirmed=False,
            code=self.code,
            error=self.message,
            diagnostic_path=self.diagnostic_path,
        )


class FormSender(Protocol):
    def prepare(self, forms: Sequence[FormSpec]) -> None:
        """Прочитать формы запуска — в начале, один раз на форму; send потом берёт готовую."""
        ...

    def form_for(self, spec: FormSpec) -> KeyForm | None:
        """Готовая форма для допуска объекта; не прочиталась — FormError. None — отправитель форму не проверяет."""
        ...

    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        """Всё нужное — внутри объекта: ключ, канал, слот, форма его пакета и готовые ответы (§7.5)."""
        ...


class NoopFormSender:
    """Форма не отправляется: ключ остаётся непереданным. Остаётся для тестов."""

    def prepare(self, forms: Sequence[FormSpec]) -> None:
        """Читать нечего."""

    def form_for(self, spec: FormSpec) -> KeyForm | None:
        """Форма не проверяется: объекты допускаются без неё."""
        return None

    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        return FormSendResult(confirmed=False)
