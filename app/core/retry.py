"""Политика повторов — единственный источник числа повторов и пауз между ними.

Ей пользуются все сетевые обращения планера: YouTube (app/platforms/youtube.py) и Google-форма
(app/form/discovery.py, app/form/submitter.py). Что именно повторять, решает вызывающий;
здесь — только сколько раз и с какой паузой. Случайная добавка (jitter) разводит повторы во времени.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final

MAX_RETRIES: Final[int] = 4             # после первого обращения — не больше стольких повторов
BASE_DELAY_SEC: Final[float] = 2.0      # пауза перед первым повтором без добавки
MAX_DELAY_SEC: Final[float] = 32.0      # потолок паузы вместе с добавкой
JITTER_MAX_SEC: Final[float] = 1.0      # случайная добавка к паузе: от 0 до стольких секунд


@dataclass(frozen=True)
class RetryPolicy:
    """Паузы повторов 1–4: 2–3, 4–5, 8–9, 16–17 с."""

    max_retries: int = MAX_RETRIES
    base_delay_sec: float = BASE_DELAY_SEC
    max_delay_sec: float = MAX_DELAY_SEC
    jitter_max_sec: float = JITTER_MAX_SEC

    @property
    def max_attempts(self) -> int:
        """Обращений всего: первое и все повторы."""
        return self.max_retries + 1

    def has_retry_left(self, retry_number: int) -> bool:
        """Можно ли сделать повтор с этим номером (повторы нумеруются с 1)."""
        return 1 <= retry_number <= self.max_retries

    def delay_sec(self, retry_number: int, rng: random.Random) -> float:
        """Пауза перед повтором retry_number: удвоение от базовой плюс добавка, не больше потолка."""
        base: float = self.base_delay_sec * 2 ** (retry_number - 1)
        return min(base + rng.uniform(0, self.jitter_max_sec), self.max_delay_sec)
