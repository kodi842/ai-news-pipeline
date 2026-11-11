"""Просмотры собственных постов (спека §9).

Bot API просмотры постов канала не отдаёт (QUESTIONS §39) — их видит только
клиент MTProto, то есть наш юзербот. Это ЧТЕНИЕ и ничего кроме: пост уже
опубликован Bot API, юзербот лишь смотрит на счётчик под ним.

Отсюда и место съёма: процесс коллектора, потому что session-файл держит один
процесс. Publisher про эту работу не знает — они общаются через базу, как и
всё остальное в конвейере.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tgtraff.metrics import store
from tgtraff.metrics.settings import MetricsSettings

log = logging.getLogger("tgtraff.metrics.views")


@dataclass(frozen=True)
class ViewsSnapshot:
    """Ответ юзербота: у каких сообщений сколько просмотров и каких сообщений
    в канале уже нет.

    Второе — не мелочь: пост, удалённый в клиенте руками, мимо нашей карты
    draft → message_id, иначе остаётся в базе живым навсегда. Мы будем
    спрашивать про него просмотры каждые 15 минут, а вечерний топ-5 однажды
    поставит на него ссылку в никуда."""

    views: dict[int, int] = field(default_factory=dict)
    missing: set[int] = field(default_factory=set)


class ViewsCollector:
    def __init__(self, db, reader, settings: MetricsSettings | None = None, alerter=None):
        self.db = db
        self.reader = reader
        self.settings = settings or MetricsSettings()
        self.alerter = alerter
        self._alerted: set[str] = set()

    async def tick(self, now: datetime | None = None) -> int:
        """Снимает просмотры пачкой на канал. Возвращает число обновлённых постов."""
        now = now or datetime.now(timezone.utc)
        rows = store.posts_for_views(self.db, now, self.settings.views_window_hours,
                                     self.settings.views_batch)
        if not rows:
            return 0

        # Один запрос на канал, а не на пост: аккаунт живой, лишние запросы ему
        # ни к чему (та же логика, что у батчей ВК в фазе 2).
        by_channel: dict[str, dict[int, int]] = defaultdict(dict)
        for row in rows:
            try:
                by_channel[row["channel"]][int(row["message_id"])] = row["id"]
            except (TypeError, ValueError):
                log.debug("пост #%s: message_id не число — пропущен", row["id"])

        saved = 0
        for channel, wanted in by_channel.items():
            try:
                snapshot = await self.reader.views(channel, sorted(wanted))
            except Exception as exc:              # noqa: BLE001 — канал мог быть недоступен
                await self._complain(channel, exc)
                continue
            self._forget(channel)
            for message_id, count in snapshot.views.items():
                published_id = wanted.get(int(message_id))
                if published_id is None or count is None:
                    continue
                store.save_views(self.db, published_id, int(count))
                saved += 1
            self._forget_missing(channel, snapshot.missing, wanted)
        if saved:
            log.info("просмотры сняты у %s постов", saved)
        return saved

    def _forget_missing(self, channel: str, missing: set[int], wanted: dict[int, int]) -> None:
        """Сообщения, которых в канале больше нет: пост удалили руками, минуя
        админ-бота. Отмечаем удалённым — и перестаём о нём спрашивать."""
        for message_id in missing:
            published_id = wanted.get(int(message_id))
            if published_id is None:
                continue
            store.mark_deleted(self.db, published_id)
            log.info("пост %s/%s удалён в канале — отмечен удалённым в базе",
                     channel, message_id)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:              # noqa: BLE001 — метрики не роняют коллектор
                log.exception("съём просмотров упал: %s", exc)
            await asyncio.sleep(self.settings.views_poll_seconds)

    # ---------- служебное ----------

    async def _complain(self, channel: str, exc: Exception) -> None:
        """Молчащие метрики — худший вид метрик: если юзербота не пустили в наш
        канал, об этом должен узнать человек, а не лог."""
        log.warning("просмотры канала %s не сняты: %s", channel, exc)
        if channel in self._alerted or self.alerter is None:
            return
        self._alerted.add(channel)
        await self.alerter.alert(
            f"просмотры канала {channel} не снимаются ({exc}). Юзербот должен быть "
            f"подписан на наш канал — иначе топ-5 дня останется без просмотров")

    def _forget(self, channel: str) -> None:
        self._alerted.discard(channel)


class TelethonViews:
    """Чтение счётчика просмотров через юзербот.

    Клиент — `ReadOnlyTelegramClient`: запросов на запись он физически не
    пропустит, а `GetMessages` под запрет не попадает."""

    def __init__(self, client):
        self.client = client

    async def views(self, channel, message_ids) -> ViewsSnapshot:
        entity = await self.client.get_entity(_peer(channel))
        wanted = list(message_ids)
        messages = await self.client.get_messages(entity, ids=wanted)
        views, missing = {}, set()
        # Telethon держит порядок ответа: на месте несуществующего сообщения
        # приходит None — по нему и видно, что пост из канала убрали.
        for message_id, message in zip(wanted, messages):
            if message is None:
                missing.add(message_id)
            elif message.views is not None:
                views[message.id] = message.views
        return ViewsSnapshot(views=views, missing=missing)


def _peer(channel):
    """`published.channel` хранит chat_id Bot API (строкой). Telethon ждёт int."""
    text = str(channel)
    if text.lstrip("-").isdigit():
        return int(text)
    return text
