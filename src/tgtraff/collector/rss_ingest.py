"""Запись RSS-ленты → общий формат конвейера (чистые функции, без сети).

Формат тот же, что у Telegram и ВК (collector/ingest.py). Особенности лент:
  * тело приходит куском HTML — теги и сущности вычищаем, иначе они уедут в промпт
    rewriter'а и в текст поста;
  * заголовок несёт главную фактуру, поэтому он идёт в текст первой строкой;
  * даты у лент разношёрстные (pubDate/updated/dc:date), а иногда их нет вовсе;
  * картинка ленты приходит ссылкой в enclosure/media:content — качаем её сами
    (перезалив своим файлом обязателен, CLAUDE.md).
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from tgtraff.collector.ingest import MediaRef, NormalizedPost, mentions_city
from tgtraff.config import SourceSpec

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_BREAK = re.compile(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def strip_html(raw: str | None) -> str:
    """HTML описания → плоский текст. Переносы сохраняем: абзацы новости — это факты,
    склеенные в одну строку они читаются хуже и хуже режутся на фразы."""
    if not raw:
        return ""
    text = _SCRIPT_OR_STYLE.sub("", raw)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def _entry_datetime(entry: dict) -> str | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            # feedparser отдаёт struct_time, уже приведённый к UTC.
            return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
    return None


# Меньше этого по стороне — логотип, иконка или счётчик, а не фотография новости.
MIN_IMAGE_SIDE = 200


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _is_meaningful(width, height) -> bool:
    """Размеры лента указывает не всегда: без них считаем картинку годной,
    а вот явно маленькую отбрасываем."""
    w, h = _int(width), _int(height)
    if w is None and h is None:
        return True
    return max(w or 0, h or 0) >= MIN_IMAGE_SIDE


def _entry_media(entry: dict) -> list[MediaRef]:
    """Одна содержательная картинка записи (решение ревью §17).

    Ленты городских СМИ отдают по 4-6 изображений на новость, включая оригиналы
    на несколько мегабайт; посту нужна ровно одна. Порядок предпочтений:
    enclosure и media:content идут первыми, media:thumbnail — только если больше
    ничего нет (это заведомо превью).
    """
    main: list[str] = []
    thumbs: list[str] = []

    for enclosure in entry.get("enclosures") or []:
        mime = (enclosure.get("type") or "").lower()
        url = enclosure.get("href") or enclosure.get("url")
        if url and (mime.startswith("image/") or url.lower().endswith(_IMAGE_EXT)):
            if _is_meaningful(enclosure.get("width"), enclosure.get("height")):
                main.append(url)

    for block in entry.get("media_content") or []:
        url = block.get("url")
        medium = (block.get("medium") or block.get("type") or "").lower()
        if url and (medium.startswith("image") or url.lower().endswith(_IMAGE_EXT)):
            if _is_meaningful(block.get("width"), block.get("height")):
                main.append(url)

    for block in entry.get("media_thumbnail") or []:
        url = block.get("url")
        if url and _is_meaningful(block.get("width"), block.get("height")):
            thumbs.append(url)

    chosen = next(iter(main), None) or next(iter(thumbs), None)
    return [MediaRef(ord=0, kind="photo", url=chosen)] if chosen else []


def _entry_text(entry: dict) -> str:
    title = strip_html(entry.get("title"))
    body = strip_html(entry.get("summary") or entry.get("description"))
    if not body:
        contents = entry.get("content") or []
        body = strip_html(contents[0].get("value")) if contents else ""
    if body and body.startswith(title):        # часть лент дублирует заголовок в теле
        return body
    return f"{title}\n\n{body}".strip() if body else title


def normalize_rss_entry(entry: dict, source: SourceSpec, fetched_at: str | None = None) -> NormalizedPost:
    text = _entry_text(entry)
    link = entry.get("link") or ""
    picked = _entry_media(entry)

    filtered_out = bool(source.city_filter) and not mentions_city(text, source.city_filter)
    media = [] if filtered_out else picked

    published = _entry_datetime(entry) or fetched_at or datetime.now(timezone.utc).isoformat()

    return NormalizedPost(
        external_id=entry.get("id") or entry.get("guid") or link,
        text=text,
        published_at=published,
        url=link or None,
        filtered_out=filtered_out,
        filter_reason=f"city_filter:{source.city_filter}" if filtered_out else None,
        media=media,
        had_media=bool(picked),          # у записи была годная картинка (даже если не качаем)
        extra={"author": entry.get("author")} if entry.get("author") else {},
    )
