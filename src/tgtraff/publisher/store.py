"""Хранилище публикаций (спека §8): очередь и карта «драфт → сообщение».

Две таблицы:
  * `publish_queue` — что и когда выпускаем. Лежит в БД, а не в памяти: рестарт
    publisher'а не теряет одобренный пост и не выпускает его дважды (за это
    отвечает `claim` — переход queued→sending одним UPDATE с проверкой статуса).
  * `published` — что уже вышло, с message_id канала. Без этой карты админ-бот
    не сможет ни поправить, ни удалить опубликованное (требование спеки §8).

Времена в базе — ISO8601 UTC; локальные сутки города считает `schedule.local_day`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from tgtraff.publisher.schedule import local_day

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

LIVE_STATUSES = ("queued", "sending")
COUNTED_STATUSES = ("queued", "sending", "published")


class AlreadyQueued(RuntimeError):
    """У драфта уже есть живая запись очереди — второй публикации не будет."""


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------- очередь ----------

def enqueue(db, draft_id: int, city: str, scheduled_at: datetime,
            urgency: str = "normal", moderator_id: int | None = None) -> int:
    try:
        cur = db.conn.execute(
            f"""INSERT INTO publish_queue (draft_id, city, urgency, status, scheduled_at,
                                           moderator_id, created_at, updated_at)
                VALUES (?,?,?, 'queued', ?,?,{NOW_SQL},{NOW_SQL})""",
            (draft_id, city, urgency, iso(scheduled_at), moderator_id))
    except sqlite3.IntegrityError as exc:
        raise AlreadyQueued(f"драфт #{draft_id} уже в очереди") from exc
    return cur.lastrowid


def get_queued(db, queue_id: int):
    return db.conn.execute("SELECT * FROM publish_queue WHERE id=?", (queue_id,)).fetchone()


def queue_row_for_draft(db, draft_id: int):
    return db.conn.execute(
        f"""SELECT * FROM publish_queue WHERE draft_id=?
            AND status IN ({','.join('?' * len(LIVE_STATUSES))})
            ORDER BY id DESC LIMIT 1""", (draft_id, *LIVE_STATUSES)).fetchone()


def due(db, now: datetime, limit: int = 10) -> list[sqlite3.Row]:
    """Что пора публиковать. Молния впереди обычных постов даже при равном времени."""
    return db.conn.execute(
        """SELECT q.*, d.city AS draft_city FROM publish_queue q
           JOIN drafts d ON d.id = q.draft_id
           WHERE q.status='queued' AND q.scheduled_at <= ?
           ORDER BY CASE q.urgency WHEN 'flash' THEN 0 ELSE 1 END, q.scheduled_at, q.id
           LIMIT ?""", (iso(now), limit)).fetchall()


def claim(db, queue_id: int) -> bool:
    """Забрать запись в работу. False — её уже забрал другой тик/процесс."""
    cur = db.conn.execute(
        f"UPDATE publish_queue SET status='sending', attempts=attempts+1, updated_at={NOW_SQL} "
        "WHERE id=? AND status='queued'", (queue_id,))
    return bool(cur.rowcount)


def mark_published(db, queue_id: int) -> None:
    db.conn.execute(
        f"""UPDATE publish_queue SET status='published', published_at={NOW_SQL},
                last_error=NULL, updated_at={NOW_SQL} WHERE id=?""", (queue_id,))


def mark_failed(db, queue_id: int, error: str, max_attempts: int = 3) -> str:
    """Ошибка доставки возвращает пост в очередь, пока не выбраны попытки:
    сеть моргает, Telegram отвечает 429. После лимита — failed и алерт."""
    db.conn.execute(
        f"""UPDATE publish_queue
                SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END,
                    last_error=?, updated_at={NOW_SQL}
            WHERE id=?""", (max_attempts, str(error)[:500], queue_id))
    return get_queued(db, queue_id)["status"]


def cancel(db, queue_id: int) -> None:
    db.conn.execute(
        f"UPDATE publish_queue SET status='cancelled', updated_at={NOW_SQL} WHERE id=?",
        (queue_id,))


def cancel_for_draft(db, draft_id: int) -> int:
    cur = db.conn.execute(
        f"""UPDATE publish_queue SET status='cancelled', updated_at={NOW_SQL}
            WHERE draft_id=? AND status IN ({','.join('?' * len(LIVE_STATUSES))})""",
        (draft_id, *LIVE_STATUSES))
    return cur.rowcount


def release_stuck(db, older_than_minutes: int = 15) -> int:
    """Запись, застрявшая в `sending` (процесс убили на полпути), возвращается
    в очередь: без этого пост молча пропал бы."""
    cutoff = iso(datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes))
    cur = db.conn.execute(
        f"""UPDATE publish_queue SET status='queued', updated_at={NOW_SQL}
            WHERE status='sending' AND updated_at < ?""", (cutoff,))
    return cur.rowcount


def stale(db, now: datetime, older_than_hours: int = 12) -> list[sqlite3.Row]:
    """Посты, просидевшие в очереди дольше жизни городской новости."""
    cutoff = iso(now - timedelta(hours=older_than_hours))
    return db.conn.execute(
        "SELECT * FROM publish_queue WHERE status='queued' AND scheduled_at < ? "
        "ORDER BY scheduled_at", (cutoff,)).fetchall()


def last_planned_at(db, city: str) -> datetime | None:
    """Последний занятый момент города — от него отсчитывается пауза 20-40 мин."""
    row = db.conn.execute(
        f"""SELECT MAX(scheduled_at) m FROM publish_queue
            WHERE city=? AND status IN ({','.join('?' * len(COUNTED_STATUSES))})""",
        (city, *COUNTED_STATUSES)).fetchone()
    return parse(row["m"]) if row and row["m"] else None


def day_load(db, city: str, tz: str, now: datetime) -> int:
    """Сколько постов уже стоит на сегодняшние локальные сутки города —
    это и есть счётчик дневного потолка (спека §8)."""
    day = local_day(now, tz)
    window_from = iso(now - timedelta(days=2))
    window_to = iso(now + timedelta(days=2))
    rows = db.conn.execute(
        f"""SELECT scheduled_at FROM publish_queue
            WHERE city=? AND status IN ({','.join('?' * len(COUNTED_STATUSES))})
              AND scheduled_at BETWEEN ? AND ?""",
        (city, *COUNTED_STATUSES, window_from, window_to)).fetchall()
    return sum(1 for r in rows if local_day(parse(r["scheduled_at"]), tz) == day)


def queue_counts(db, city: str | None = None) -> dict[str, int]:
    where, params = ("WHERE city=?", [city]) if city else ("", [])
    rows = db.conn.execute(
        f"SELECT status, COUNT(*) c FROM publish_queue {where} GROUP BY status", params)
    return {r["status"]: r["c"] for r in rows}


# ---------- что уже опубликовано ----------

def record_publication(db, draft_id: int, city: str, platform: str, channel: str,
                       message_id: str, url: str | None = None,
                       extra_message_ids=None) -> int:
    cur = db.conn.execute(
        f"""INSERT INTO published (draft_id, city, platform, channel, message_id, url,
                                   extra_message_ids, published_at)
            VALUES (?,?,?,?,?,?,?,{NOW_SQL})""",
        (draft_id, city, platform, channel, str(message_id), url,
         json.dumps(list(extra_message_ids), ensure_ascii=False) if extra_message_ids else None))
    return cur.lastrowid


def publications(db, draft_id: int, include_deleted: bool = False) -> list[sqlite3.Row]:
    where = "" if include_deleted else " AND deleted_at IS NULL"
    return db.conn.execute(
        f"SELECT * FROM published WHERE draft_id=?{where} ORDER BY id", (draft_id,)).fetchall()


def decode_extra(row) -> list[str]:
    raw = row["extra_message_ids"] if row else None
    if not raw:
        return []
    try:
        return [str(x) for x in json.loads(raw)]
    except (TypeError, ValueError):
        return []


def mark_deleted(db, publication_id: int) -> None:
    db.conn.execute(f"UPDATE published SET deleted_at={NOW_SQL} WHERE id=?", (publication_id,))


def mark_edited(db, publication_id: int) -> None:
    db.conn.execute(f"UPDATE published SET edited_at={NOW_SQL} WHERE id=?", (publication_id,))
