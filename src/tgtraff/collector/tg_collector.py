"""TG-коллектор: event-driven чтение источников юзерботом.

Спека, фаза 1: «Telethon-юзербот, event-driven (NewMessage), 30-50 каналов-источников
из конфига. Только чтение. Реконнект и алерт при разлогине.»

Как держатся гарантии приёмки:
  * «в течение минуты» — постим не по таймеру, а по событию NewMessage; запись поста
    не ждёт скачивания медиа (медиа качается отдельной очередью).
  * «рестарт не теряет» — при старте по каждому источнику догоняем сообщения после
    max(external_id) из БД, плюс досасываем незакачанное медиа.
  * «рестарт не дублирует» — UNIQUE(source_id, external_id) в raw_posts; догон и
    событийный поток могут пересечься, и это безопасно.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import events, utils as tl_utils
from telethon.errors import FloodWaitError, RPCError

from tgtraff.collector.ingest import normalize_message
from tgtraff.config import TG_SOURCE_TYPES, Config, SourceSpec
from tgtraff.db import Database, InsertResult

log = logging.getLogger("tgtraff.collector")


@dataclass
class RuntimeSource:
    spec: SourceSpec
    source_id: int
    entity: object | None = None
    peer_id: int | None = None


def store_message(db: Database, rs: "RuntimeSource", message, via: str = "live") -> InsertResult:
    """Синхронная запись поста Telegram в raw_posts. Медиа только ставится в очередь —
    скачивание не должно задерживать появление поста в базе.

    Функция модуля, а не метод: её зовут и событийный коллектор, и поллинг-фолбэк
    прогрева (collector/tg_polling.py) — запись должна быть одна на оба пути."""
    post = normalize_message(message, rs.spec)
    res = db.insert_raw_post(
        source_id=rs.source_id,
        source_type=rs.spec.source_type,
        external_id=post.external_id,
        grouped_id=post.grouped_id,
        text=post.text,
        url=post.url,
        published_at=post.published_at,
        edited_at=post.edited_at,
        views=post.views,
        fwd_from=post.fwd_from,
        has_media=int(post.has_media),
        media_state=post.media_state,
        filtered_out=int(post.filtered_out),
        filter_reason=post.filter_reason,
        raw_json=json.dumps({"grouped_id": post.grouped_id}, ensure_ascii=False),
        via=via,
    )
    if res.is_new:
        for ref in post.media:
            db.add_media(res.id, ord=ref.ord, kind=ref.kind, source_url=ref.url)
    return res


class TgCollector:
    def __init__(self, client, db: Database, cfg: Config, media_dir: Path | None = None):
        self.client = client
        self.db = db
        self.cfg = cfg
        self.media_dir = Path(media_dir or cfg.paths.media_dir)
        self.sources: dict[int, RuntimeSource] = {}      # source_id -> RuntimeSource
        self._by_peer: dict[int, RuntimeSource] = {}     # event.chat_id -> RuntimeSource
        # Момент старта процесса: всё, что опубликовано раньше, живой доставкой
        # не является, даже если пришло обработчиком NewMessage (см. on_new_message).
        self.started_at = datetime.now(timezone.utc)

    # ---------- источники ----------

    def register_source(self, rs: RuntimeSource) -> None:
        self.sources[rs.source_id] = rs
        if rs.entity is not None:
            # тот же формат, в котором приходит event.chat_id (-100<channel_id>)
            self._by_peer[tl_utils.get_peer_id(rs.entity)] = rs

    def _known_keys(self) -> set[tuple[str, str]]:
        return {(rs.spec.city, rs.spec.key) for rs in self.sources.values()}

    async def prepare_sources(self) -> list[RuntimeSource]:
        """Заводит источники в БД и разрешает их peer'ы. Источник, к которому нет
        доступа (ещё не подписаны / приватный), пропускается с предупреждением —
        коллектор из-за него не падает."""
        known = self._known_keys()
        prepared: list[RuntimeSource] = []
        for spec in self.cfg.collectable_tg_sources():
            if (spec.city, spec.key) in known:
                continue
            source_id = self.db.upsert_source(spec)
            try:
                entity = await self.client.get_entity(spec.key)
            except FloodWaitError as e:
                log.warning("FloodWait %s c на @%s — ждём", e.seconds, spec.key)
                await asyncio.sleep(e.seconds + 5)
                continue
            except (RPCError, ValueError, TypeError) as e:
                log.warning("источник @%s (%s) недоступен: %s: %s — пропускаем",
                            spec.key, spec.city, type(e).__name__, e)
                continue

            peer_id = getattr(entity, "id", None)
            rs = RuntimeSource(spec=spec, source_id=source_id, entity=entity, peer_id=peer_id)
            self.db.set_source_peer(source_id, peer_id)
            self.register_source(rs)
            prepared.append(rs)
        log.info("источников к чтению: %d из %d в конфиге",
                 len(self.sources), len(self.cfg.collectable_tg_sources()))
        return prepared

    async def refresh_sources(self) -> list[RuntimeSource]:
        """Подхватывает источники, на которые юзербот подписался уже после старта.
        Прогрев идёт по 5-10 каналов в день несколько суток — коллектор не должен
        требовать рестарта после каждой подписки."""
        added = await self.prepare_sources()
        if added:
            log.info("подхвачены новые источники: %s", ", ".join(f"@{rs.spec.key}" for rs in added))
        return added

    async def sources_worker(self, interval: int = 600) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                if await self.refresh_sources():
                    await self.catchup_all()
            except Exception:
                log.exception("сбой при обновлении списка источников")

    # ---------- запись ----------

    def store_message(self, rs: RuntimeSource, message, via: str = "live") -> InsertResult:
        return store_message(self.db, rs, message, via)

    # ---------- догон после простоя ----------

    def catchup_min_id(self, rs: RuntimeSource) -> int:
        return self.db.last_seen_external_id(rs.source_id) or 0

    async def catchup_source(self, rs: RuntimeSource) -> int:
        """Добирает сообщения, вышедшие пока процесс был не в сети."""
        min_id = self.catchup_min_id(rs)
        limit = self.cfg.collector.catchup_limit
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.cfg.collector.catchup_hours)
        new = 0
        try:
            async for message in self.client.iter_messages(rs.entity, min_id=min_id, limit=limit):
                published = message.date
                if published is not None:
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                    if published < cutoff:
                        break          # история идёт от свежих к старым — дальше только старее
                if self.store_message(rs, message, via="catchup").is_new:
                    new += 1
        except FloodWaitError as e:
            log.warning("FloodWait %s c при догоне @%s", e.seconds, rs.spec.key)
            await asyncio.sleep(e.seconds + 5)
        except RPCError as e:
            log.warning("догон @%s не удался: %s", rs.spec.key, e)
        if new:
            log.info("догон @%s: +%d постов (message_id > %d, не старше %d ч)",
                     rs.spec.key, new, min_id, self.cfg.collector.catchup_hours)
        return new

    async def catchup_all(self) -> int:
        total = 0
        for rs in list(self.sources.values()):
            total += await self.catchup_source(rs)
        log.info("догон завершён: %d новых постов", total)
        return total

    # ---------- медиа ----------

    def _media_path(self, rs: RuntimeSource, external_id: str, ord: int) -> Path:
        d = self.media_dir / rs.spec.city / rs.spec.key
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{external_id}_{ord}"

    def drop_stale_media(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.cfg.collector.media_backfill_hours)
        n = self.db.skip_stale_media(cutoff.isoformat())
        if n:
            log.info("медиа старше %d ч снято с очереди: %d файлов",
                     self.cfg.collector.media_backfill_hours, n)
        return n

    async def download_pending_media(self, limit: int = 200) -> int:
        """Очередь медиа из БД: качаем локально (перезалив своим файлом — требование
        CLAUDE.md, форвард запрещён). Очередь переживает рестарт.

        Берём только свои источники: медиа ВК и RSS качает HTTP-воркер фазы 2,
        Telethon его не вытянет и трогать не должен."""
        done = 0
        queue = self.db.pending_media(max_attempts=self.cfg.collector.media_max_attempts,
                                      limit=limit, source_types=TG_SOURCE_TYPES)
        for item in queue:
            rs = self._source_for(item)
            if rs is None:
                self.db.mark_media_skipped(item.media_id, "источник не активен в этом процессе")
                continue
            if await self._download_one(rs, item):
                done += 1
        return done

    def _source_for(self, item) -> RuntimeSource | None:
        for rs in self.sources.values():
            if rs.spec.key == item.source_key and rs.spec.city == item.source_city:
                return rs
        return None

    async def _download_one(self, rs: RuntimeSource, item) -> bool:
        try:
            message = await self.client.get_messages(rs.entity, ids=int(item.external_id))
            if isinstance(message, list):
                message = message[0] if message else None
            if message is None or getattr(message, "media", None) is None:
                self.db.mark_media_skipped(item.media_id, "сообщение или медиа больше недоступно")
                return False

            target = self._media_path(rs, item.external_id, item.ord)
            path = await asyncio.wait_for(
                self.client.download_media(message, file=str(target)),
                timeout=self.cfg.collector.media_download_timeout_sec,
            )
            if not path:
                self.db.mark_media_failed(item.media_id, "download_media вернул пусто")
                return False

            p = Path(path)
            size = p.stat().st_size
            if size > self.cfg.collector.media_max_bytes:
                p.unlink(missing_ok=True)
                self.db.mark_media_skipped(item.media_id, f"больше лимита {self.cfg.collector.media_max_mb} МБ")
                return False

            self.db.mark_media_downloaded(
                item.media_id, path=str(p), size=size,
                sha256=_sha256(p), mime=_guess_mime(p),
            )
            log.info("медиа @%s/%s → %s (%.0f КБ)", rs.spec.key, item.external_id, p.name, size / 1024)
            return True

        except FloodWaitError as e:
            self.db.mark_media_failed(item.media_id, f"FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds + 5)
        except asyncio.TimeoutError:
            self.db.mark_media_failed(item.media_id, "таймаут скачивания")
        except (RPCError, OSError, ValueError) as e:
            self.db.mark_media_failed(item.media_id, f"{type(e).__name__}: {e}")
            log.warning("медиа @%s/%s не скачалось: %s", rs.spec.key, item.external_id, e)
        return False

    async def media_worker(self, interval: int = 10) -> None:
        """Фоновая очередь медиа — отдельно от записи постов, чтобы тяжёлое видео
        не задерживало появление поста в raw_posts."""
        while True:
            try:
                self.drop_stale_media()
                self.db.purge_filtered_media()     # ретеншн §17
                await self.download_pending_media()
            except Exception:                      # воркер не должен убивать процесс
                log.exception("сбой в очереди медиа")
            await asyncio.sleep(interval)

    # ---------- событийный поток ----------

    def _source_for_event(self, event) -> RuntimeSource | None:
        return self._by_peer.get(getattr(event, "chat_id", None))

    def _via_for(self, message) -> str:
        """live только для того, что вышло уже при нас.

        После обрыва связи или рестарта Telethon добирает пропущенные апдейты
        (getDifference) и отдаёт их тем же обработчиком. Это догон: пометить его
        live — значит испортить замер «пост в базе в течение минуты» часами лага,
        которого в живой доставке не было."""
        published = getattr(message, "date", None)
        if published is None:
            return "live"
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return "live" if published >= self.started_at else "catchup"

    async def on_new_message(self, event) -> None:
        rs = self._source_for_event(event)
        if rs is None:
            return
        try:
            res = self.store_message(rs, event.message, via=self._via_for(event.message))
            if res.is_new:
                log.info("+ @%s/%s%s", rs.spec.key, event.message.id,
                         " [фильтр города]" if rs.spec.city_filter and
                         self.db.conn.execute("SELECT filtered_out FROM raw_posts WHERE id=?",
                                              (res.id,)).fetchone()[0] else "")
        except Exception:
            log.exception("не смог записать сообщение @%s/%s", rs.spec.key, getattr(event.message, "id", "?"))

    async def run(self) -> None:
        """Порядок важен: сначала подписываемся на события, потом догоняем пропущенное.
        Так сообщения, пришедшие во время догона, не теряются (а пересечение
        обезврежено UNIQUE-констрейнтом).

        Обработчик вешается без списка чатов, а маршрутизация идёт по peer_id: тогда
        источник, добавленный прогревом на ходу, начинает читаться без рестарта."""
        prepared = await self.prepare_sources()
        self.client.add_event_handler(self.on_new_message, events.NewMessage())
        if prepared:
            log.info("слушаю %d источников (event-driven, NewMessage)", len(prepared))
        else:
            log.warning("доступных источников пока нет — жду, пока прогрев подпишется "
                        "(scripts/warmup.py); список обновляется каждые 10 мин")

        await self.catchup_all()
        self.drop_stale_media()
        asyncio.create_task(self.media_worker())
        asyncio.create_task(self.sources_worker())
        await self.client.run_until_disconnected()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _guess_mime(path: Path) -> str | None:
    import mimetypes
    return mimetypes.guess_type(path.name)[0]
