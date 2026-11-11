"""Драфты: результат фазы 4 и вход модерации фазы 5.

Одна строка `drafts` = одна новость, готовая к показу модератору: хук, текст,
категория, выбранное медиа, вердикт safety и причина relevance. Правило спеки
§3 «пост в кластер с драфтом второй публикации не порождает» держится здесь
техническим ограничением: у кластера может быть только один драфт.
"""
from __future__ import annotations

import json

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


class DraftExists(RuntimeError):
    """У кластера уже есть драфт — второй публикации новость не порождает."""


def encode_list(values) -> str | None:
    if not values:
        return None
    return json.dumps(list(values), ensure_ascii=False)


def decode_list(value) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [str(value)]
    return list(parsed) if isinstance(parsed, list) else [str(parsed)]


# ---------- драфты ----------

def save_draft(db, cluster_id: int | None, city: str, title: str, body: str,
               category: str | None = None, urgency: str | None = None,
               safety_verdict: str | None = None, safety_reasons=None,
               flags=None, relevance: str | None = None,
               media_kind: str | None = None, media_reason: str | None = None,
               media_path: str | None = None, media_raw_media_id: int | None = None,
               sources=None, model: str | None = None, cost_usd: float | None = None,
               notes: str | None = None, status: str = "new",
               district: str | None = None, origin: str = "news",
               ugc_user_id: int | None = None, ugc_username: str | None = None) -> int:
    """Строка драфта. `origin` разделяет конвейерную новость, UGC от читателя
    и сервисный автопост — очередь и карточка модератора у них общие."""
    if cluster_id is not None and draft_for_cluster(db, cluster_id) is not None:
        raise DraftExists(f"у кластера #{cluster_id} драфт уже есть")
    cur = db.conn.execute(
        f"""INSERT INTO drafts (cluster_id, city, title, body, category, urgency,
                                safety_verdict, safety_reasons, flags, relevance,
                                media_kind, media_reason, media_path, media_raw_media_id,
                                sources, model, cost_usd, notes, status,
                                district, origin, ugc_user_id, ugc_username,
                                created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,{NOW_SQL},{NOW_SQL})""",
        (cluster_id, city, title, body, category, urgency, safety_verdict,
         encode_list(safety_reasons), encode_list(flags), relevance,
         media_kind, media_reason, media_path, media_raw_media_id,
         encode_list(sources), model, cost_usd, notes, status,
         district, origin, ugc_user_id, ugc_username),
    )
    return cur.lastrowid


def get_draft(db, draft_id: int):
    return db.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()


def draft_for_cluster(db, cluster_id: int):
    """Живой драфт кластера. `superseded` — драфт, уступивший место повторному
    предложению (§28): замок «у кластера уже есть драфт» он не держит."""
    return db.conn.execute(
        "SELECT * FROM drafts WHERE cluster_id=? AND status != 'superseded' "
        "ORDER BY id LIMIT 1", (cluster_id,)
    ).fetchone()


# ---------- relevance ----------

def set_cluster_relevance(db, cluster_id: int, verdict: str, reason: str | None = None) -> None:
    db.conn.execute(
        f"UPDATE clusters SET relevance=?, relevance_reason=?, relevance_at={NOW_SQL} WHERE id=?",
        (verdict, reason, cluster_id),
    )


def pending_clusters(db, city: str | None = None, limit: int = 100,
                     since: str | None = None, min_size: int = 1) -> list[int]:
    """Кластеры, по которым фаза 4 ещё не приняла решения.

    Разобранным считается кластер, у которого есть драфт ИЛИ вердикт relevance
    (отсеянному драфт не положен). Порядок — от старых к новым: очередь новостей
    хронологическая, как и у дедупа."""
    where = ["d.id IS NULL", "cl.relevance IS NULL", "cl.size >= ?"]
    params: list = [min_size]
    if city:
        where.append("cl.city = ?")
        params.append(city)
    if since:
        where.append("cl.first_seen_at >= ?")
        params.append(since)
    rows = db.conn.execute(
        f"""SELECT cl.id FROM clusters cl
            LEFT JOIN drafts d ON d.cluster_id = cl.id AND d.status != 'superseded'
            WHERE {' AND '.join(where)}
            ORDER BY cl.first_seen_at, cl.id
            LIMIT ?""", (*params, limit)).fetchall()
    return [r["id"] for r in rows]


# ---------- вотермарки ----------

def set_media_watermark(db, media_id: int, verdict: str, owner: str | None = None) -> None:
    """Вердикт vision-детектора кэшируется на строке медиа: одна картинка —
    один платный запрос за всю её жизнь."""
    db.conn.execute(
        f"""UPDATE raw_media SET watermark=?, watermark_owner=?,
                                 watermark_checked_at={NOW_SQL}, updated_at={NOW_SQL}
            WHERE id=?""", (verdict, owner, media_id))


# ---------- деньги ----------

def log_llm_usage(db, purpose: str, model: str, city: str | None, cluster_id: int | None,
                  input_tokens: int, output_tokens: int, cache_read: int, cache_write: int,
                  cost_usd: float) -> None:
    db.conn.execute(
        f"""INSERT INTO llm_usage (created_at, purpose, model, city, cluster_id,
                                   input_tokens, output_tokens, cache_read, cache_write, cost_usd)
            VALUES ({NOW_SQL},?,?,?,?,?,?,?,?,?)""",
        (purpose, model, city, cluster_id, input_tokens, output_tokens,
         cache_read, cache_write, cost_usd))


def usage_total(db, since: str | None = None) -> dict:
    where, params = ("WHERE created_at >= ?", [since]) if since else ("", [])
    row = db.conn.execute(
        f"""SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) input_tokens,
                   COALESCE(SUM(output_tokens),0) output_tokens,
                   COALESCE(SUM(cache_read),0) cache_read,
                   COALESCE(SUM(cache_write),0) cache_write,
                   COALESCE(SUM(cost_usd),0) cost_usd
            FROM llm_usage {where}""", params).fetchone()
    return dict(row)


def usage_by_purpose(db, since: str | None = None) -> list[dict]:
    where, params = ("WHERE created_at >= ?", [since]) if since else ("", [])
    return [dict(r) for r in db.conn.execute(
        f"""SELECT purpose, COUNT(*) calls, COALESCE(SUM(cost_usd),0) cost_usd,
                   COALESCE(SUM(input_tokens),0) input_tokens,
                   COALESCE(SUM(cache_read),0) cache_read,
                   COALESCE(SUM(output_tokens),0) output_tokens
            FROM llm_usage {where} GROUP BY purpose ORDER BY cost_usd DESC""", params)]
