"""Скачивание медиа источников, живущих по HTTP (ВК, RSS).

CLAUDE.md: «Медиа перезаливается файлом» — публиковать чужую ссылку нельзя, файл
должен лежать у нас. У Telegram это делает Telethon (tg_collector), здесь — всё
остальное. Очередь общая (raw_media), воркеры разделены по типу источника.

Ретраи и лимиты берём те же, что у Telegram-очереди: media_max_attempts,
media_max_mb, media_download_timeout_sec.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import re
from pathlib import Path

from tgtraff.db import Database

log = logging.getLogger("tgtraff.media")

# Типы источников, чьё медиа качается по прямой ссылке.
HTTP_SOURCE_TYPES = ("vk", "rss", "official")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value: str, keep: int = 40) -> str:
    """Имя файла/каталога из произвольного ключа источника (у RSS это URL).

    Читаемый хвост + короткий хэш: каталоги остаются узнаваемыми глазом, а два
    разных URL не схлопываются в один путь."""
    cleaned = _UNSAFE.sub("_", value).strip("_")
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    if len(cleaned) <= keep and cleaned == value:
        return cleaned or digest
    return f"{cleaned[-keep:].strip('_')}_{digest}"


def _extension(url: str, content_type: str | None) -> str:
    from urllib.parse import urlparse

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and len(suffix) <= 5:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".bin"


class HttpMediaDownloader:
    def __init__(self, fetch, db: Database, cfg, media_dir: Path | None = None):
        self.fetch = fetch                       # async (url, timeout) -> (status, bytes, content_type)
        self.db = db
        self.cfg = cfg
        self.media_dir = Path(media_dir or cfg.paths.media_dir)

    async def download_pending(self, limit: int = 100) -> int:
        done = 0
        queue = self.db.pending_media(max_attempts=self.cfg.collector.media_max_attempts,
                                      limit=limit, source_types=HTTP_SOURCE_TYPES)
        for item in queue:
            if not item.source_url:
                # Видео ВК: файл сервисным ключом не отдают. Крутить в очереди нечего.
                self.db.mark_media_skipped(item.media_id, "прямой ссылки на файл нет (напр. видео ВК)")
                continue
            if await self._download_one(item):
                done += 1
        return done

    def _target(self, item, url: str, content_type: str | None) -> Path:
        directory = self.media_dir / safe_name(item.source_city) / safe_name(item.source_key)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{safe_name(item.external_id)}_{item.ord}{_extension(url, content_type)}"

    async def _download_one(self, item) -> bool:
        try:
            status, body, content_type = await asyncio.wait_for(
                self.fetch(item.source_url, timeout=self.cfg.collector.media_download_timeout_sec),
                timeout=self.cfg.collector.media_download_timeout_sec + 5,
            )
        except asyncio.TimeoutError:
            self.db.mark_media_failed(item.media_id, "таймаут скачивания")
            return False
        except Exception as e:                    # сеть/DNS/обрыв — соседям в очереди не мешаем
            self.db.mark_media_failed(item.media_id, f"{type(e).__name__}: {e}")
            log.warning("медиа %s не скачалось: %s: %s", item.source_url, type(e).__name__, e)
            return False

        if status != 200 or not body:
            self.db.mark_media_failed(item.media_id, f"HTTP {status}")
            return False
        if len(body) > self.cfg.collector.media_max_bytes:
            self.db.mark_media_skipped(item.media_id, f"больше лимита {self.cfg.collector.media_max_mb} МБ")
            return False

        try:
            path = self._target(item, item.source_url, content_type)
            path.write_bytes(body)
        except OSError as e:
            self.db.mark_media_failed(item.media_id, f"не записал файл: {e}")
            return False

        self.db.mark_media_downloaded(item.media_id, path=str(path), size=len(body),
                                      sha256=hashlib.sha256(body).hexdigest(),
                                      mime=(content_type or "").split(";")[0] or None)
        log.info("медиа %s → %s (%.0f КБ)", item.source_key, path.name, len(body) / 1024)
        return True

    async def run(self, interval: int = 15) -> None:
        while True:
            try:
                self.db.purge_filtered_media()     # ретеншн §17: чужой город на диске не держим
                await self.download_pending()
            except Exception:                     # воркер не должен убивать процесс
                log.exception("сбой HTTP-очереди медиа")
            await asyncio.sleep(interval)


def httpx_fetcher(timeout: int = 30):
    """Боевой загрузчик файлов. httpx импортируем лениво — тесты обходятся без него."""
    import httpx

    async def fetch(url: str, timeout: int = timeout):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url)
            return r.status_code, r.content, r.headers.get("content-type")

    return fetch


def httpx_feed_fetcher(timeout: int = 20):
    """Загрузчик лент: отдаёт статус, тело и заголовки (нужны ETag/Last-Modified)."""
    import httpx

    async def fetch(url: str, headers: dict | None = None, timeout: int = timeout):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, headers=headers or {})
            return r.status_code, r.content, dict(r.headers)

    return fetch
