"""Приведение сообщения Telegram к записи raw_posts (чистые функции, без сети).

Единый формат (спека, «Normalizer»): {source_id, source_type, text, media[],
published_at, url, views}. Здесь — TG-часть и общие для всех источников типы
`MediaRef`/`NormalizedPost`; ВК и RSS кладут в них своё в vk_ingest.py и rss_ingest.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from telethon.tl.types import (
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from tgtraff.config import SourceSpec


@dataclass(frozen=True)
class MediaRef:
    ord: int
    kind: str
    filename: str | None = None
    # Прямая ссылка на файл: её дают ВК и RSS. У Telegram ссылки нет — медиа
    # тянется Telethon по самому сообщению.
    url: str | None = None


@dataclass(frozen=True)
class NormalizedPost:
    external_id: str
    text: str
    published_at: str
    url: str | None = None
    grouped_id: str | None = None
    edited_at: str | None = None
    views: int | None = None
    fwd_from: str | None = None
    filtered_out: bool = False
    filter_reason: str | None = None
    media: list[MediaRef] = field(default_factory=list)
    # Поля источника, которые пригодятся дальше по конвейеру (напр. пометка рекламы
    # у ВК — её разбирает relevance-фильтр фазы 4). Уезжают в raw_posts.raw_json.
    extra: dict = field(default_factory=dict)

    @property
    def has_media(self) -> bool:
        return bool(self.media)

    # Медиа в сообщении было, но качать не будем (пост отфильтрован по городу).
    had_media: bool = False

    @property
    def media_state(self) -> str:
        if self.media:
            return "pending"
        return "skipped" if self.filtered_out and self.had_media else "none"

    @property
    def is_empty(self) -> bool:
        """Ни текста, ни медиа — публиковать нечего (у ВК так выглядят
        служебные записи и посты, состоящие из одной ссылки)."""
        return not self.text.strip() and not self.media


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def media_kind(message) -> str | None:
    """Тип медиа сообщения. Превью ссылок, опросы, гео — не медиа."""
    media = getattr(message, "media", None)
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        mime = (getattr(media.document, "mime_type", "") or "") if media.document else ""
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("image/"):
            return "photo"
        return "document"
    return None


def _media_filename(message) -> str | None:
    media = getattr(message, "media", None)
    doc = getattr(media, "document", None) if media else None
    for attr in getattr(doc, "attributes", None) or []:
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return None


def mentions_city(text: str | None, city_filter: str) -> bool:
    """Областной источник берём, только если город упомянут (спека: «парсить с фильтром»).
    Подстрока ловит и склонения: «в магнитогорскЕ», «магнитогорскА»."""
    return bool(text) and city_filter.lower() in text.lower()


def _fwd_from(message) -> str | None:
    fwd = getattr(message, "fwd_from", None)
    if fwd is None:
        return None
    return getattr(fwd, "from_name", None) or str(getattr(fwd, "from_id", "") or "") or "unknown"


def normalize_message(message, source: SourceSpec) -> NormalizedPost:
    text = getattr(message, "message", None) or ""
    kind = media_kind(message)

    filtered_out = False
    filter_reason = None
    if source.city_filter and not mentions_city(text, source.city_filter):
        filtered_out = True
        filter_reason = f"city_filter:{source.city_filter}"

    # Отфильтрованное храним текстом (пригодится для калибровки фаз 3-4), но медиа не качаем.
    media = []
    if kind and not filtered_out:
        media = [MediaRef(ord=0, kind=kind, filename=_media_filename(message))]

    url = f"https://t.me/{source.key}/{message.id}" if source.key else None
    grouped = getattr(message, "grouped_id", None)

    return NormalizedPost(
        external_id=str(message.id),
        text=text,
        published_at=_iso(message.date),
        url=url,
        grouped_id=str(grouped) if grouped else None,
        edited_at=_iso(getattr(message, "edit_date", None)),
        views=getattr(message, "views", None),
        fwd_from=_fwd_from(message),
        filtered_out=filtered_out,
        filter_reason=filter_reason,
        media=media,
        had_media=bool(kind),
    )
