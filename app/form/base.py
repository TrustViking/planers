"""Отправка ключа в Google-форму (ТЗ §7.5): Protocol FormSender и заглушка до этапа 4."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.config.loader import ChannelConfig
from app.package.model import FormSpec, Slot
from app.state.registry import Registration


@dataclass(frozen=True)
class FormSendResult:
    confirmed: bool       # ответ формы подтверждён (§7.5 п.5)
    error: str | None     # причина неудачи; None вместе с confirmed=False — отправка не выполнялась


class FormSender(Protocol):
    def send(
        self,
        registration: Registration,
        slot: Slot,
        channel: ChannelConfig,
        form: FormSpec,
    ) -> FormSendResult:
        ...


class NoopFormSender:
    """До этапа 4 форма не отправляется: form_status остаётся pending."""

    def send(
        self,
        registration: Registration,
        slot: Slot,
        channel: ChannelConfig,
        form: FormSpec,
    ) -> FormSendResult:
        return FormSendResult(confirmed=False, error=None)
