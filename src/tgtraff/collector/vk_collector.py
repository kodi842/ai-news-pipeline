"""ВК-коллектор: официальный API, `wall.get` батчами через `execute`.

Спека, §1 Collector: «VK: официальный API, wall.get батчами через execute
(до 25 пабликов/запрос), поллинг раз в 2-3 мин».

Почему батч: у ВК лимит на частоту запросов, а пабликов у трёх городов больше десятка.
`execute` выполняет до 25 вызовов API за один запрос — вся сетка пабликов опрашивается
одним обращением раз в три минуты.

Недоступность отдельного паблика (закрыт, удалён, бан) — штатная ситуация: VK кладёт
в его слот `false`, остальные ответы приходят как обычно. Такой источник помечается
`last_error` и не мешает остальным (критерий приёмки фазы 2).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

from tgtraff.collector.vk_ingest import normalize_vk_post
from tgtraff.config import Config, SourceSpec
from tgtraff.db import Database

log = logging.getLogger("tgtraff.vk")

API_URL = "https://api.vk.com/method/"
EXECUTE_BATCH = 25            # предел вызовов внутри одного execute
RATE_LIMIT_CODE = 6           # «Too many requests per second» — не отказ, а «подожди»


class VkError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"VK error {code}: {message}")
        self.code = code
        self.message = message


class VkApi:
    """Тонкая обёртка над VK API. Транспорт внедряется — в тестах вместо сети
    подставляются записанные ответы, включая ответы со сбоями."""

    def __init__(self, token: str, transport=None, api_version: str = "5.199",
                 retry_pause: float = 1.0, max_retries: int = 3):
        self.token = token
        self.api_version = api_version
        self.retry_pause = retry_pause
        self.max_retries = max_retries
        self._transport = transport or _httpx_transport()

    async def call(self, method: str, **params) -> dict:
        params = {**params, "access_token": self.token, "v": self.api_version}
        for attempt in range(self.max_retries):
            data = await self._transport(method, params)
            error = data.get("error")
            if not error:
                return data.get("response")
            code = error.get("error_code")
            if code == RATE_LIMIT_CODE and attempt + 1 < self.max_retries:
                await asyncio.sleep(self.retry_pause * (attempt + 1))
                continue
            raise VkError(code, error.get("error_msg", ""))
        raise VkError(RATE_LIMIT_CODE, "лимит частоты не отпустил после ретраев")

    async def wall_get_batch(self, domains: list[str], count: int = 20) -> dict[str, list | VkError]:
        """Стены нескольких пабликов за один запрос. Возвращает по домену либо список
        постов, либо VkError — чтобы вызывающий пометил именно этот источник."""
        out: dict[str, list | VkError] = {}
        for chunk in (domains[i:i + EXECUTE_BATCH] for i in range(0, len(domains), EXECUTE_BATCH)):
            code = (
                "var d = " + json.dumps(chunk, ensure_ascii=False) + ";"
                "var i = 0; var r = [];"
                "while (i < d.length) {"
                f'  r.push(API.wall.get({{"domain": d[i], "count": {int(count)}, "extended": 0}}));'
                "  i = i + 1;"
                "}"
                "return r;"
            )
            try:
                response = await self.call("execute", code=code)
            except VkError as e:                       # упал весь батч (напр. протух токен)
                out.update({d: e for d in chunk})
                continue

            errors = list((response or {}).get("execute_errors") or []) if isinstance(response, dict) else []
            items = response if isinstance(response, list) else (response or {}).get("items") or []
            for i, domain in enumerate(chunk):
                slot = items[i] if i < len(items) else False
                if not slot:
                    err = errors.pop(0) if errors else {}
                    out[domain] = VkError(err.get("error_code", 0),
                                          err.get("error_msg", "источник недоступен"))
                else:
                    out[domain] = slot.get("items", []) if isinstance(slot, dict) else slot
        return out


def _httpx_transport():
    """Боевой транспорт. httpx импортируем лениво — тесты обходятся без него."""
    import httpx

    async def transport(method: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(API_URL + method, data=params)
            r.raise_for_status()
            return r.json()

    return transport


class VkCollector:
    def __init__(self, api, db: Database, cfg: Config):
        self.api = api
        self.db = db
        self.cfg = cfg
        self.sources: dict[str, tuple[SourceSpec, int]] = {}   # screen_name -> (спека, source_id)

    def prepare_sources(self) -> list[SourceSpec]:
        """Заводит ВК-паблики активных городов в БД. status: resolve сюда не попадает —
        такие записи разбирает scripts/verify_sources.py."""
        specs = [s for city in self.cfg.active() for s in city.vk_sources if s.is_collectable]
        for spec in specs:
            self.sources[spec.key] = (spec, self.db.upsert_source(spec))
        log.info("ВК-источников к опросу: %d", len(self.sources))
        return specs

    async def poll_once(self) -> int:
        """Один проход по всем пабликам. Возвращает число новых постов.

        Сетевой сбой всего батча не должен ронять процесс: следующий проход
        через poll_seconds попробует снова."""
        if not self.sources:
            return 0
        try:
            batch = await self.api.wall_get_batch(list(self.sources),
                                                  count=self.cfg.vk.posts_per_source)
        except Exception as e:                     # транспорт/сеть — не наша вина, ждём следующий тик
            log.warning("опрос ВК не удался целиком: %s: %s", type(e).__name__, e)
            return 0

        new = 0
        for domain, result in batch.items():
            spec, source_id = self.sources[domain]
            if isinstance(result, Exception):
                self.db.mark_source_error(source_id, str(result))
                log.warning("ВК паблик %s недоступен: %s", domain, result)
                continue
            self.db.mark_source_polled(source_id)
            for item in result:
                try:
                    new += self._store(spec, source_id, item)
                except Exception:
                    log.exception("не смог записать пост ВК %s/%s", domain, item.get("id"))
        if new:
            log.info("ВК: +%d постов", new)
        return new

    def _store(self, spec: SourceSpec, source_id: int, item: dict) -> int:
        post = normalize_vk_post(item, spec)
        if post.is_empty:
            return 0
        res = self.db.insert_raw_post(
            source_id=source_id,
            source_type=spec.source_type,
            external_id=post.external_id,
            text=post.text,
            url=post.url,
            published_at=post.published_at,
            views=post.views,
            fwd_from=post.fwd_from,
            has_media=int(post.has_media),
            media_state=post.media_state,
            filtered_out=int(post.filtered_out),
            filter_reason=post.filter_reason,
            raw_json=json.dumps(post.extra, ensure_ascii=False),
            via="poll",
        )
        if not res.is_new:
            return 0
        for ref in post.media:
            self.db.add_media(res.id, ord=ref.ord, kind=ref.kind, source_url=ref.url)
        return 1

    async def run(self) -> None:
        """Долгоживущий поллинг. Джиттер — чтобы не долбить API ровно по секундам."""
        self.prepare_sources()
        period = self.cfg.vk.poll_seconds
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("сбой цикла опроса ВК")
            await asyncio.sleep(period + random.uniform(0, period * 0.2))
