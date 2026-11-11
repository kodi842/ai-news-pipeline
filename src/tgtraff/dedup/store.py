"""Хранилище дедупа: подписи постов, кластеры и их участники.

Инкрементальность держится здесь: вектор и хэш считаются один раз на пост и
ложатся в `post_signatures`, а новый пост сравнивается только с окном 48 часов
своего города. Пересчёта всей ленты не бывает ни при каких условиях.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
# В таком виде даты лежат в raw_posts.published_at — сравнение окна строковое,
# поэтому формат обязан совпадать до символа.
TS_FMT = "%Y-%m-%dT%H:%M:%S+00:00"


def parse_ts(value: str) -> datetime:
    """ISO8601 из базы → datetime в UTC (форматы `+00:00` и `Z` оба валидны)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TS_FMT)


def window_bounds(at: str, hours: int) -> tuple[str, str]:
    """Границы окна вокруг поста. Окно симметричное: догон приносит посты
    не по порядку, и новость, добранная позже соседей, обязана к ним приклеиться."""
    center = parse_ts(at)
    delta = timedelta(hours=hours)
    return format_ts(center - delta), format_ts(center + delta)


# ---------- векторы ----------

def pack_vector(vec: np.ndarray | None) -> bytes | None:
    if vec is None:
        return None
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack_vector(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32)


# ---------- подписи ----------

@dataclass(frozen=True)
class Signature:
    raw_post_id: int
    city: str
    source_id: int
    published_at: str
    text_hash: str | None
    text_chars: int
    embedding: np.ndarray | None
    entities: frozenset[str] = frozenset()
    cluster_id: int | None = None
    source_key: str | None = None
    source_type: str | None = None


def has_signature(conn, raw_post_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM post_signatures WHERE raw_post_id=?", (raw_post_id,)
    ).fetchone() is not None


def save_signature(conn, raw_post_id: int, city: str, source_id: int, published_at: str,
                   text_hash: str | None, text_chars: int,
                   embedding: np.ndarray | None, embed_model: str | None,
                   entities: set[str] | None = None) -> None:
    """Подпись пишется один раз: повторный проход по ленте её не трогает."""
    conn.execute(
        f"""INSERT INTO post_signatures
                (raw_post_id, city, source_id, published_at, text_hash, text_chars,
                 embedding, embed_model, entities, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,{NOW_SQL})
            ON CONFLICT(raw_post_id) DO NOTHING""",
        (raw_post_id, city, source_id, published_at, text_hash, text_chars,
         pack_vector(embedding), embed_model, " ".join(sorted(entities or ()))),
    )


def window_signatures(conn, city: str, at: str, hours: int, model: str | None = None,
                      exclude_post_id: int | None = None) -> list[Signature]:
    """Подписи окна: только этот город, только этот же вектор-модель.

    Города не смешиваются: одна и та же новость про две области — это два
    разных канала и два разных кластера."""
    since, until = window_bounds(at, hours)
    sql = [
        """SELECT s.raw_post_id, s.city, s.source_id, s.published_at, s.text_hash,
                  s.text_chars, s.embedding, s.entities, m.cluster_id, src.key AS source_key,
                  src.source_type
           FROM post_signatures s
           LEFT JOIN cluster_members m ON m.raw_post_id = s.raw_post_id
           LEFT JOIN sources src ON src.id = s.source_id
           WHERE s.city = ? AND s.published_at BETWEEN ? AND ?"""
    ]
    params: list = [city, since, until]
    if model:
        sql.append("AND (s.embed_model = ? OR s.embedding IS NULL)")
        params.append(model)
    if exclude_post_id is not None:
        sql.append("AND s.raw_post_id <> ?")
        params.append(exclude_post_id)

    rows = conn.execute(" ".join(sql), params).fetchall()
    return [
        Signature(
            raw_post_id=r["raw_post_id"], city=r["city"], source_id=r["source_id"],
            published_at=r["published_at"], text_hash=r["text_hash"],
            text_chars=r["text_chars"], embedding=unpack_vector(r["embedding"]),
            entities=frozenset((r["entities"] or "").split()),
            cluster_id=r["cluster_id"], source_key=r["source_key"],
            source_type=r["source_type"],
        )
        for r in rows
    ]


# ---------- перцептивные хэши медиа ----------

@dataclass(frozen=True)
class MediaHash:
    media_id: int
    raw_post_id: int
    source_id: int
    phash: str
    cluster_id: int | None = None


def save_phash(conn, media_id: int, phash: str | None) -> None:
    conn.execute("UPDATE raw_media SET phash=? WHERE id=?", (phash, media_id))


def post_media_to_hash(conn, raw_post_ids: list[int]) -> list[tuple[int, str]]:
    """Скачанные картинки поста, у которых хэш ещё не посчитан: (media_id, path)."""
    if not raw_post_ids:
        return []
    q = ",".join("?" * len(raw_post_ids))
    rows = conn.execute(
        f"""SELECT id, file_path FROM raw_media
            WHERE raw_post_id IN ({q}) AND status='done' AND phash IS NULL
              AND file_path IS NOT NULL AND kind='photo'""",
        raw_post_ids,
    ).fetchall()
    return [(r["id"], r["file_path"]) for r in rows]


def post_phashes(conn, raw_post_ids: list[int]) -> list[str]:
    if not raw_post_ids:
        return []
    q = ",".join("?" * len(raw_post_ids))
    rows = conn.execute(
        f"SELECT phash FROM raw_media WHERE raw_post_id IN ({q}) AND phash IS NOT NULL",
        raw_post_ids,
    ).fetchall()
    return [r["phash"] for r in rows]


def text_hash_repeats(conn, city: str, text_hash: str) -> int:
    """Сколько постов города уже приходили ровно с этим текстом.

    Объявления («введён режим Беспилотная опасность», «отключение воды»)
    повторяются слово в слово при каждом новом событии: по такому тексту можно
    склеивать только внутри эпизода, иначе вчерашняя тревога станет сегодняшней."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM post_signatures WHERE city=? AND text_hash=?",
        (city, text_hash),
    ).fetchone()
    return row["c"] if row else 0


def cluster_bounds(conn, cluster_id: int) -> tuple[str | None, str | None]:
    """Первый и последний пост кластера — по ним считается его возраст."""
    row = conn.execute(
        """SELECT MIN(p.published_at) a, MAX(p.published_at) b
           FROM cluster_members m JOIN raw_posts p ON p.id = m.raw_post_id
           WHERE m.cluster_id = ?""", (cluster_id,)).fetchone()
    return (row["a"], row["b"]) if row else (None, None)


def window_phashes(conn, city: str, at: str, hours: int,
                   exclude_post_id: int | None = None) -> list[MediaHash]:
    since, until = window_bounds(at, hours)
    # Через post_heads медиа альбома принадлежит своему логическому посту:
    # картинка из второго сообщения альбома ищется наравне с первой.
    sql = """SELECT m.id AS media_id, ph.head_id AS raw_post_id, m.phash, p.source_id,
                    cm.cluster_id
             FROM raw_media m
             JOIN raw_posts p ON p.id = m.raw_post_id
             JOIN post_heads ph ON ph.raw_post_id = m.raw_post_id
             JOIN post_signatures s ON s.raw_post_id = ph.head_id
             LEFT JOIN cluster_members cm ON cm.raw_post_id = ph.head_id
             WHERE m.phash IS NOT NULL AND s.city = ?
               AND s.published_at BETWEEN ? AND ?"""
    params: list = [city, since, until]
    if exclude_post_id is not None:
        sql += " AND ph.head_id <> ?"
        params.append(exclude_post_id)
    return [
        MediaHash(media_id=r["media_id"], raw_post_id=r["raw_post_id"],
                  source_id=r["source_id"], phash=r["phash"], cluster_id=r["cluster_id"])
        for r in conn.execute(sql, params).fetchall()
    ]


def phash_usage_by_source(conn, phash: str, source_id: int) -> int:
    """Сколько постов ЭТОГО источника несут ту же картинку.

    Шапка рубрики, логотип и заставка приходят с каждым постом паблика —
    склеивать по ним нельзя, иначе весь источник схлопнется в один кластер."""
    row = conn.execute(
        """SELECT COUNT(DISTINCT m.raw_post_id) c FROM raw_media m
           JOIN raw_posts p ON p.id = m.raw_post_id
           WHERE m.phash = ? AND p.source_id = ?""",
        (phash, source_id),
    ).fetchone()
    return row["c"] if row else 0


# ---------- кластеры ----------

def create_cluster(conn, city: str, lead_raw_post_id: int, text_hash: str | None,
                   seen_at: str) -> int:
    cur = conn.execute(
        f"""INSERT INTO clusters (city, status, lead_raw_post_id, text_hash, size,
                                  first_seen_at, last_seen_at, created_at)
            VALUES (?, 'open', ?, ?, 0, ?, ?, {NOW_SQL})""",
        (city, lead_raw_post_id, text_hash, seen_at, seen_at),
    )
    return cur.lastrowid


def add_member(conn, cluster_id: int, raw_post_id: int, similarity: float | None,
               reason: str, matched_raw_post_id: int | None, seen_at: str) -> bool:
    """Возвращает True, если участник действительно добавлен (а не был там раньше)."""
    cur = conn.execute(
        f"""INSERT OR IGNORE INTO cluster_members
                (cluster_id, raw_post_id, similarity, reason, matched_raw_post_id, added_at)
            VALUES (?,?,?,?,?,{NOW_SQL})""",
        (cluster_id, raw_post_id, similarity, reason, matched_raw_post_id),
    )
    if not cur.rowcount:
        return False
    conn.execute(
        """UPDATE clusters
              SET size = (SELECT COUNT(*) FROM cluster_members WHERE cluster_id = ?),
                  last_seen_at = MAX(COALESCE(last_seen_at,''), ?),
                  first_seen_at = MIN(first_seen_at, ?)
            WHERE id = ?""",
        (cluster_id, seen_at, seen_at, cluster_id),
    )
    return True


def cluster_of(conn, raw_post_id: int) -> int | None:
    row = conn.execute(
        "SELECT cluster_id FROM cluster_members WHERE raw_post_id=?", (raw_post_id,)
    ).fetchone()
    return row["cluster_id"] if row else None


def cluster_members(conn, cluster_id: int) -> list[int]:
    return [r["raw_post_id"] for r in conn.execute(
        "SELECT raw_post_id FROM cluster_members WHERE cluster_id=? ORDER BY added_at, raw_post_id",
        (cluster_id,))]


def cluster_draft(conn, cluster_id: int):
    """Драфт кластера, если он уже заведён (фазы 4-5).

    Спека §3: пост, попавший в кластер с уже опубликованным драфтом, новой
    публикации не порождает — фактура добавляется в кластер и всё."""
    return conn.execute(
        """SELECT id, status FROM drafts WHERE cluster_id=? AND status != 'superseded'
           ORDER BY CASE status WHEN 'published' THEN 0 WHEN 'approved' THEN 1
                                WHEN 'queued' THEN 2 WHEN 'new' THEN 3 ELSE 4 END, id
           LIMIT 1""",
        (cluster_id,),
    ).fetchone()


def close_stale_clusters(conn, before: str) -> int:
    """Кластеры, чьё окно закончилось, закрываются: новых постов в них не будет."""
    cur = conn.execute(
        "UPDATE clusters SET status='closed' WHERE status='open' AND last_seen_at < ?",
        (before,),
    )
    return cur.rowcount


def unclustered_posts(conn, city: str | None = None, limit: int = 500) -> list[int]:
    """Посты с подписью, но без кластера — след сбоя; в норме список пуст."""
    sql = """SELECT s.raw_post_id FROM post_signatures s
             LEFT JOIN cluster_members m ON m.raw_post_id = s.raw_post_id
             WHERE m.cluster_id IS NULL"""
    params: list = []
    if city:
        sql += " AND s.city = ?"
        params.append(city)
    sql += " ORDER BY s.published_at LIMIT ?"
    params.append(limit)
    return [r["raw_post_id"] for r in conn.execute(sql, params)]
