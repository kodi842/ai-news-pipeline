"""Предпросмотр фазы 5: карточка модератора и пост канала по живым драфтам.

Нужен ревью и калибровке формата: и то и другое проще проверить глазами.
Ни одного обращения к сети — только база и шаблоны.
"""
from __future__ import annotations

from tgtraff.moderation import actions
from tgtraff.moderation.card import card_text
from tgtraff.publisher.format import render_post
from tgtraff.publisher.runner import content_of


def render_preview(db, channels, city_names: dict[str, str], limit: int = 5) -> str:
    rows = actions.pending(db, limit=limit)
    out = ["# Предпросмотр фазы 5: карточки и посты", ""]
    if not rows:
        out.append("Драфтов в очереди модерации нет.")
        return "\n".join(out)

    mode = "боевой" if channels.is_live else "тестовый"
    out += [f"Режим публикации: **{mode}**. Драфтов в очереди: {len(rows)}.", ""]
    for row in rows:
        city = channels.city(row["city"])
        out += [f"## Драфт #{row['id']} · {row['city']} · {row['category']}", "",
                "**Карточка модератора**", "", "```html",
                card_text(row, city_name=city_names.get(row["city"])), "```", ""]
        if city is None or not city.tg.chat_id:
            out += [f"**Пост в канале** — канал не настроен для города {row['city']}.", ""]
            continue
        post = render_post(content_of(row), city, channels.ugc_bot)
        out += ["**Пост в канале**", "", "```html", post.text, "```"]
        if post.button:
            out.append(f"Кнопка: `{post.button.text}` → {post.button.url}")
        media = row["media_path"] or "без медиа"
        out += [f"Медиа: `{media}` ({row['media_kind'] or '—'})", ""]
    return "\n".join(out)
