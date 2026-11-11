"""Нормализатор: сырые строки `raw_posts` → логические посты конвейера.

Спека §2: единый формат `{source_id, source_type, text, media[], published_at, url, views}`
для всех источников — Telegram, ВК, RSS.

Главная работа здесь — склейка альбомов (QUESTIONS §4). Telegram присылает альбом как
N отдельных сообщений с общим `grouped_id`: подпись лежит на одном из них, остальные —
голые медиа. Если не склеить, dedup увидит N новостей вместо одной, а rewriter получит
пост без текста. У ВК альбом приходит одним постом с несколькими вложениями — там
склейка не нужна, но формат на выходе тот же.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Медиа, которое уже не появится: качать больше не будем, ждать нечего.
_TERMINAL_MEDIA = ("done", "failed", "skipped")


@dataclass(frozen=True)
class MediaItem:
    ord: int
    kind: str
    status: str
    file_path: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status == "done" and bool(self.file_path)


@dataclass(frozen=True)
class NormalizedItem:
    """Один логический пост: то, что дальше идёт в dedup и rewriter."""

    source_id: int
    source_type: str
    city: str
    source_key: str
    external_id: str
    text: str
    published_at: str
    url: str | None = None
    views: int | None = None
    grouped_id: str | None = None
    media: list[MediaItem] = field(default_factory=list)
    raw_post_ids: list[int] = field(default_factory=list)

    @property
    def head_id(self) -> int:
        """Головное сообщение поста — то, что несёт текст. Им логический пост
        и представлен в кластере: остальные сообщения альбома достаются по
        grouped_id и отдельными участниками не считаются (фаза 3)."""
        return self.raw_post_ids[0]

    @property
    def has_media(self) -> bool:
        return bool(self.media)

    @property
    def is_album(self) -> bool:
        """Склеен из нескольких сообщений источника (только Telegram)."""
        return len(self.raw_post_ids) > 1

    @property
    def media_ready(self) -> bool:
        """Все медиа отработаны очередью: скачаны или окончательно нет.
        Пост с недокачанным медиа лучше подождать — иначе rewriter выберет
        картинку из того, что успело лечь на диск."""
        return all(m.status in _TERMINAL_MEDIA for m in self.media)

    @property
    def media_missing(self) -> int:
        """Сколько медиа источника мы так и не получили (упало или пропущено)."""
        return sum(1 for m in self.media if m.status in ("failed", "skipped"))


def _sort_key(external_id: str):
    """Сообщения Telegram упорядочены message_id (число), у ВК/RSS id строковый."""
    return (0, int(external_id), "") if external_id.lstrip("-").isdigit() else (1, 0, external_id)


def _merge(rows: list[dict]) -> NormalizedItem:
    """Собирает один логический пост из строк одного альбома (или из одной строки)."""
    rows = sorted(rows, key=lambda r: _sort_key(r["external_id"]))
    head = rows[0]

    # Подпись альбома лежит на одном из сообщений; изредка дублируется обрезанной —
    # берём самую полную фактуру, выдумывать текст мы не имеем права (спека §5).
    text = max((r.get("text") or "" for r in rows), key=len)

    media: list[MediaItem] = []
    for r in rows:
        for m in sorted(r.get("media") or [], key=lambda m: m["ord"]):
            media.append(MediaItem(ord=len(media), kind=m["kind"], status=m["status"],
                                   file_path=m.get("file_path")))

    views = [r["views"] for r in rows if r.get("views") is not None]

    return NormalizedItem(
        source_id=head["source_id"],
        source_type=head["source_type"],
        city=head["city"],
        source_key=head["source_key"],
        external_id=head["external_id"],
        text=text,
        published_at=min(r["published_at"] for r in rows),
        url=head.get("url"),
        views=max(views) if views else None,
        grouped_id=head.get("grouped_id"),
        media=media,
        raw_post_ids=[r["raw_post_id"] for r in rows],
    )


def glue_albums(rows: list[dict]) -> list[NormalizedItem]:
    """Строки `raw_posts` (с прикреплённым списком медиа) → логические посты.

    Альбомом считаются строки одного источника с одинаковым `grouped_id`:
    сам по себе grouped_id уникален только внутри чата, поэтому ключ — пара
    (source_id, grouped_id), иначе новости разных городов слиплись бы в одну.
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for i, r in enumerate(rows):
        gid = r.get("grouped_id")
        key = (r["source_id"], "g", gid) if gid else (r["source_id"], "single", i)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    items = [_merge(groups[k]) for k in order]
    items.sort(key=lambda it: (it.published_at, _sort_key(it.external_id)), reverse=True)
    return items


def normalized_feed(db, city: str | None = None, since: str | None = None,
                    source_type: str | None = None, limit: int = 500) -> list[NormalizedItem]:
    """Лента конвейера: свежие посты из `raw_posts` в едином формате, альбомы склеены.

    Отфильтрованные по городу областные посты сюда не попадают: они лежат в базе
    для калибровки dedup/relevance (QUESTIONS §3), но новостями не являются.
    """
    where = ["p.filtered_out = 0"]
    params: list = []
    if city:
        where.append("s.city = ?")
        params.append(city)
    if since:
        where.append("p.published_at >= ?")
        params.append(since)
    if source_type:
        where.append("p.source_type = ?")
        params.append(source_type)

    rows = db.conn.execute(
        f"""
        SELECT p.id AS raw_post_id, p.source_id, p.source_type, p.external_id, p.grouped_id,
               p.text, p.url, p.published_at, p.views, s.city, s.key AS source_key
        FROM raw_posts p JOIN sources s ON s.id = p.source_id
        WHERE {" AND ".join(where)}
        ORDER BY p.published_at DESC, p.id DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    if not rows:
        return []

    media_by_post: dict[int, list[dict]] = {}
    ids = [r["raw_post_id"] for r in rows]
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        q = ",".join("?" * len(chunk))
        for m in db.conn.execute(
            f"SELECT raw_post_id, ord, kind, status, file_path FROM raw_media "
            f"WHERE raw_post_id IN ({q}) ORDER BY raw_post_id, ord", chunk
        ):
            media_by_post.setdefault(m["raw_post_id"], []).append(dict(m))

    return glue_albums([dict(r, media=media_by_post.get(r["raw_post_id"], [])) for r in rows])
