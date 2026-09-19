"""Прогресс запуска в консоли: строка на каждый долгий шаг между шапкой и «Итогом».

Шапку печатает main.py сразу после настройки логов; итоговые блоки — console.py. Здесь — только
живые строки по ходу работы: пакеты, даты формы, чтение каналов, создание и исправление эфиров, отправка ключей, отчёт.
Прогресс передаётся параметром: NoProgress — умолчание (тесты, вызовы без консоли), ConsoleProgress — main.
"""
from __future__ import annotations

from enum import Enum
from typing import Final, Protocol

from app.config.loader import ChannelConfig
from app.form.key_form import DateCoverage
from app.pipeline.plan import PlannedBroadcast
from app.ui import messages_ru as msg


class BroadcastStep(str, Enum):
    CREATE = "create"
    FIX = "fix"


_STEP_TEXTS: Final[dict[BroadcastStep, str]] = {
    BroadcastStep.CREATE: msg.PROGRESS_BROADCAST_CREATE,
    BroadcastStep.FIX: msg.PROGRESS_BROADCAST_FIX,
}


class RunProgress(Protocol):
    def packages_read(self, packages: int, slots_total: int, slots_mine: int) -> None: ...

    def form_dates_checked(self, coverage: DateCoverage) -> None: ...

    def channel_read_started(self, channel: ChannelConfig) -> None: ...

    def channel_read_done(self, channel: ChannelConfig, upcoming: int) -> None: ...

    def broadcast_step_started(self, item: PlannedBroadcast, step: BroadcastStep) -> None: ...

    def key_send_started(self, item: PlannedBroadcast) -> None: ...

    def report_started(self) -> None: ...


class NoProgress:
    """Ничего не печатает."""

    def packages_read(self, packages: int, slots_total: int, slots_mine: int) -> None:
        return None

    def form_dates_checked(self, coverage: DateCoverage) -> None:
        return None

    def channel_read_started(self, channel: ChannelConfig) -> None:
        return None

    def channel_read_done(self, channel: ChannelConfig, upcoming: int) -> None:
        return None

    def broadcast_step_started(self, item: PlannedBroadcast, step: BroadcastStep) -> None:
        return None

    def key_send_started(self, item: PlannedBroadcast) -> None:
        return None

    def report_started(self) -> None:
        return None


class ConsoleProgress:
    """Строка сразу в консоль: flush обязателен — в собранном exe вывод иначе копится до конца запуска."""

    def packages_read(self, packages: int, slots_total: int, slots_mine: int) -> None:
        self._say(msg.PROGRESS_PACKAGES_READ.format(packages=packages, slots_total=slots_total, slots_mine=slots_mine))

    def form_dates_checked(self, coverage: DateCoverage) -> None:
        if not coverage.is_checkable:
            self._say(msg.PROGRESS_FORM_DATES_ANY)
            return
        if coverage.is_complete:
            self._say(msg.PROGRESS_FORM_DATES_OK.format(wanted=len(coverage.wanted), accepted=coverage.accepted_count))
            return
        self._say(msg.PROGRESS_FORM_DATES_MISSING.format(dates=coverage.missing_text))

    def channel_read_started(self, channel: ChannelConfig) -> None:
        self._say(msg.PROGRESS_CHANNEL_READ_STARTED.format(account_name=channel.account_name, handle=channel.handle))

    def channel_read_done(self, channel: ChannelConfig, upcoming: int) -> None:
        self._say(
            msg.PROGRESS_CHANNEL_READ_DONE.format(
                account_name=channel.account_name, handle=channel.handle, count=upcoming
            )
        )

    def broadcast_step_started(self, item: PlannedBroadcast, step: BroadcastStep) -> None:
        self._say(_STEP_TEXTS[step].format(**_broadcast_fields(item)))

    def key_send_started(self, item: PlannedBroadcast) -> None:
        self._say(msg.PROGRESS_KEY_SEND.format(**_broadcast_fields(item)))

    def report_started(self) -> None:
        self._say(msg.PROGRESS_REPORT)

    @staticmethod
    def _say(text: str) -> None:
        print(msg.PROGRESS_LINE.format(text=text), flush=True)


def _broadcast_fields(item: PlannedBroadcast) -> dict[str, str]:
    return {
        "account_name": item.account_name,
        "handle": item.channel.handle,
        "date": item.date,
        "time": item.time,
        "language": item.language,
    }
