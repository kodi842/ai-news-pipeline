"""Материал кластера: всё, из чего rewriter имеет право писать пост.

Спека §5: «Вход: весь кластер (все версии текста + медиа)», «Запрещено выдумывать
факты, отсутствующие в кластере; при недостатке фактуры — пометка драфта
`low_facts`». Значит, у фазы 4 должен быть один объект, который честно отвечает
на вопрос «какая фактура у нас есть» — и он же идёт в промпт.

Плейсхолдеры фактурой не считаются (решение ревью 2026-09-03): «В посте есть
вложения:» — это не текст новости, а заглушка Telegram-экспорта. Пост без текста
остаётся в материале ради медиа и ради самого факта «источник тоже это снял».
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from tgtraff.dedup.text import strip_boilerplate

# Заглушки вместо текста: встречались на живой ленте (docs/reports/dedup-sample).
_PLACEHOLDERS = (
    "в посте есть вложения",
    "без текста",
    "только медиа",
    "фото", "видео", "фотo", "фоторепортаж",
    "смотрите видео", "видео с места", "на видео",
)
# Ниже этого объёма очищенный текст новостью не является: «Авария на Ленина» —
# повод посмотреть, но не повод написать пост без домысла.
MIN_FACT_CHARS = 80

PLATFORM_LABELS = {
    "tg": "Telegram", "tg_regional": "Telegram (областной)",
    "vk": "ВКонтакте", "rss": "сайт СМИ", "official": "ведомство",
}


def is_placeholder(text: str | None) -> bool:
    """Текста нет или он не несёт фактуры."""
    body = strip_boilerplate(text)
    body = re.sub(r"[^\w\s]", " ", body, flags=re.U).strip().lower()
    body = re.sub(r"\s+", " ", body)
    if not body:
        return True
    return body in _PLACEHOLDERS or (len(body) <= 24 and any(
        body.startswith(p) for p in _PLACEHOLDERS))


@dataclass(frozen=True)
class MediaRef:
    """Один файл кластера — кандидат в иллюстрацию (медиа-каскад, спека §5)."""

    media_id: int
    raw_post_id: int
    ord: int
    kind: str
    file_path: str | None
    source_key: str
    official: bool
    watermark: str | None = None          # none | foreign | own | unknown
    watermark_owner: str | None = None
    phash: str | None = None

    @property
    def is_ready(self) -> bool:
        return bool(self.file_path)


@dataclass(frozen=True)
class SourcePost:
    raw_post_id: int
    source_key: str
    source_type: str
    official: bool
    published_at: str
    url: str | None
    text: str                              # очищенный от футеров
    raw_text: str
    is_placeholder: bool
    media: list[MediaRef] = field(default_factory=list)
    # Площадка сама пометила запись рекламной (ВК API, собирается фазой 2) —
    # готовый бесплатный признак для relevance-фильтра.
    marked_as_ads: bool = False

    @property
    def platform(self) -> str:
        return PLATFORM_LABELS.get(self.source_type, self.source_type)


@dataclass(frozen=True)
class ClusterMaterial:
    cluster_id: int
    city: str
    city_name: str
    first_seen_at: str
    posts: list[SourcePost]
    # Порог «фактуры мало» — параметр конфига (rewriter.min_fact_chars):
    # на калибровке его двигают, а не правят код.
    min_fact_chars: int = MIN_FACT_CHARS

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for p in self.posts:
            if p.source_key not in seen:
                seen.append(p.source_key)
        return seen

    @property
    def platforms(self) -> list[str]:
        seen: list[str] = []
        for p in self.posts:
            if p.source_type not in seen:
                seen.append(p.source_type)
        return seen

    @property
    def media(self) -> list[MediaRef]:
        return [m for p in self.posts for m in p.media]

    @property
    def fact_texts(self) -> list[str]:
        """Тексты, которые действительно несут фактуру (без плейсхолдеров)."""
        return [p.text for p in self.posts if not p.is_placeholder and p.text]

    @property
    def fact_chars(self) -> int:
        return max((len(t) for t in self.fact_texts), default=0)

    @property
    def is_low_facts(self) -> bool:
        """Фактуры мало — драфт помечается `low_facts` и идёт только в ручную."""
        return self.fact_chars < self.min_fact_chars

    @property
    def has_official(self) -> bool:
        return any(p.official for p in self.posts)

    def as_prompt(self) -> str:
        """Материал в виде текста для модели. Ничего, кроме того, что есть в базе."""
        head = [f"Город: {self.city_name}",
                f"Источников: {len(self.sources)}, публикаций: {len(self.posts)}", ""]
        for i, p in enumerate(self.posts, 1):
            tag = "официальный источник" if p.official else p.platform
            when = p.published_at[:16].replace("T", " ")
            head.append(f"[{i}] {p.source_key} ({tag}), {when} UTC")
            if p.is_placeholder:
                media = f", медиа: {len(p.media)}" if p.media else ""
                head.append(f"    (публикация без текста{media})")
            else:
                head.append("    " + p.text.replace("\n", "\n    "))
            head.append("")
        return "\n".join(head).strip()


def collect_material(db, cluster_id: int, city_name: str | None = None,
                     min_fact_chars: int = MIN_FACT_CHARS) -> ClusterMaterial:
    """Собирает кластер из базы: посты в хронологическом порядке и их медиа."""
    head = db.conn.execute(
        "SELECT id, city, first_seen_at FROM clusters WHERE id=?", (cluster_id,)).fetchone()
    if head is None:
        raise LookupError(f"кластера #{cluster_id} нет в базе")

    rows = db.conn.execute(
        """SELECT p.id AS raw_post_id, p.source_type, p.text, p.url, p.published_at,
                  p.raw_json, s.key AS source_key, s.official
           FROM cluster_members m
           JOIN raw_posts p ON p.id = m.raw_post_id
           JOIN sources s ON s.id = p.source_id
           WHERE m.cluster_id = ?
           ORDER BY p.published_at, p.id""", (cluster_id,)).fetchall()

    posts: list[SourcePost] = []
    for r in rows:
        official = bool(r["official"]) or r["source_type"] == "official"
        media = _post_media(db, r["raw_post_id"], r["source_key"], official)
        text = strip_boilerplate(r["text"])
        posts.append(SourcePost(
            raw_post_id=r["raw_post_id"], source_key=r["source_key"],
            source_type=r["source_type"], official=official,
            published_at=r["published_at"], url=r["url"],
            text=text, raw_text=r["text"] or "",
            is_placeholder=is_placeholder(r["text"]), media=media,
            marked_as_ads=_marked_as_ads(r["raw_json"]),
        ))
    return ClusterMaterial(
        cluster_id=cluster_id, city=head["city"], city_name=city_name or head["city"],
        first_seen_at=head["first_seen_at"], posts=posts, min_fact_chars=min_fact_chars,
    )


def _marked_as_ads(raw_json: str | None) -> bool:
    if not raw_json:
        return False
    try:
        return bool(json.loads(raw_json).get("marked_as_ads"))
    except (TypeError, ValueError, AttributeError):
        return False


def _post_media(db, raw_post_id: int, source_key: str, official: bool) -> list[MediaRef]:
    """Медиа поста и остальных сообщений его альбома (вид post_heads, фаза 3)."""
    rows = db.conn.execute(
        """SELECT m.id, m.raw_post_id, m.ord, m.kind, m.file_path, m.phash,
                  m.watermark, m.watermark_owner
           FROM raw_media m
           JOIN post_heads ph ON ph.raw_post_id = m.raw_post_id
           WHERE ph.head_id = ? AND m.status='done'
           ORDER BY m.raw_post_id, m.ord""", (raw_post_id,)).fetchall()
    return [
        MediaRef(media_id=r["id"], raw_post_id=r["raw_post_id"], ord=r["ord"],
                 kind=r["kind"], file_path=r["file_path"], source_key=source_key,
                 official=official, watermark=r["watermark"],
                 watermark_owner=r["watermark_owner"], phash=r["phash"])
        for r in rows
    ]
