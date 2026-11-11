"""Отчёт по драфтам для ручной разметки (итерации приёмки фазы 4).

Формат один и тот же на каждой итерации: кластер → драфт → источники. Оператор
читает драфт, тут же видит всю фактуру, из которой он собран, и ставит оценку
в строку «Оценка». По его пометкам правятся промпты — и следующая итерация
гоняется на такой же странице.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tgtraff import drafts as store

CATEGORY_LABELS = {
    "dtp": "ДТП", "fire": "ПОЖАР", "crime": "КРИМИНАЛ", "utility": "КОММУНАЛКА",
    "weather": "ПОГОДА", "city": "ГОРОД", "other": "ПРОЧЕЕ",
}
MEDIA_LABELS = {
    "photo": "фото", "video": "видео", "animation": "гиф",
    "template": "шаблонная обложка", "none": "без медиа",
}
MEDIA_REASONS = {
    "official": "официальный источник", "ugc": "UGC",
    "clean": "без чужого вотермарка", "unverified": "не проверено (видео)",
    "watermarked": "всё медиа с чужим знаком", "no_media": "медиа в кластере нет",
}
PLATFORMS = {"tg": "ТГ", "tg_regional": "ТГ-обл", "vk": "ВК", "rss": "RSS",
             "official": "ведомство"}


def collect_report(db, city: str | None = None, limit: int = 30,
                   since: str | None = None, draft_ids: list[int] | None = None) -> dict:
    where, params = ["1=1"], []
    if city:
        where.append("d.city = ?")
        params.append(city)
    if since:
        where.append("d.created_at >= ?")
        params.append(since)
    if draft_ids:
        where.append(f"d.id IN ({','.join('?' * len(draft_ids))})")
        params.extend(draft_ids)

    rows = db.conn.execute(
        f"""SELECT * FROM drafts d WHERE {' AND '.join(where)}
            ORDER BY d.id DESC LIMIT ?""", (*params, limit)).fetchall()

    drafts = [_card(db, r) for r in rows]
    drafts.reverse()                    # в файле — по возрастанию, читать сверху вниз

    by_safety: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for card in drafts:
        by_safety[card["safety"]] = by_safety.get(card["safety"], 0) + 1
        by_category[card["category"]] = by_category.get(card["category"], 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "city": city or "все города",
        "drafts": drafts,
        "by_safety": by_safety,
        "by_category": by_category,
        "total_cost_usd": store.usage_total(db, since=since)["cost_usd"],
        "usage": store.usage_by_purpose(db, since=since),
    }


def _card(db, row) -> dict:
    sources = db.conn.execute(
        """SELECT p.id, p.published_at, p.url, p.text, p.source_type,
                  s.key AS source_key, s.official,
                  (SELECT COUNT(*) FROM raw_media m
                    WHERE m.raw_post_id = p.id AND m.status='done') AS media
           FROM cluster_members cm
           JOIN raw_posts p ON p.id = cm.raw_post_id
           JOIN sources s ON s.id = p.source_id
           WHERE cm.cluster_id = ?
           ORDER BY p.published_at, p.id""", (row["cluster_id"],)).fetchall() \
        if row["cluster_id"] else []

    return {
        "draft_id": row["id"],
        "cluster_id": row["cluster_id"],
        "city": row["city"],
        "title": row["title"],
        "body": row["body"] or "",
        "category": row["category"] or "other",
        "urgency": row["urgency"] or "normal",
        "safety": row["safety_verdict"] or "—",
        "safety_reasons": store.decode_list(row["safety_reasons"]),
        "flags": store.decode_list(row["flags"]),
        "relevance": row["relevance"],
        "media_kind": row["media_kind"],
        "media_reason": row["media_reason"],
        "media_path": row["media_path"],
        "notes": row["notes"] or "",
        "cost_usd": row["cost_usd"] or 0.0,
        "sources": [
            {
                "source": s["source_key"],
                "platform": PLATFORMS.get(s["source_type"], s["source_type"]),
                "official": bool(s["official"]),
                "published_at": s["published_at"][:16].replace("T", " "),
                "url": s["url"],
                "media": s["media"],
                "text": " ".join((s["text"] or "").split()),
            }
            for s in sources
        ],
    }


def render_markdown(data: dict) -> str:
    out = [
        f"# Драфты на разметку — {data['city']}",
        "",
        f"Сформировано {data['generated_at']}. Драфтов: **{len(data['drafts'])}**.",
        "",
        "Как размечать: в строке «Оценка» поставьте `+` (публикабельно без правок), "
        "`~` (нужна правка) или `−` (плохо), и через тире — что именно не так. "
        "Особое внимание — фактам: всё, что есть в драфте, должно быть в источниках ниже.",
        "",
    ]
    if data["by_safety"]:
        verdicts = ", ".join(f"{k}: {v}" for k, v in sorted(data["by_safety"].items()))
        out.append(f"Safety: {verdicts}.")
    if data["by_category"]:
        cats = ", ".join(f"{CATEGORY_LABELS.get(k, k)}: {v}"
                         for k, v in sorted(data["by_category"].items()))
        out.append(f"Категории: {cats}.")
    if data.get("usage"):
        spend = ", ".join(f"{u['purpose']} ${u['cost_usd']:.4f}" for u in data["usage"])
        out.append(f"Потрачено на модель: **${data['total_cost_usd']:.4f}** ({spend}).")
    out += ["", "---", ""]

    for card in data["drafts"]:
        out += _render_card(card)
    return "\n".join(out)


def _render_card(card: dict) -> list[str]:
    flags = ", ".join(card["flags"]) or "—"
    safety = card["safety"]
    if card["safety_reasons"]:
        safety += " (" + ", ".join(card["safety_reasons"]) + ")"
    media = MEDIA_LABELS.get(card["media_kind"], card["media_kind"] or "—")
    if card["media_reason"]:
        media += f" — {MEDIA_REASONS.get(card['media_reason'], card['media_reason'])}"

    out = [
        f"## Драфт #{card['draft_id']} · кластер #{card['cluster_id']} · {card['city']}",
        "",
        f"### {card['title']}",
        "",
        card["body"],
        "",
        f"`{CATEGORY_LABELS.get(card['category'], card['category'])}` · "
        f"срочность: {card['urgency']} · медиа: {media} · safety: {safety} · "
        f"флаги: {flags} · ${card['cost_usd']:.4f}",
        "",
    ]
    if card["notes"]:
        out += [f"Заметки конвейера: _{card['notes']}_", ""]
    out += ["**Оценка:** ", ""]

    out += [f"<details><summary>Источники кластера ({len(card['sources'])})</summary>", ""]
    for s in card["sources"]:
        tag = "официальный" if s["official"] else s["platform"]
        media = f", медиа: {s['media']}" if s["media"] else ""
        out.append(f"**{s['source']}** ({tag}), {s['published_at']}{media}")
        out.append("")
        out.append("> " + (s["text"][:1500] or "(без текста, только медиа)"))
        out.append("")
    out += ["</details>", "", "---", ""]
    return out
