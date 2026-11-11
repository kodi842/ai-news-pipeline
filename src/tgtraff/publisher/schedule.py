"""Когда публиковать (спека §8).

Три правила, и все три считаются в ЛОКАЛЬНОМ времени города:
  * слоты прайм-таймов — пост ждёт ближайшего окна;
  * пауза 20-40 мин между постами — лента не должна выходить очередью;
  * дневной потолок — переполнение уезжает на завтра.

«Молния» (`urgency=flash`) сетку игнорирует: спека §8 разрешает срочному
выходить немедленно и вне окна — после модерации.

Пауза случайная: ровный интервал в 30 минут читается как автомат, а нам нужен
ритм живого канала. Генератор случайных чисел подставляется в тестах.
"""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_LOOKAHEAD_DAYS = 8


def zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"неизвестная таймзона города: {tz!r}") from exc


def local_day(moment: datetime, tz: str) -> str:
    """Календарный день по часам города — по нему считается дневной потолок."""
    return moment.astimezone(zone(tz)).strftime("%Y-%m-%d")


def next_slot(now: datetime, tz: str, slots=(), *, min_gap_minutes: int = 20,
              max_gap_minutes: int = 40, daily_limit: int = 18,
              last_at: datetime | None = None, published_today: int = 0,
              urgency: str = "normal", rng: random.Random | None = None,
              floor: datetime | None = None) -> datetime:
    """Ближайший момент (UTC), когда пост можно выпустить.

    `floor` — «не раньше этого момента»: им планировщик перебирает дни, когда
    сегодняшний потолок уже выбран."""
    if urgency == "flash":
        return now

    tzinfo = zone(tz)
    rng = rng or random
    earliest = now.astimezone(tzinfo)
    if floor is not None:
        earliest = max(earliest, floor.astimezone(tzinfo))

    if last_at is not None:
        gap = timedelta(minutes=rng.randint(min(min_gap_minutes, max_gap_minutes),
                                            max(min_gap_minutes, max_gap_minutes)))
        earliest = max(earliest, last_at.astimezone(tzinfo) + gap)

    if daily_limit and published_today >= daily_limit:
        earliest = max(earliest, _midnight(earliest, days=1))

    if not slots:
        return earliest.astimezone(timezone.utc)

    for offset in range(MAX_LOOKAHEAD_DAYS):
        floor = earliest if offset == 0 else _midnight(earliest, days=offset)
        day = floor.date()
        for slot in sorted(slots, key=lambda s: s.start):
            end = datetime.combine(day, slot.end, tzinfo=tzinfo)
            if floor >= end:
                continue
            start = datetime.combine(day, slot.start, tzinfo=tzinfo)
            return max(floor, start).astimezone(timezone.utc)

    # Слоты заданы так, что ни одно окно не подошло за неделю — это конфиг,
    # а не расписание: публикуем как только можно, но громко.
    return earliest.astimezone(timezone.utc)


def _midnight(moment: datetime, days: int = 0) -> datetime:
    return datetime.combine(moment.date() + timedelta(days=days), time(0, 0),
                            tzinfo=moment.tzinfo)
