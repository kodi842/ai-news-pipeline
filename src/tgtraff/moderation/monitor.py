"""Алерты конвейера в админ-бот (спека §7).

Спека перечисляет: источник молчит > 24 ч, юзербот разлогинен, VK-токен
протух, очередь переполнена, ошибки публикации. Разлогин юзербота ловит сам
коллектор (он падает и алертит), остальное видно из базы — этим и занят
монитор.

Два правила: алерты агрегируются (пять молчащих источников — одно сообщение,
а не пять) и не повторяются каждым тиком (гашение — в `Alerter`, здесь ещё
и своя память, чтобы не будить человека одним и тем же).

Гашение по тексту работает не везде: у «очередь переполнена» в сообщении стоит
число, и оно растёт каждым тиком — одинаковых сообщений просто не бывает.
Поэтому у этого алерта своя память по времени (§57).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from tgtraff.moderation import actions
from tgtraff.publisher import store
from tgtraff.publisher.settings import ModerationSettings

log = logging.getLogger("tgtraff.monitor")

# Сколько источников платформы должны отвечать ошибкой, чтобы это выглядело
# как протухший токен, а не как один закрытый паблик.
TOKEN_SUSPECT_MIN = 2


class Monitor:
    def __init__(self, db, alerter, settings: ModerationSettings | None = None):
        self.db = db
        self.alerter = alerter
        self.settings = settings or ModerationSettings()
        self._seen: set[str] = set()
        # §57: когда в последний раз жаловались на неразобранное срочное.
        self._queue_alert_at: datetime | None = None

    async def check(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        # `dedup=False` — у алерта своя память: гасить его по тексту нельзя,
        # текст меняется вместе с числом в очереди.
        checks = ((self._silent_sources(now), True),
                  (self._dead_token(), True),
                  (self._queue_overflow(now), False),
                  (self._failed_publications(), True),
                  (self._stale_queue(now), True))
        fresh = []
        for message, dedup in checks:
            if not message or (dedup and message in self._seen):
                continue
            if dedup:
                self._seen.add(message)
            fresh.append(message)
        for message in fresh:
            if self.alerter is not None:
                await self.alerter.alert(message)
        return fresh

    # ---------- проверки ----------

    def _silent_sources(self, now: datetime) -> str | None:
        cutoff = (now - timedelta(hours=self.settings.silent_source_hours)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.db.conn.execute(
            """SELECT key, city, last_post_at FROM sources
               WHERE enabled=1 AND status='verified'
                 AND last_post_at IS NOT NULL AND last_post_at < ?
               ORDER BY last_post_at LIMIT 20""", (cutoff,)).fetchall()
        if not rows:
            return None
        names = ", ".join(f"{r['key']} ({r['city']})" for r in rows[:8])
        tail = f" и ещё {len(rows) - 8}" if len(rows) > 8 else ""
        return (f"источников молчит дольше {self.settings.silent_source_hours} ч: "
                f"{len(rows)} — {names}{tail}")

    def _dead_token(self) -> str | None:
        rows = self.db.conn.execute(
            """SELECT source_type, COUNT(*) c FROM sources
               WHERE enabled=1 AND last_error IS NOT NULL GROUP BY source_type""").fetchall()
        for row in rows:
            if row["source_type"] == "vk" and row["c"] >= TOKEN_SUSPECT_MIN:
                return (f"ВК: ошибка сразу у {row['c']} пабликов — похоже, протух "
                        f"VK-токен (проверьте VK_SERVICE_TOKEN)")
        return None

    def _queue_overflow(self, now: datetime) -> str | None:
        """§57: «модератор не справляется» — это неразобранное СРОЧНОЕ.

        Считаем только то, что пушится карточкой (молния, UGC, manual_only).
        Пул несрочного после §55 большой by design — его модератор тянет сам
        по /queue, и алерт на его размер будил человека каждые 15 минут новым
        числом («481», «487», «492»).

        Шлём при пересечении порога вверх и далее, пока очередь не разобрана,
        не чаще раза в `queue_alert_repeat_hours`."""
        count = actions.urgent_waiting_count(self.db)
        if count < self.settings.queue_alert_size:
            return None
        since = self._queue_alert_at
        if since is not None and now - since < timedelta(
                hours=self.settings.queue_alert_repeat_hours):
            return None
        self._queue_alert_at = now
        return (f"очередь срочного не разобрана: {count} карточек ждут решения "
                f"(молнии, UGC, manual_only; порог {self.settings.queue_alert_size}). "
                f"Несрочный пул сюда не входит — он в /queue")

    def _failed_publications(self) -> str | None:
        rows = self.db.conn.execute(
            "SELECT id, draft_id, last_error FROM publish_queue WHERE status='failed' "
            "ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            return None
        first = rows[0]
        return (f"ошибки публикации: {len(rows)} постов не вышли, последний — драфт "
                f"#{first['draft_id']} ({(first['last_error'] or '')[:120]})")

    def _stale_queue(self, now: datetime) -> str | None:
        rows = store.stale(self.db, now, older_than_hours=12)
        if not rows:
            return None
        return (f"в очереди публикации протухло {len(rows)} постов — они ждут дольше "
                "12 часов, новость города столько не живёт")
