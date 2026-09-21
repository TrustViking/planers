"""Статистика запуска — один объект на запуск (RunStats): время, обращения к YouTube и форме, единицы квоты.

Создаёт его main.run_cli первым делом и передаёт параметром туда, где меряется: YouTubePlatform, FormDiscovery,
GoogleFormSender, runner.run. Сейчас только меряем: паузы, порядок и состав запросов от статистики не зависят.
В конце запуска объект ложится строкой в таблицу runs памяти планера (app/records/run_record.py),
в лог — строками run_stats*, в терминал — «Время работы» и «YouTube» (тексты — messages_ru, печатает main).
Часы — time.monotonic; тесты передают свои.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Final

from app.core.dates import format_datetime_seconds
from app.observability.logging_setup import get_logger

LOGGER = get_logger("stats")

SECONDS_DIGITS: Final[int] = 1   # секунды в логе и в stats_json — с одной цифрой после точки
OTHER_STAGE: Final[str] = "other"   # общее время минус этапы


class RunKind(str, Enum):
    """Режим запуска для статистики: три режима runner и два режима main."""

    FULL = "full"
    DRY_RUN = "dry_run"
    STATUS = "status"
    CHECK = "check"
    AUTH = "auth"


class RunStage(str, Enum):
    """Этапы запуска; этапы не вкладываются друг в друга."""

    START = "start"          # конфиги и сверка каналов без браузера (main)
    PACKAGES = "packages"    # пакеты и отбор объектов
    FORM = "form"            # чтение форм и проверка дат
    LOGINS = "logins"        # входы в каналы: ожидание браузера — здесь, а не в остальных цифрах
    RECONCILE = "reconcile"  # допуск, память, сверка с площадкой
    ACTIONS = "actions"      # действия по объектам, ключи в форму, keys.txt
    REPORT = "report"        # отчёт, чистка


@dataclass
class MethodStats:
    """Один метод YouTube (имя операции, как в YouTubePlatform._execute)."""

    calls: int = 0         # каждая попытка
    retries: int = 0
    empty: int = 0         # пустой ответ чтения по id (notListed)
    refusals: int = 0      # окончательные отказы
    request_sec: float = 0.0   # только execute(), без пауз
    units: int = 0         # единицы квоты — оценка сверху: цена метода за каждую попытку


@dataclass
class TimedCount:
    count: int = 0
    seconds: float = 0.0

    def add(self, seconds: float) -> None:
        self.count += 1
        self.seconds += seconds


@dataclass
class ObjectTimes:
    """Время объектов в этапе действий по одному итоговому решению."""

    count: int = 0
    total_sec: float = 0.0
    max_sec: float = 0.0

    def add(self, seconds: float) -> None:
        self.count += 1
        self.total_sec += seconds
        self.max_sec = max(self.max_sec, seconds)


class RunStats:
    """Счётчики и секунды запуска; у каждой величины одно место — поле этого объекта."""

    def __init__(
        self,
        *,
        kind: RunKind = RunKind.FULL,
        version: str = "",
        clock: Callable[[], float] = time.monotonic,
        started_utc: datetime | None = None,
    ) -> None:
        self._clock: Callable[[], float] = clock
        self._started_at: float = clock()
        self.started_utc: datetime = started_utc or datetime.now(timezone.utc)
        self.kind: RunKind = kind
        self.version: str = version
        self.methods: dict[str, MethodStats] = {}
        self.pause_sec: float = 0.0
        self.retry_sleep_sec: float = 0.0
        self.pictures: TimedCount = TimedCount()
        self.form_reads: TimedCount = TimedCount()
        self.form_posts: TimedCount = TimedCount()
        self.stages: dict[RunStage, float] = {}
        self.objects: dict[str, ObjectTimes] = {}
        self.totals: dict[str, int] = {}
        self.exit_code: int | None = None
        self._elapsed_sec: float | None = None   # задано finish: время дальше не идёт

    # --- часы
    def now(self) -> float:
        return self._clock()

    @property
    def elapsed_sec(self) -> float:
        if self._elapsed_sec is not None:
            return self._elapsed_sec
        return self._clock() - self._started_at

    def finish(self, exit_code: int) -> None:
        """Конец запуска: код выхода и общее время замораживаются."""
        self.exit_code = exit_code
        self._elapsed_sec = self._clock() - self._started_at

    # --- YouTube
    def _method(self, operation: str) -> MethodStats:
        return self.methods.setdefault(operation, MethodStats())

    def request_done(self, operation: str, seconds: float, units: int) -> None:
        """Одна попытка обращения: секунды внутри запроса и её цена в единицах квоты."""
        method: MethodStats = self._method(operation)
        method.calls += 1
        method.request_sec += seconds
        method.units += units

    def retried(self, operation: str, sleep_sec: float) -> None:
        self._method(operation).retries += 1
        self.retry_sleep_sec += sleep_sec

    def empty_answer(self, operation: str) -> None:
        self._method(operation).empty += 1

    def refused(self, operation: str) -> None:
        self._method(operation).refusals += 1

    def paused(self, seconds: float) -> None:
        self.pause_sec += seconds

    def picture_downloaded(self, seconds: float) -> None:
        self.pictures.add(seconds)

    @property
    def youtube_calls(self) -> int:
        return sum(method.calls for method in self.methods.values())

    @property
    def youtube_units(self) -> int:
        return sum(method.units for method in self.methods.values())

    @property
    def request_sec(self) -> float:
        return sum(method.request_sec for method in self.methods.values())

    # --- форма
    def form_read(self, seconds: float) -> None:
        self.form_reads.add(seconds)

    def form_posted(self, seconds: float) -> None:
        self.form_posts.add(seconds)

    # --- этапы и объекты
    @contextmanager
    def stage(self, stage: RunStage) -> Iterator[None]:
        """Время этапа копится, даже если этап оборвался исключением."""
        started: float = self._clock()
        try:
            yield
        finally:
            self.stages[stage] = self.stages.get(stage, 0.0) + (self._clock() - started)

    @property
    def other_sec(self) -> float:
        return max(self.elapsed_sec - sum(self.stages.values()), 0.0)

    def object_done(self, decision: str, seconds: float) -> None:
        self.objects.setdefault(decision, ObjectTimes()).add(seconds)

    def record_totals(self, totals: Mapping[str, int]) -> None:
        """Итоги по объектам — из build_totals, если отчёт был."""
        self.totals = dict(totals)

    # --- выгрузка
    def to_json(self) -> dict[str, Any]:
        """Всё для stats_json строки runs; моменты — DD-MM-YYYY HH:MM:SS по часам машины."""
        return {
            "version": self.version,
            "mode": self.kind.value,
            "exit_code": self.exit_code,
            "started": format_datetime_seconds(self.started_utc.astimezone()),
            "finished": format_datetime_seconds((self.started_utc + timedelta(seconds=self.elapsed_sec)).astimezone()),
            "elapsed_sec": _sec(self.elapsed_sec),
            "youtube": {
                "calls": self.youtube_calls,
                "units": self.youtube_units,
                "request_sec": _sec(self.request_sec),
                "pause_sec": _sec(self.pause_sec),
                "retry_sleep_sec": _sec(self.retry_sleep_sec),
                "methods": {name: _rounded(asdict(method)) for name, method in sorted(self.methods.items())},
            },
            "pictures": _rounded(asdict(self.pictures)),
            "form": {"reads": _rounded(asdict(self.form_reads)), "posts": _rounded(asdict(self.form_posts))},
            "stages": {**{stage.value: _sec(sec) for stage, sec in self.stages.items()}, OTHER_STAGE: _sec(self.other_sec)},
            "objects": {name: _rounded(asdict(times)) for name, times in sorted(self.objects.items())},
            "totals": self.totals,
        }

    def log_summary(self, quota_day: str, quota_day_units: int | None) -> None:
        """Строки лога перед run_finished: run_stats, по строке на метод, этап и решение."""
        LOGGER.info(
            "run_stats elapsed_sec=%.1f youtube_calls=%d youtube_units=%d quota_day=%s quota_day_units=%s"
            " request_sec=%.1f pause_sec=%.1f retry_sleep_sec=%.1f pictures=%d picture_sec=%.1f"
            " form_reads=%d form_posts=%d form_sec=%.1f",
            self.elapsed_sec,
            self.youtube_calls,
            self.youtube_units,
            quota_day,
            quota_day_units if quota_day_units is not None else "-",
            self.request_sec,
            self.pause_sec,
            self.retry_sleep_sec,
            self.pictures.count,
            self.pictures.seconds,
            self.form_reads.count,
            self.form_posts.count,
            self.form_reads.seconds + self.form_posts.seconds,
        )
        for name, method in sorted(self.methods.items()):
            LOGGER.info(
                "run_stats_method operation=%s calls=%d retries=%d empty=%d refusals=%d request_sec=%.1f units=%d",
                name,
                method.calls,
                method.retries,
                method.empty,
                method.refusals,
                method.request_sec,
                method.units,
            )
        for stage, seconds in self.stages.items():
            LOGGER.info("run_stats_stage stage=%s sec=%.1f", stage.value, seconds)
        LOGGER.info("run_stats_stage stage=%s sec=%.1f", OTHER_STAGE, self.other_sec)
        for name, times in sorted(self.objects.items()):
            LOGGER.info(
                "run_stats_objects decision=%s count=%d total_sec=%.1f max_sec=%.1f",
                name,
                times.count,
                times.total_sec,
                times.max_sec,
            )


def _sec(value: float) -> float:
    return round(value, SECONDS_DIGITS)


def _rounded(values: dict[str, Any]) -> dict[str, Any]:
    """Секунды (float) — с одной цифрой после точки; счётчики как есть."""
    return {key: _sec(value) if isinstance(value, float) else value for key, value in values.items()}
