"""Временный поллинг-фолбэк Telegram на время прогрева (QUESTIONS §8).

Событийная доставка (`NewMessage`) работает только по каналам, в которых юзербот
состоит. Плавный прогрев подписывает 5-10 каналов в день, то есть первые несколько
суток большая часть ленты живёт без live-доставки, хотя читать её мы можем
(`iter_messages` работает и без подписки).

Решение ревью: пока идёт прогрев, неподписанные источники опрашиваются раз в 3-5
минут — норматив доставки на это время ≤ 6 мин. Как только прогрев подписывает
источник, тот выпадает из опроса (дальше он приходит событием), а когда подписаны
все — модуль выключает сам себя. Ничего постоянного здесь нет по построению.

Темп важнее полноты: пачками по несколько каналов с паузой, FloodWait — «притормози»,
а не авария. Всплеск запросов с молодого аккаунта — ровно то, ради чего написан
плавный прогрев.
"""
from __future__ import annotations

import asyncio
import logging
import random

from telethon.errors import FloodWaitError

from tgtraff.collector.tg_collector import RuntimeSource, store_message
from tgtraff.config import Config
from tgtraff.db import Database

log = logging.getLogger("tgtraff.poll")


class PollingFallback:
    def __init__(self, client, db: Database, cfg: Config, store=None, source_provider=None):
        self.client = client
        self.db = db
        self.cfg = cfg
        self._own_sources: list[RuntimeSource] = []
        # Список источников берём у коллектора: прогрев подписывает каналы на ходу,
        # и коллектор добавляет их без рестарта — своя копия успела бы устареть.
        self._source_provider = source_provider or (lambda: self._own_sources)
        # Запись поста — общая с событийным коллектором (tg_collector.store_message):
        # фолбэк отвечает только за доставку, формат и дедуп у обоих путей одни.
        self._store = store or (lambda rs, message, via: store_message(self.db, rs, message, via))
        self.sleep = asyncio.sleep
        self._finished_logged = False

    # ---------- цели ----------

    @property
    def sources(self) -> list[RuntimeSource]:
        return list(self._source_provider())

    def register(self, rs: RuntimeSource) -> None:
        self._own_sources.append(rs)

    def register_all(self, sources) -> None:
        for rs in sources:
            self.register(rs)

    def _joined_ids(self) -> set[int]:
        rows = self.db.conn.execute("SELECT id FROM sources WHERE joined_at IS NOT NULL")
        return {r["id"] for r in rows}

    def targets(self) -> list[RuntimeSource]:
        """Источники, до которых событийная доставка ещё не дотягивается."""
        joined = self._joined_ids()
        return [rs for rs in self.sources if rs.source_id not in joined and rs.entity is not None]

    @property
    def is_finished(self) -> bool:
        """Прогрев подписал всё — фолбэк больше не нужен."""
        return bool(self.sources) and not self.targets()

    # ---------- опрос ----------

    async def poll_once(self) -> int:
        targets = self.targets()
        if not targets:
            return 0

        batch_size = self.cfg.poll_fallback.batch_size
        new = 0
        for i in range(0, len(targets), batch_size):
            if i:                                  # пауза между пачками, а не после последней
                await self.sleep(self.cfg.poll_fallback.batch_pause_sec)
            for rs in targets[i:i + batch_size]:
                new += await self._poll_source(rs)
        if new:
            log.info("поллинг-фолбэк: +%d постов из %d неподписанных источников", new, len(targets))
        return new

    async def _poll_source(self, rs: RuntimeSource) -> int:
        min_id = self.db.last_seen_external_id(rs.source_id) or 0
        new = 0
        try:
            async for message in self.client.iter_messages(
                rs.entity, min_id=min_id, limit=self.cfg.poll_fallback.limit_per_source
            ):
                if self._store(rs, message, via="poll").is_new:
                    new += 1
            self.db.mark_source_polled(rs.source_id)
        except FloodWaitError as e:
            self.db.mark_source_error(rs.source_id, f"FloodWait {e.seconds}s")
            log.warning("FloodWait %s c на @%s — фолбэк ждёт", e.seconds, rs.spec.key)
            await self.sleep(e.seconds + 5)
        except Exception as e:                     # один источник не должен ронять проход
            self.db.mark_source_error(rs.source_id, f"{type(e).__name__}: {e}")
            log.warning("поллинг @%s не удался: %s: %s", rs.spec.key, type(e).__name__, e)
        return new

    # ---------- цикл ----------

    async def run(self) -> None:
        if not self.cfg.poll_fallback.enabled:
            log.info("поллинг-фолбэк выключен конфигом")
            return
        log.info("поллинг-фолбэк включён: опрос неподписанных источников раз в %d-%d c",
                 self.cfg.poll_fallback.interval_min_sec, self.cfg.poll_fallback.interval_max_sec)
        while True:
            try:
                if self.is_finished:
                    log.info("прогрев подписал все источники — поллинг-фолбэк выключается, "
                             "дальше только live-доставка")
                    return
                # Считаем цели каждый цикл: при старте коллектор ещё только заводит
                # источники, а прогрев добавляет подписки на ходу.
                targets = self.targets()
                if targets:
                    log.info("поллинг-фолбэк: опрашиваю %d неподписанных источников", len(targets))
                await self.poll_once()
            except Exception:
                log.exception("сбой цикла поллинг-фолбэка")
            await self.sleep(random.randint(self.cfg.poll_fallback.interval_min_sec,
                                            self.cfg.poll_fallback.interval_max_sec))
