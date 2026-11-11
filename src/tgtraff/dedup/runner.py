"""Прогон дедупа по ленте: что брать в работу и в каком порядке.

Дедуп — отдельный процесс, а не задача коллектора: модель эмбеддингов держит
в памяти сотни мегабайт и считает на CPU, а коллектор обязан оставаться
отзывчивым (норматив доставки ЧП — минута). Общение через ту же базу.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from tgtraff.dedup import store
from tgtraff.dedup.cluster import Deduplicator
from tgtraff.normalize import NormalizedItem, glue_albums

log = logging.getLogger("tgtraff.dedup.runner")


@dataclass
class RunStats:
    processed: int = 0
    new_clusters: int = 0
    merged: int = 0
    deferred: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def add(self, decision) -> None:
        self.processed += 1
        self.by_reason[decision.reason] = self.by_reason.get(decision.reason, 0) + 1
        if decision.is_new_cluster:
            self.new_clusters += 1
        else:
            self.merged += 1

    def line(self) -> str:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(self.by_reason.items())) or "—"
        return (f"обработано {self.processed}, новых кластеров {self.new_clusters}, "
                f"склеек {self.merged} ({reasons}), отложено {self.deferred}")


def pending_feed(db, city: str | None = None, limit: int = 300,
                 since: str | None = None) -> list[NormalizedItem]:
    """Посты, ещё не попавшие в кластер, — в хронологическом порядке.

    Хронология важна: кластер должен начинаться с самой ранней публикации новости.
    Хвосты альбомов не считаются отдельными постами (см. вид `post_heads`),
    поэтому обработанный альбом в очередь не возвращается.
    """
    where = ["p.filtered_out = 0", "cm.cluster_id IS NULL"]
    params: list = []
    if city:
        where.append("s.city = ?")
        params.append(city)
    if since:
        where.append("p.published_at >= ?")
        params.append(since)

    rows = db.conn.execute(
        f"""SELECT p.id AS raw_post_id, p.source_id, p.source_type, p.external_id,
                   p.grouped_id, p.text, p.url, p.published_at, p.views,
                   s.city, s.key AS source_key
            FROM raw_posts p
            JOIN sources s ON s.id = p.source_id
            JOIN post_heads ph ON ph.raw_post_id = p.id
            LEFT JOIN cluster_members cm ON cm.raw_post_id = ph.head_id
            WHERE {" AND ".join(where)}
            ORDER BY p.published_at, p.id
            LIMIT ?""",
        (*params, limit),
    ).fetchall()
    if not rows:
        return []

    rows = _with_album_siblings(db, [dict(r) for r in rows])
    items = glue_albums(_attach_media(db, rows))
    items.reverse()          # glue_albums отдаёт свежие первыми, нам нужны ранние
    return items


def _with_album_siblings(db, rows: list[dict]) -> list[dict]:
    """Дотягивает остальные сообщения альбома: лимит выборки не должен резать
    альбом пополам, иначе одна новость станет двумя постами."""
    groups = {(r["source_id"], r["grouped_id"]) for r in rows if r["grouped_id"]}
    if not groups:
        return rows
    known = {r["raw_post_id"] for r in rows}
    for source_id, grouped_id in groups:
        for extra in db.conn.execute(
            """SELECT p.id AS raw_post_id, p.source_id, p.source_type, p.external_id,
                      p.grouped_id, p.text, p.url, p.published_at, p.views,
                      s.city, s.key AS source_key
               FROM raw_posts p JOIN sources s ON s.id = p.source_id
               WHERE p.source_id = ? AND p.grouped_id = ?""",
            (source_id, grouped_id),
        ):
            if extra["raw_post_id"] not in known:
                rows.append(dict(extra))
                known.add(extra["raw_post_id"])
    return rows


def _attach_media(db, rows: list[dict]) -> list[dict]:
    ids = [r["raw_post_id"] for r in rows]
    media: dict[int, list[dict]] = {}
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        q = ",".join("?" * len(chunk))
        for m in db.conn.execute(
            f"""SELECT raw_post_id, ord, kind, status, file_path FROM raw_media
                WHERE raw_post_id IN ({q}) ORDER BY raw_post_id, ord""", chunk
        ):
            media.setdefault(m["raw_post_id"], []).append(dict(m))
    return [dict(r, media=media.get(r["raw_post_id"], [])) for r in rows]


def run_once(db, dedup: Deduplicator, city: str | None = None, limit: int = 300,
             since: str | None = None) -> RunStats:
    items = pending_feed(db, city=city, limit=limit, since=since)
    stats = RunStats()
    for item in items:
        decision = dedup.process(item)
        if decision is None:
            stats.deferred += 1
            continue
        stats.add(decision)
    return stats


def close_stale(db, dedup: Deduplicator) -> int:
    """Закрывает кластеры, чьё окно закончилось: новых постов в них уже не будет."""
    before = store.format_ts(dedup.now() - timedelta(hours=dedup.settings.window_hours))
    return store.close_stale_clusters(db.conn, before)


def watch(db, dedup: Deduplicator, cities: list[str], once: bool = False,
          limit: int | None = None, stop=None) -> None:
    """Фоновый проход по ленте. Шаг ждём по календарю, а не монотонными часами:
    на ноутбуке процесс засыпает вместе с машиной (урок фазы 1, PROGRESS)."""
    step = dedup.settings.poll_seconds
    batch = limit or dedup.settings.batch_limit
    while True:
        for city in cities:
            stats = run_once(db, dedup, city=city, limit=batch)
            if stats.processed or stats.deferred:
                log.info("%s: %s", city, stats.line())
        closed = close_stale(db, dedup)
        if closed:
            log.info("закрыто кластеров вне окна: %s", closed)
        if once or (stop is not None and stop()):
            return
        deadline = datetime.now(timezone.utc) + timedelta(seconds=step)
        while datetime.now(timezone.utc) < deadline:
            if stop is not None and stop():
                return
            time.sleep(min(5, step))
