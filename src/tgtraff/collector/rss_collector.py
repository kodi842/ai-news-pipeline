"""RSS-коллектор: ленты городских СМИ и ведомств.

Спека, §1 Collector: «RSS/сайты: feedparser, поллинг раз в 5-10 мин (местные СМИ,
МЧС, ГИБДД, администрация, прокуратура)».

Два правила, которые здесь важнее скорости:
  * недоступная лента — штатное состояние, а не авария: ошибка пишется в
    `sources.last_error`, опрос остальных продолжается (критерий приёмки фазы 2);
  * условный GET (ETag / Last-Modified) — чтобы не тянуть одну и ту же ленту
    каждые семь минут и не мозолить глаза серверам СМИ.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone

from tgtraff.collector.rss_ingest import normalize_rss_entry
from tgtraff.config import Config, SourceSpec
from tgtraff.db import Database

log = logging.getLogger("tgtraff.rss")

# Типы источников, которые читает этот поллер: ленты СМИ и сайты ведомств.
RSS_SOURCE_TYPES = ("rss", "official")


def parse_feed(body: bytes) -> list[dict]:
    """Байты ленты → список записей. feedparser проглатывает и битый XML,
    поэтому пустой список здесь означает «это не лента» — так и трактуем."""
    import feedparser

    return list(feedparser.parse(body).entries or [])


class RssCollector:
    def __init__(self, fetch, db: Database, cfg: Config):
        self.fetch = fetch               # async (url, headers, timeout) -> (status, body, headers)
        self.db = db
        self.cfg = cfg
        self.sources: list[tuple[SourceSpec, int]] = []

    def register(self, specs: list[SourceSpec]) -> list[SourceSpec]:
        for spec in specs:
            self.sources.append((spec, self.db.upsert_source(spec)))
        return specs

    def prepare_sources(self) -> list[SourceSpec]:
        """Ленты активных городов. Записи со `status: resolve` (адрес ленты ещё не
        найден) сюда не попадают — их разбирает scripts/verify_sources.py."""
        specs = self.cfg.collectable_sources(*RSS_SOURCE_TYPES)
        self.register(specs)
        log.info("RSS-лент к опросу: %d", len(self.sources))
        return specs

    async def poll_once(self) -> int:
        new = 0
        for spec, source_id in self.sources:
            try:
                new += await self._poll_source(spec, source_id)
            except Exception:                       # одна лента не должна ронять проход
                log.exception("сбой обработки ленты %s", spec.key)
        if new:
            log.info("RSS: +%d записей", new)
        return new

    async def _poll_source(self, spec: SourceSpec, source_id: int) -> int:
        row = self.db.get_source(source_id)
        headers = {"User-Agent": self.cfg.rss.user_agent, "Accept": "application/rss+xml, application/xml, */*"}
        if row and row["http_etag"]:
            headers["If-None-Match"] = row["http_etag"]
        if row and row["http_last_modified"]:
            headers["If-Modified-Since"] = row["http_last_modified"]

        try:
            status, body, response_headers = await self.fetch(
                spec.key, headers=headers, timeout=self.cfg.rss.request_timeout_sec)
        except Exception as e:                      # таймаут, DNS, обрыв — источник, а не мы
            self.db.mark_source_error(source_id, f"{type(e).__name__}: {e}")
            log.warning("лента %s недоступна: %s: %s", spec.key, type(e).__name__, e)
            return 0

        if status == 304:                           # не менялась с прошлого раза
            self.db.mark_source_polled(source_id)
            return 0
        if status != 200:
            self.db.mark_source_error(source_id, f"HTTP {status}")
            log.warning("лента %s ответила HTTP %s", spec.key, status)
            return 0

        entries = parse_feed(body)
        if not entries:
            self.db.mark_source_error(source_id, "ответ не похож на RSS-ленту (0 записей)")
            log.warning("по адресу %s ленты нет — адрес нужно уточнить", spec.key)
            return 0

        response_headers = response_headers or {}
        self.db.mark_source_polled(
            source_id,
            etag=response_headers.get("ETag") or response_headers.get("etag"),
            last_modified=response_headers.get("Last-Modified") or response_headers.get("last-modified"),
        )

        fetched_at = datetime.now(timezone.utc).isoformat()
        new = 0
        for entry in entries[: self.cfg.rss.entries_per_feed]:
            new += self._store(spec, source_id, entry, fetched_at)
        return new

    def _store(self, spec: SourceSpec, source_id: int, entry: dict, fetched_at: str) -> int:
        post = normalize_rss_entry(entry, spec, fetched_at=fetched_at)
        if not post.external_id or post.is_empty:
            return 0
        res = self.db.insert_raw_post(
            source_id=source_id,
            source_type=spec.source_type,
            external_id=post.external_id,
            text=post.text,
            url=post.url,
            published_at=post.published_at,
            has_media=int(post.has_media),
            media_state=post.media_state,
            filtered_out=int(post.filtered_out),
            filter_reason=post.filter_reason,
            raw_json=json.dumps(post.extra, ensure_ascii=False) if post.extra else None,
            via="poll",
        )
        if not res.is_new:
            return 0
        for ref in post.media:
            self.db.add_media(res.id, ord=ref.ord, kind=ref.kind, source_url=ref.url)
        return 1

    async def run(self) -> None:
        self.prepare_sources()
        period = self.cfg.rss.poll_seconds
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("сбой цикла опроса RSS")
            await asyncio.sleep(period + random.uniform(0, period * 0.2))
