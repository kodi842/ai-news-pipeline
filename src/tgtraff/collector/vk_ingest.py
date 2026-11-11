"""Пост ВК → общий формат конвейера (чистые функции, без сети).

Формат тот же, что у Telegram (collector/ingest.py): различия платформ кончаются
здесь. Особенности ВК, которые приходится учитывать:
  * альбом приходит ОДНИМ постом с массивом вложений — склейка, в отличие от ТГ,
    не нужна (QUESTIONS §4);
  * репост держит фактуру в `copy_history`, свой текст при этом часто пуст;
  * видео сервисным ключом не скачать — фиксируем факт видео без ссылки;
  * `marked_as_ads` — готовый признак для relevance-фильтра фазы 4.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tgtraff.collector.ingest import MediaRef, NormalizedPost, mentions_city
from tgtraff.config import SourceSpec

# Расширения документов, которые для нас картинка, а не файл.
_IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp"}


def _biggest_photo_url(photo: dict) -> str | None:
    sizes = photo.get("sizes") or []
    if not sizes:
        return photo.get("url")
    return max(sizes, key=lambda s: (s.get("width") or 0, s.get("height") or 0)).get("url")


def _attachment_media(att: dict, ord: int) -> MediaRef | None:
    kind = att.get("type")
    if kind == "photo":
        return MediaRef(ord=ord, kind="photo", url=_biggest_photo_url(att.get("photo") or {}))
    if kind == "video":
        # Файл видео отдаётся только пользовательскому токену; сервисным — никак.
        return MediaRef(ord=ord, kind="video", url=None)
    if kind == "doc":
        d = att.get("doc") or {}
        ext = (d.get("ext") or "").lower()
        return MediaRef(ord=ord, kind="photo" if ext in _IMAGE_EXT else "document",
                        filename=d.get("title"), url=d.get("url"))
    return None                      # link/poll/audio/market — не медиа поста


def _collect_media(item: dict, start: int = 0) -> list[MediaRef]:
    media: list[MediaRef] = []
    for att in item.get("attachments") or []:
        ref = _attachment_media(att, start + len(media))
        if ref is not None:
            media.append(ref)
    return media


def normalize_vk_post(item: dict, source: SourceSpec) -> NormalizedPost:
    owner_id = item.get("owner_id", item.get("from_id"))
    post_id = item["id"]

    text = (item.get("text") or "").strip()
    media = _collect_media(item)

    # Репост: своя подпись + оригинал. Ни то ни другое выбрасывать нельзя —
    # rewriter работает только с той фактурой, что мы принесли (спека §5).
    fwd_from = None
    for original in item.get("copy_history") or []:
        fwd_from = str(original.get("owner_id") or original.get("from_id") or "unknown")
        original_text = (original.get("text") or "").strip()
        text = f"{text}\n\n{original_text}".strip() if text else original_text
        media.extend(_collect_media(original, start=len(media)))

    filtered_out = bool(source.city_filter) and not mentions_city(text, source.city_filter)
    if filtered_out:
        media = []                   # чужой город — медиа не качаем (как у областных ТГ)

    views = (item.get("views") or {}).get("count")
    return NormalizedPost(
        external_id=f"{owner_id}_{post_id}",
        text=text,
        published_at=datetime.fromtimestamp(item["date"], tz=timezone.utc).isoformat(),
        url=f"https://vk.com/wall{owner_id}_{post_id}",
        views=views,
        fwd_from=fwd_from,
        filtered_out=filtered_out,
        filter_reason=f"city_filter:{source.city_filter}" if filtered_out else None,
        media=media,
        had_media=bool(item.get("attachments")),
        extra={
            "marked_as_ads": bool(item.get("marked_as_ads")),
            "is_pinned": bool(item.get("is_pinned")),
            "likes": (item.get("likes") or {}).get("count"),
        },
    )
