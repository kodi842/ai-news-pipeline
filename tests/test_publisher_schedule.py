"""Сетка публикаций (спека §8): слоты 07-09, 12-14, 18-22 в ЛОКАЛЬНОМ времени
города, паузы 20-40 мин, дневной потолок, «молния» вне сетки.

Один и тот же момент UTC — это разное локальное время в Череповце (UTC+3),
Магнитогорске (UTC+5) и Новокузнецке (UTC+7), поэтому очередь у каждого города
своя.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff.publisher.schedule import local_day, next_slot
from tgtraff.publisher.settings import parse_slots

SLOTS = parse_slots(["07:00-09:00", "12:00-14:00", "18:00-22:00"])
CHE, NVK = "Europe/Moscow", "Asia/Novokuznetsk"      # UTC+3 и UTC+7


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def plan(now, tz=CHE, **kw):
    kw.setdefault("slots", SLOTS)
    kw.setdefault("min_gap_minutes", 20)
    kw.setdefault("max_gap_minutes", 40)
    kw.setdefault("daily_limit", 18)
    return next_slot(now, tz, **kw)


def test_flash_is_published_immediately_outside_the_grid():
    now = utc(2026, 9, 4, 12, 30)          # 15:30 в Череповце — вне слотов
    assert plan(now, urgency="flash") == now


def test_moment_inside_a_slot_is_published_now():
    now = utc(2026, 9, 4, 15, 30)          # 18:30 локального — внутри 18-22
    assert plan(now) == now


def test_moment_outside_slots_waits_for_the_next_slot():
    now = utc(2026, 9, 4, 12, 30)          # 15:30 локального
    assert plan(now) == utc(2026, 9, 4, 15, 0)     # 18:00 локального


def test_night_moves_the_post_to_the_morning_slot():
    now = utc(2026, 9, 4, 20, 30)          # 23:30 локального
    assert plan(now) == utc(2026, 9, 5, 4, 0)      # 07:00 следующего дня


def test_pause_between_posts_is_kept():
    """Пауза отсчитывается от ПРОШЛОГО поста, а не от «сейчас»: пост, вышедший
    пять минут назад, оставляет очереди 15-35 минут ожидания."""
    now = utc(2026, 9, 4, 15, 30)
    last = now - timedelta(minutes=5)
    when = plan(now, last_at=last)
    assert timedelta(minutes=20) <= when - last <= timedelta(minutes=40)


def test_pause_that_runs_past_the_slot_moves_to_the_next_one():
    now = utc(2026, 9, 4, 18, 50)          # 21:50 локального, слот кончается в 22:00
    when = plan(now, last_at=now)
    assert when == utc(2026, 9, 5, 4, 0)           # 07:00 следующего дня


def test_daily_limit_moves_the_post_to_tomorrow():
    now = utc(2026, 9, 4, 15, 30)          # внутри вечернего слота
    assert plan(now, published_today=18) == utc(2026, 9, 5, 4, 0)


def test_city_timezone_changes_the_slot():
    """20:30 UTC — это 23:30 в Череповце (ждём утра) и 03:30 в Новокузнецке
    (тоже ждём утра, но своего)."""
    now = utc(2026, 9, 4, 20, 30)
    assert plan(now, tz=NVK) == utc(2026, 9, 5, 0, 0)   # 07:00 по Новокузнецку


def test_pause_is_random_within_the_configured_window():
    now = utc(2026, 9, 4, 15, 0)
    gaps = {plan(now, last_at=now, min_gap_minutes=20, max_gap_minutes=40) for _ in range(30)}
    assert len(gaps) > 1, "пауза должна плавать, а не быть константой"


def test_local_day_is_counted_by_city_clock():
    now = utc(2026, 9, 4, 20, 30)          # 23:30 в Череповце, 03:30 5-го в Новокузнецке
    assert local_day(now, CHE) == "2026-09-04"
    assert local_day(now, NVK) == "2026-09-05"


def test_city_without_slots_publishes_as_soon_as_the_pause_allows():
    now = utc(2026, 9, 4, 12, 30)
    assert plan(now, slots=()) == now


def test_unknown_timezone_is_a_config_error():
    with pytest.raises(ValueError):
        plan(utc(2026, 9, 4, 12, 0), tz="Mars/Olympus")
