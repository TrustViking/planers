"""Отправка ключа в Google-форму (ТЗ §7.5): Protocol FormSender и заглушка до этапа 4."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.pipeline.plan import PlannedBroadcast


@dataclass(frozen=True)
class FormSendResult:
    confirmed: bool       # ответ формы подтверждён (§7.5 п.5)
    error: str | None     # причина неудачи; None вместе с confirmed=False — отправка не выполнялась


class FormSender(Protocol):
    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        """Всё нужное — внутри объекта: ключ, канал, слот и форма его пакета (§7.5)."""
        ...


class NoopFormSender:
    """До этапа 4 форма не отправляется: form_status остаётся pending."""

    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        return FormSendResult(confirmed=False, error=None)
