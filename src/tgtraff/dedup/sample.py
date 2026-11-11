"""Выборка кластеров для ручной проверки (приёмка фазы 3, спека §3).

Логика отдельно от скрипта: тем же кодом пользуются и текстовый отчёт
(`scripts/dedup_sample.py`), и HTML-страница проверки (`scripts/dedup_report.py`).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

REASON_LABELS = {
    "new": "первый пост кластера",
    "exact_text": "точный хэш текста",
    "phash": "та же картинка",
    "semantic": "смысловая близость",
}


def day_bounds(day: str) -> tuple[str, str]:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S+00:00"
    return start.strftime(fmt), (start + timedelta(days=1)).strftime(fmt)


def collect(db, day: str, limit: int, seed: int, only_merged: bool,
            city: str | None = None) -> dict:
    since, until = day_bounds(day)
    where = ["cl.first_seen_at >= ?", "cl.first_seen_at < ?"]
    params: list = [since, until]
    if city:
        where.append("cl.city = ?")
        params.append(city)
    if only_merged:
        where.append("cl.size > 1")

    rows = db.conn.execute(
        f"SELECT id, city, size FROM clusters cl WHERE {' AND '.join(where)} ORDER BY id",
        params,
    ).fetchall()

    population = [dict(r) for r in rows]
    rnd = random.Random(seed)
    picked = population if len(population) <= limit else rnd.sample(population, limit)
    picked.sort(key=lambda c: c["id"])

    clusters = [cluster_card(db, c["id"]) for c in picked]
    return {
        "day": day,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "seed": seed,
        "city": city or "все города пилота",
        "total_clusters_in_day": len(population),
        "merged_clusters_in_day": sum(1 for c in population if c["size"] > 1),
        "sample_size": len(clusters),
        "sample_merged": sum(1 for c in clusters if c["size"] > 1),
        "clusters": clusters,
    }


def cluster_card(db, cluster_id: int) -> dict:
    head = db.conn.execute(
        "SELECT id, city, size, first_seen_at, last_seen_at FROM clusters WHERE id=?",
        (cluster_id,)).fetchone()
    posts = db.conn.execute(
        """SELECT m.reason, m.similarity, m.matched_raw_post_id, p.id AS raw_post_id,
                  p.published_at, p.url, p.text, p.source_type, s.key AS source_key,
                  (SELECT COUNT(*) FROM raw_media rm
                    WHERE rm.raw_post_id = p.id AND rm.status='done') AS media
           FROM cluster_members m
           JOIN raw_posts p ON p.id = m.raw_post_id
           JOIN sources s ON s.id = p.source_id
           WHERE m.cluster_id = ? ORDER BY p.published_at, p.id""",
        (cluster_id,)).fetchall()
    return {
        "id": head["id"],
        "city": head["city"],
        "size": head["size"],
        "first_seen_at": head["first_seen_at"],
        "sources": sorted({p["source_key"] for p in posts}),
        "platforms": sorted({p["source_type"] for p in posts}),
        "posts": [
            {
                "raw_post_id": p["raw_post_id"],
                "source": p["source_key"],
                "platform": p["source_type"],
                "published_at": p["published_at"],
                "url": p["url"],
                "media": p["media"],
                "reason": p["reason"],
                "reason_label": REASON_LABELS.get(p["reason"], p["reason"] or ""),
                "similarity": round(p["similarity"], 3) if p["similarity"] is not None else None,
                "text": " ".join((p["text"] or "").split()),
            }
            for p in posts
        ],
    }


def render_markdown(data: dict) -> str:
    out = [
        f"# Проверочная выборка кластеров — {data['day']}",
        "",
        f"Города: {data['city']}. Сформировано {data['generated_at']}, seed={data['seed']}.",
        "",
        f"За сутки кластеров: **{data['total_clusters_in_day']}**, "
        f"из них со склейкой (2+ поста): **{data['merged_clusters_in_day']}**.",
        f"В выборке: **{data['sample_size']}** кластеров, со склейкой — **{data['sample_merged']}**.",
        "",
        "Как проверять: кластер верный, если ВСЕ посты в нём — об одном и том же событии.",
        "Кластер из одного поста ошибкой быть не может — его достаточно пролистать.",
        "",
        "---",
        "",
    ]
    for c in data["clusters"]:
        flag = "🔗" if c["size"] > 1 else "·"
        out.append(f"## {flag} Кластер #{c['id']} — {c['city']}, постов: {c['size']}")
        out.append("")
        out.append(f"Источники: {', '.join(c['sources'])}  ·  площадки: {', '.join(c['platforms'])}")
        out.append("")
        for p in c["posts"]:
            sim = f", cosine {p['similarity']}" if p["similarity"] is not None else ""
            out.append(f"**{p['source']}** ({p['platform']}), {p['published_at'][:16].replace('T', ' ')} — "
                       f"_{p['reason_label']}{sim}_")
            out.append("")
            out.append(f"> {p['text'][:1200] or '(без текста, только медиа)'}")
            out.append("")
        out.append("---")
        out.append("")
    return "\n".join(out)


