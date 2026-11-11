"""Формат-шаблоны постов (спека §8; подпись — assets/brand/PINS.md §3).

Один шаблон на сеть, категория и город — параметры (требование CLAUDE.md
о мультиканальности):

    🚗 <b>Хук одной строкой</b>

    Тело поста в стиле канала.

    #Индустриальный · ЧП Череповец — прислать новость

Разметка — HTML Bot API. Всё, что пришло из источника или от читателя,
экранируется: чужой текст не имеет права ломать вёрстку нашего поста.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

# Эмодзи-префикс категории. Свой у каждой из семи категорий rewriter'а — в ленте
# читатель узнаёт тип новости раньше, чем читает хук.
CATEGORY_EMOJI = {
    "dtp": "🚗", "fire": "🔥", "crime": "🚔", "utility": "🚰",
    "weather": "🌤", "city": "🏙", "other": "❗",
}
FLASH_EMOJI = "⚡️"
DEFAULT_EMOJI = "❗"

# Подпись к медиа в Bot API — 1024 знака, обычное сообщение — 4096.
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

# Слова, которые в хэштег района не идут: «Индустриальный район» → #Индустриальный.
_DISTRICT_NOISE = ("микрорайон", "мкрн", "мкр", "район", "р-н", "посёлок", "поселок")


@dataclass(frozen=True)
class PostContent:
    """Содержимое поста — то, что публикуется, без деталей доставки."""

    city: str
    title: str
    body: str
    category: str = "other"
    urgency: str = "normal"
    district: str | None = None
    media_path: str | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)
    # Тело уже размечено НАМИ (сервисный автопост: ссылки топ-5). Всё остальное —
    # текст источника или читателя — экранируется всегда: чужая разметка не имеет
    # права ломать вёрстку нашего поста.
    body_html: bool = False


@dataclass(frozen=True)
class Button:
    text: str
    url: str


@dataclass(frozen=True)
class RenderedPost:
    text: str
    button: Button | None = None


@dataclass(frozen=True)
class DeliveryPlan:
    """Как класть пост в канал: подписью к медиа или отдельным сообщением."""

    mode: str            # text | photo_caption | photo_then_text
    kind: str | None = None   # photo | video


def district_hashtag(district: str | None) -> str | None:
    if not district:
        return None
    words = [w for w in re.split(r"[\s,]+", district.strip()) if w]
    kept = [w for w in words if w.lower().strip(".") not in _DISTRICT_NOISE]
    tag = "".join(re.sub(r"[^\w-]", "", w, flags=re.U).capitalize()
                  for w in (kept or words))
    tag = tag.replace("-", "")
    return f"#{tag}" if tag else None


def ugc_link(ugc_bot: str, deep_link: str) -> str:
    return f"https://t.me/{ugc_bot}?start={deep_link}"


def render_post(post: PostContent, city, ugc_bot: str) -> RenderedPost:
    emoji = CATEGORY_EMOJI.get(post.category, DEFAULT_EMOJI)
    if post.urgency == "flash":
        emoji = FLASH_EMOJI + emoji
    parts = [f"{emoji} <b>{html.escape(post.title.strip())}</b>"]
    if post.body and post.body.strip():
        body = post.body.strip()
        parts.append(body if post.body_html else html.escape(body))

    footer = []
    tag = district_hashtag(post.district)
    if tag:
        footer.append(html.escape(tag))
    if city.signature:
        name = html.escape(city.tg.title or city.city)
        link = city.tg.link
        footer.append(f'<a href="{link}">{name}</a>' if link else name)
        footer.append(f'<a href="{ugc_link(ugc_bot, city.deep_link)}">прислать новость</a>')
    if footer:
        # Хэштег и канал разделяет «·», подпись от кнопки — «—» (PINS.md §3).
        line = " · ".join(footer[:-1]) + " — " + footer[-1] if city.signature else footer[0]
        parts.append(line)

    button = None
    if city.ugc_button:
        button = Button("📸 Прислать новость", ugc_link(ugc_bot, city.deep_link))
    return RenderedPost("\n\n".join(parts)[:TEXT_LIMIT], button)


def render_plain(post: PostContent, city, ugc_bot: str) -> str:
    """Тот же пост для площадок без разметки (ВК). Структура одна и та же —
    читатель, пришедший из ВК в Telegram, узнаёт ленту."""
    emoji = CATEGORY_EMOJI.get(post.category, DEFAULT_EMOJI)
    if post.urgency == "flash":
        emoji = FLASH_EMOJI + emoji
    parts = [f"{emoji} {post.title.strip()}"]
    if post.body and post.body.strip():
        parts.append(strip_html(post.body.strip()) if post.body_html else post.body.strip())

    footer = []
    tag = district_hashtag(post.district)
    if tag:
        footer.append(tag)
    if city.signature:
        footer.append(city.tg.title or city.city)
        if city.tg.link:
            footer.append(f"быстрее всего в Telegram: {city.tg.link}")
    if footer:
        parts.append(" · ".join(footer))
    return "\n\n".join(parts)


# У ВК разметки нет: наши же ссылки в теле сервисного поста разворачиваются
# в чистый текст.
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text))


def media_kind(media_path: str | None) -> str | None:
    if not media_path:
        return None
    suffix = Path(media_path).suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in PHOTO_SUFFIXES:
        return "photo"
    return "document"


def plan_delivery(text: str, media_path: str | None) -> DeliveryPlan:
    """Длинный пост не режем: подпись к медиа ограничена 1024 знаками, поэтому
    такой пост уходит двумя сообщениями — медиа, следом текст с кнопкой."""
    kind = media_kind(media_path)
    if kind is None:
        return DeliveryPlan("text")
    if len(text) <= CAPTION_LIMIT:
        return DeliveryPlan("photo_caption", kind)
    return DeliveryPlan("photo_then_text", kind)
