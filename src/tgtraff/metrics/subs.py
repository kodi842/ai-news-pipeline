"""Снимки канала: подписчики, охват, ER (спека §9).

Подписчиков отдаёт Bot API — админ-бот и так админ канала, отдельных прав не
нужно. Охват и ER считаются из просмотров, которые снял юзербот (`views.py`):
пока просмотров нет, снимок всё равно пишется — подписчики и их прирост нужны
гейту месяца 2 сами по себе.

Снимки не перезаписываются: каждая строка `subs_metrics` — точка на графике,
и суточный прирост считается сравнением с точкой сутками раньше.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from tgtraff.metrics import store
from tgtraff.metrics.settings import MetricsSettings

log = logging.getLogger("tgtraff.metrics.subs")


class SubsCollector:
    def __init__(self, db, bot, channels, settings: MetricsSettings | None = None,
                 alerter=None):
        self.db = db
        self.bot = bot
        self.channels = channels
        self.settings = settings or MetricsSettings()
        self.alerter = alerter
        self._alerted: set[str] = set()

    async def tick(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        window = now - timedelta(hours=self.settings.reach_window_hours)
        saved = 0
        for slug, city in self.channels.cities.items():
            chat_id = city.tg.chat_id
            if not chat_id:
                continue
            try:
                subscribers = int(await self.bot.get_chat_member_count(chat_id))
            except Exception as exc:              # noqa: BLE001 — бота могли снять с админов
                await self._complain(slug, exc)
                continue
            self._alerted.discard(slug)

            reach = store.reach(self.db, slug, window, now)
            er = None
            if subscribers and reach["avg"] is not None:
                er = round(reach["avg"] / subscribers, 4)
            store.save_snapshot(self.db, slug, str(chat_id), subscribers=subscribers,
                                views_24h=reach["views"], er=er, posts_24h=reach["posts"],
                                captured_at=now)
            saved += 1
        return saved

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:              # noqa: BLE001
                log.exception("снимок канала упал: %s", exc)
            await asyncio.sleep(self.settings.subs_poll_seconds)

    async def _complain(self, slug: str, exc: Exception) -> None:
        log.warning("%s: подписчики не сняты (%s)", slug, exc)
        if slug in self._alerted or self.alerter is None:
            return
        self._alerted.add(slug)
        await self.alerter.alert(
            f"{slug}: не читаются подписчики канала ({exc}) — метрики гейта "
            f"остаются без базы. Проверьте, что бот всё ещё админ канала")
