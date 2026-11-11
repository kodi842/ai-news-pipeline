"""Параметры публикации (config/settings.yaml → блок `publisher`, спека §8).

Слоты прайм-таймов задаются в ЛОКАЛЬНОМ времени города: Череповец UTC+3,
Магнитогорск UTC+5, Новокузнецк UTC+7 — один и тот же «18:00» это три разных
момента UTC, и очередь обязана считать их порознь.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time

SLOT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True)
class SlotWindow:
    """Окно прайм-тайма в локальном времени города."""

    start: time
    end: time

    def contains(self, moment: time) -> bool:
        return self.start <= moment < self.end

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def parse_slot(raw: str) -> SlotWindow:
    m = SLOT_RE.match(str(raw))
    if not m:
        raise ValueError(f"слот прайм-тайма должен быть вида «18:00-22:00», получено {raw!r}")
    sh, sm, eh, em = (int(g) for g in m.groups())
    if not (0 <= sh < 24 and 0 <= eh <= 24 and sm < 60 and em < 60):
        raise ValueError(f"слот вне суток: {raw!r}")
    start, end = time(sh, sm), time(23, 59) if (eh, em) == (24, 0) else time(eh, em)
    if end <= start:
        raise ValueError(f"слот заканчивается раньше, чем начинается: {raw!r}")
    return SlotWindow(start, end)


def parse_slots(raw) -> tuple[SlotWindow, ...]:
    return tuple(parse_slot(item) for item in (raw or ()))


@dataclass(frozen=True)
class PublisherSettings:
    enabled: bool = True
    # Как часто процесс смотрит очередь. Молния уходит на ближайшем тике,
    # поэтому секунды здесь — это верхняя граница задержки срочного поста.
    poll_seconds: int = 20
    max_attempts: int = 3
    request_timeout_sec: int = 60
    # Сколько драфт может ждать своего слота, прежде чем протухнет: новость
    # города живёт часы, ставить её в завтрашний прайм-тайм бессмысленно.
    stale_after_hours: int = 12
    # Сервисные автопосты (спека §8). Это наш собственный контент без фактуры
    # источников — модерации не требует, но выключается одним флагом.
    service_enabled: bool = True
    weather_hour: int = 8            # локальное время города
    top_hour: int = 21
    top_size: int = 5
    # Ниже этого числа публикаций «топ дня» не собирается: список из одной
    # строки — не топ, а признак пустого дня (живой дефект первого вечера).
    top_min_items: int = 3
    weather_timeout_sec: int = 20
    # Подпись и кнопка под постом (PINS.md §3).
    signature: bool = True
    ugc_button: bool = True
    parse_mode: str = "HTML"


@dataclass(frozen=True)
class ModerationSettings:
    """Админ-бот (спека §7)."""

    enabled: bool = True
    poll_seconds: int = 10
    # Сколько карточек за раз выкладываем в чат модератора: очередь после ночи
    # может быть длинной, а чат — не лента.
    cards_per_tick: int = 3
    # §57: алерт «очередь не разобрана» считает только СРОЧНОЕ без решения
    # (молния, UGC, manual_only) — пул несрочного после §55 большой by design.
    queue_alert_size: int = 20
    # Пока очередь не разобрана, алерт повторяется не чаще этого срока.
    queue_alert_repeat_hours: int = 6
    # Молчащий источник: алерт по спеке §7.
    silent_source_hours: int = 24
    monitor_seconds: int = 900
    # §55: драфт без решения дольше этого срока протухает — новость города
    # старше суток мертва; молния, не вышедшая за 6 часов, молнией быть перестала.
    stale_hours: int = 24
    stale_flash_hours: int = 6
    # Сколько протухших карточек переписываем за тик: после ночи их десятки,
    # а Telegram ограничивает правки в одном чате.
    stale_per_tick: int = 20
    # §55: /queue без числа показывает сводку; кнопка под ней выдаёт столько.
    queue_default: int = 10
    moderators: tuple[int, ...] = field(default_factory=tuple)
