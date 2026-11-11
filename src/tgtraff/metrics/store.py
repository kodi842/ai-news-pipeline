"""Хранилище метрик (спека §9): просмотры, снимки канала, инвайт-ссылки.

Весь SQL фазы 6 собран здесь по той же причине, что и в publisher'е: считалки
и отчёты не должны знать про схему, а тесты — поднимать процессы.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- просмотры собственных постов ----------

def posts_for_views(db, now: datetime, window_hours: int = 72,
                    limit: int = 100) -> list[sqlite3.Row]:
    """Опубликованные посты, у которых ещё имеет смысл снимать просмотры.

    Порядок — «давно не смотрели вперёд»: свежий пост без единого замера важнее
    вчерашнего, у которого просмотры уже есть."""
    since = iso(now - timedelta(hours=window_hours))
    return db.conn.execute(
        """SELECT id, city, channel, message_id, published_at, views, views_at
           FROM published
           WHERE platform='tg' AND deleted_at IS NULL AND message_id IS NOT NULL
             AND published_at >= ?
           ORDER BY (views_at IS NOT NULL), COALESCE(views_at, published_at)
           LIMIT ?""", (since, limit)).fetchall()


def save_views(db, published_id: int, views: int) -> None:
    """Просмотры только растут: снимок, пришедший меньше уже записанного, —
    это сбой замера (пост мог попасть в другой запрос), а не падение охвата."""
    db.conn.execute(
        f"""UPDATE published SET views=MAX(COALESCE(views,0), ?), views_at={NOW_SQL}
            WHERE id=?""", (int(views), published_id))


def views_coverage(db, now: datetime, window_hours: int = 24) -> tuple[int, int]:
    """(постов в окне, из них с замером) — по этому монитор понимает, снимаются
    ли просмотры вообще."""
    since = iso(now - timedelta(hours=window_hours))
    row = db.conn.execute(
        """SELECT COUNT(*) total, SUM(views IS NOT NULL) measured FROM published
           WHERE platform='tg' AND deleted_at IS NULL AND published_at >= ?""",
        (since,)).fetchone()
    return int(row["total"] or 0), int(row["measured"] or 0)


# ---------- снимки канала ----------

def reach(db, city: str, since: datetime, until: datetime | None = None) -> dict:
    """Охват города за окно: посты, суммарные и средние просмотры."""
    params = [city, iso(since)]
    upper = ""
    if until is not None:
        upper = " AND published_at < ?"
        params.append(iso(until))
    row = db.conn.execute(
        f"""SELECT COUNT(*) posts, SUM(COALESCE(views,0)) views,
                   SUM(views IS NOT NULL) measured
            FROM published
            WHERE city=? AND platform='tg' AND deleted_at IS NULL
              AND published_at >= ?{upper}""", params).fetchone()
    posts, views = int(row["posts"] or 0), int(row["views"] or 0)
    measured = int(row["measured"] or 0)
    return {"posts": posts, "views": views, "measured": measured,
            "avg": round(views / measured, 1) if measured else None}


def save_snapshot(db, city: str, channel: str, subscribers: int | None,
                  views_24h: int | None = None, er: float | None = None,
                  posts_24h: int | None = None, platform: str = "tg",
                  captured_at: datetime | None = None) -> int:
    cur = db.conn.execute(
        """INSERT INTO subs_metrics (city, platform, channel, captured_at, subscribers,
                                     views_24h, er, posts_24h)
           VALUES (?,?,?,?,?,?,?,?)""",
        (city, platform, channel, iso(captured_at or datetime.now(timezone.utc)),
         subscribers, views_24h, er, posts_24h))
    return cur.lastrowid


def last_snapshot(db, city: str, platform: str = "tg", before: datetime | None = None):
    """Последний снимок города (при `before` — последний ДО момента: так
    считается суточный прирост подписчиков)."""
    params: list = [city, platform]
    where = ""
    if before is not None:
        where = " AND captured_at <= ?"
        params.append(iso(before))
    return db.conn.execute(
        f"""SELECT * FROM subs_metrics WHERE city=? AND platform=?{where}
            ORDER BY captured_at DESC LIMIT 1""", params).fetchone()


# ---------- инвайт-ссылки ----------

def ensure_link(db, city: str, channel: str, name: str, url: str | None = None,
                chat_id: str | None = None, kind: str = "other",
                note: str | None = None) -> sqlite3.Row:
    """Заводит ссылку под источник трафика или возвращает уже заведённую.

    Ключ — (город, канал, имя источника): повторный вызов не создаёт второй
    ссылки, иначе прирост площадки размажется по двойникам."""
    db.conn.execute(
        """INSERT OR IGNORE INTO invite_links (city, channel, name, url, chat_id, kind, note)
           VALUES (?,?,?,?,?,?,?)""", (city, channel, name, url, chat_id, kind, note))
    db.conn.execute(
        """UPDATE invite_links
              SET url=COALESCE(?, url), chat_id=COALESCE(?, chat_id),
                  kind=COALESCE(?, kind), note=COALESCE(?, note)
            WHERE city=? AND channel=? AND name=?""",
        (url, chat_id, kind, note, city, channel, name))
    return get_link(db, city, channel, name)


def get_link(db, city: str, channel: str, name: str):
    return db.conn.execute(
        "SELECT * FROM invite_links WHERE city=? AND channel=? AND name=?",
        (city, channel, name)).fetchone()


def link_by_url(db, url: str):
    return db.conn.execute("SELECT * FROM invite_links WHERE url=?", (url,)).fetchone()


def links(db, city: str | None = None, include_revoked: bool = False) -> list[sqlite3.Row]:
    where, params = ["1=1"], []
    if city:
        where.append("city=?")
        params.append(city)
    if not include_revoked:
        where.append("revoked=0")
    return db.conn.execute(
        f"SELECT * FROM invite_links WHERE {' AND '.join(where)} ORDER BY city, kind, name",
        params).fetchall()


def record_join(db, link_id: int, day: str, count: int = 1) -> None:
    """Вступление по ссылке. Считаем и всего, и по дням: «всего» отвечает на
    вопрос «сколько привела площадка», по дням — «сколько привела вчера»."""
    db.conn.execute(
        f"""UPDATE invite_links SET joins_total = joins_total + ?, last_checked_at={NOW_SQL}
            WHERE id=?""", (count, link_id))
    db.conn.execute(
        """INSERT INTO invite_link_days (link_id, day, joins) VALUES (?,?,?)
           ON CONFLICT(link_id, day) DO UPDATE SET joins = joins + excluded.joins""",
        (link_id, day, count))


def set_cost(db, link_id: int, cost_rub: float) -> None:
    """Сколько заплачено за площадку. Вводит оператор — автоматически это
    знание взять неоткуда (кабинеты VK Ads и переписка с админами пабликов)."""
    db.conn.execute("UPDATE invite_links SET cost_rub=? WHERE id=?", (float(cost_rub), link_id))


def joins_in_days(db, link_id: int, days: tuple[str, ...]) -> int:
    if not days:
        return 0
    row = db.conn.execute(
        f"""SELECT SUM(joins) j FROM invite_link_days
            WHERE link_id=? AND day IN ({','.join('?' * len(days))})""",
        (link_id, *days)).fetchone()
    return int(row["j"] or 0)


def joins_by_day(db, day: str) -> dict[int, int]:
    rows = db.conn.execute(
        "SELECT link_id, joins FROM invite_link_days WHERE day=?", (day,)).fetchall()
    return {r["link_id"]: r["joins"] for r in rows}


def growth(db, city: str, now: datetime, hours: int = 24, platform: str = "tg") -> int | None:
    """Прирост подписчиков за окно: последний снимок минус снимок «сутки назад».

    Пустой ответ — не ноль: одного снимка мало, чтобы говорить о приросте."""
    latest = last_snapshot(db, city, platform, before=now)
    earlier = last_snapshot(db, city, platform, before=now - timedelta(hours=hours))
    if latest is None or earlier is None or latest["id"] == earlier["id"]:
        return None
    if latest["subscribers"] is None or earlier["subscribers"] is None:
        return None
    return int(latest["subscribers"]) - int(earlier["subscribers"])


def mark_deleted(db, published_id: int) -> None:
    """Пост удалили в канале руками, мимо админ-бота. Помечаем — иначе он
    останется в базе живым навсегда: и в топе дня, и в охвате."""
    db.conn.execute(
        f"UPDATE published SET deleted_at={NOW_SQL} WHERE id=? AND deleted_at IS NULL",
        (published_id,))
