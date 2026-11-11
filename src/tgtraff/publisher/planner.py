"""Планировщик очереди: когда выпускать одобренный пост (спека §8).

Связывает три вещи: конфиг города (слоты, паузы, потолок), состояние очереди
(что уже занято на сегодня) и часы города. Отдельным классом — потому что
одинаково нужен и админ-боту (сказать модератору время), и сервисным
автопостам, и тестам.
"""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone

from tgtraff.publisher import store
from tgtraff.publisher.schedule import next_slot, zone

DEFAULT_TZ = "Europe/Moscow"
# Насколько далеко вперёд планировщик готов искать свободные сутки.
MAX_LOOKAHEAD_DAYS = 8


class Planner:
    def __init__(self, channels, city_tz: dict[str, str] | None = None,
                 rng: random.Random | None = None):
        self.channels = channels
        self.city_tz = city_tz or {}
        self.rng = rng

    def tz(self, city: str) -> str:
        return self.city_tz.get(city, DEFAULT_TZ)

    def city_config(self, city: str):
        cfg = self.channels.cities.get(city)
        if cfg is None:
            raise ValueError(f"город {city!r} не описан в config/channels.yaml")
        return cfg

    def plan(self, db, city: str, urgency: str = "normal",
             now: datetime | None = None) -> datetime:
        cfg = self.city_config(city)
        now = now or datetime.now(timezone.utc)
        tz = self.tz(city)
        if urgency == "flash":                    # молния сетку не спрашивает (спека §8)
            return now

        last_at = store.last_planned_at(db, city)
        floor, candidate = None, now
        # Потолок считается для КАЖДЫХ суток, а не только для сегодняшних:
        # иначе очередь, переполнившая сегодня, свалила бы всё на завтра.
        for _ in range(MAX_LOOKAHEAD_DAYS):
            candidate = next_slot(
                now, tz, cfg.slots,
                min_gap_minutes=cfg.min_gap_minutes, max_gap_minutes=cfg.max_gap_minutes,
                daily_limit=0, last_at=last_at, urgency=urgency, rng=self.rng, floor=floor)
            if not cfg.daily_limit or store.day_load(db, city, tz, candidate) < cfg.daily_limit:
                return candidate
            floor = _next_local_midnight(candidate, tz)
        return candidate


def _next_local_midnight(moment: datetime, tz: str) -> datetime:
    local = moment.astimezone(zone(tz))
    return datetime.combine(local.date() + timedelta(days=1), time(0, 0),
                            tzinfo=local.tzinfo)
