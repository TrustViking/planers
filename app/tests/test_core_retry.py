from __future__ import annotations

import random

import pytest

from app.core.retry import BASE_DELAY_SEC, JITTER_MAX_SEC, MAX_DELAY_SEC, MAX_RETRIES, RetryPolicy


class _FixedRandom(random.Random):
    """uniform всегда отдаёт заданную долю интервала: границы добавки проверяются точно."""

    def __init__(self, share: float) -> None:
        super().__init__(0)
        self._share: float = share

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self._share


def test_policy_defaults() -> None:
    policy: RetryPolicy = RetryPolicy()
    assert (policy.max_retries, policy.base_delay_sec, policy.max_delay_sec, policy.jitter_max_sec) == (
        MAX_RETRIES, BASE_DELAY_SEC, MAX_DELAY_SEC, JITTER_MAX_SEC
    ) == (4, 2.0, 32.0, 1.0)
    assert policy.max_attempts == 5


@pytest.mark.parametrize(("retry_number", "low"), [(1, 2.0), (2, 4.0), (3, 8.0), (4, 16.0)])
def test_delays_by_retry_number_with_jitter_bounds(retry_number: int, low: float) -> None:
    policy: RetryPolicy = RetryPolicy()
    assert policy.delay_sec(retry_number, _FixedRandom(0.0)) == low
    assert policy.delay_sec(retry_number, _FixedRandom(1.0)) == low + JITTER_MAX_SEC


def test_delays_with_seeded_rng_are_repeatable_and_within_bounds() -> None:
    policy: RetryPolicy = RetryPolicy()
    first: list[float] = [policy.delay_sec(number, random.Random(3)) for number in range(1, 5)]
    second: list[float] = [policy.delay_sec(number, random.Random(3)) for number in range(1, 5)]
    assert first == second
    for number, delay in enumerate(first, start=1):
        assert 2.0 * 2 ** (number - 1) <= delay <= 2.0 * 2 ** (number - 1) + 1.0


def test_delay_never_exceeds_the_ceiling() -> None:
    policy: RetryPolicy = RetryPolicy()
    assert policy.delay_sec(5, _FixedRandom(1.0)) == MAX_DELAY_SEC       # 32 + 1 → 32
    assert policy.delay_sec(10, _FixedRandom(0.0)) == MAX_DELAY_SEC


def test_has_retry_left() -> None:
    policy: RetryPolicy = RetryPolicy()
    assert [policy.has_retry_left(number) for number in range(0, 6)] == [False, True, True, True, True, False]
