"""Хранилище клипов (VIDEO-SPEC, фаза V1).

Таблица `clips` — состояние клипа от скоринга до решения модератора:

    planned → rendering → ready → sent → published | rejected
                  ↓
               failed (выбраны попытки)

Очередь рендера лежит в БД, а не в памяти процесса: ffmpeg на 2 vCPU идёт
минутами, и рестарт контейнера не имеет права ни потерять клип, ни собрать его
дважды. Параллельность 1 держится двумя вещами: `claim_render` (planned →
rendering одним UPDATE) и тем, что рендерящий процесс берёт по одной записи.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("tgtraff.clips.store")

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

# Статусы, при которых клип драфта считается живым: второй такой не создаётся.
# `rehook` — модератор попросил другой хук, и клип ждёт завод: он тоже живой.
LIVE_STATUSES = ("planned", "rendering", "ready", "sent", "published", "rehook")
# Что съедает дневную квоту города: отклонённый и не собравшийся — не съедают.
COUNTED_STATUSES = ("planned", "rendering", "ready", "sent", "published", "rehook")


class ClipExists(RuntimeError):
    """У драфта уже есть живой клип — второй упаковки той же новости не будет."""


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def encode_lines(values) -> str | None:
    if not values:
        return None
    return json.dumps(list(values), ensure_ascii=False)


def decode_lines(row_or_value) -> list[str]:
    """Строки сценария из строки клипа (или прямо из JSON-значения)."""
    value = row_or_value
    if isinstance(row_or_value, sqlite3.Row):
        value = row_or_value["lines"]
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [str(value)]
    return [str(v) for v in parsed] if isinstance(parsed, list) else [str(parsed)]


def decode_flags(row) -> list[str]:
    return decode_lines(row["flags"] if isinstance(row, sqlite3.Row) else row)


# ---------- запись ----------

def save_clip(db, draft_id: int, city: str, mode: str, score: int | None = None,
              score_reason: str | None = None, hook: str | None = None,
              lines=None, cta: str | None = None, voice: str | None = None,
              flags=None, media_kind: str | None = None, media_ids=None,
              source_key: str | None = None, model: str | None = None,
              cost_usd: float | None = None) -> int:
    """Заводит клип в состоянии `planned` — очередь рендера его подхватит."""
    if clip_for_draft(db, draft_id) is not None:
        raise ClipExists(f"у драфта #{draft_id} клип уже есть")
    cur = db.conn.execute(
        f"""INSERT INTO clips (draft_id, city, mode, score, score_reason, hook, lines, cta,
                               voice, flags, media_kind, media_ids, source_key, model,
                               cost_usd, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'planned', {NOW_SQL}, {NOW_SQL})""",
        (draft_id, city, mode, score, score_reason, hook, encode_lines(lines), cta,
         voice, encode_lines(flags), media_kind, encode_lines(media_ids), source_key,
         model, cost_usd))
    return cur.lastrowid


def get_clip(db, clip_id: int):
    return db.conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()


def clip_for_draft(db, draft_id: int):
    """Живой клип драфта. Отклонённый и не собравшийся замок не держат."""
    marks = ",".join("?" * len(LIVE_STATUSES))
    return db.conn.execute(
        f"""SELECT * FROM clips WHERE draft_id=? AND status IN ({marks})
            ORDER BY id DESC LIMIT 1""", (draft_id, *LIVE_STATUSES)).fetchone()


def update_script(db, clip_id: int, hook: str, lines=None, cta: str | None = None,
                  model: str | None = None, cost_usd: float | None = None) -> None:
    """Перегенерённый сценарий (кнопка «Перегенерить хук»): клип возвращается
    в очередь рендера — файл собирается заново с новой плашкой."""
    row = get_clip(db, clip_id)
    spent = (row["cost_usd"] or 0.0) + (cost_usd or 0.0) if row is not None else cost_usd
    db.conn.execute(
        f"""UPDATE clips SET hook=?, lines=COALESCE(?, lines), cta=COALESCE(?, cta),
                model=COALESCE(?, model), cost_usd=?, status='planned', attempts=0,
                last_error=NULL, file_path=NULL, file_size=NULL, duration_sec=NULL,
                card_chat_id=NULL, card_message_id=NULL, card_sent_at=NULL,
                render_started_at=NULL, rendered_at=NULL, updated_at={NOW_SQL}
            WHERE id=?""",
        (hook, encode_lines(lines), cta, model, spent, clip_id))


# ---------- очередь рендера ----------

def pending_render(db, limit: int = 5) -> list[sqlite3.Row]:
    """Что ждёт сборки. Лучший скор вперёд: если очередь не разойдётся за тик,
    первым соберётся клип с большим виральным потенциалом."""
    return db.conn.execute(
        """SELECT * FROM clips WHERE status='planned'
           ORDER BY score DESC, id LIMIT ?""", (limit,)).fetchall()


def claim_render(db, clip_id: int) -> bool:
    """Забрать клип в рендер. False — его уже забрал другой тик/процесс."""
    cur = db.conn.execute(
        f"""UPDATE clips SET status='rendering', attempts=attempts+1,
                render_started_at={NOW_SQL}, updated_at={NOW_SQL}
            WHERE id=? AND status='planned'""", (clip_id,))
    return bool(cur.rowcount)


def mark_rendered(db, clip_id: int, path: str, size: int, duration: float,
                  subtitles: str = "none") -> None:
    db.conn.execute(
        f"""UPDATE clips SET status='ready', file_path=?, file_size=?, duration_sec=?,
                subtitles=?, last_error=NULL, rendered_at={NOW_SQL}, updated_at={NOW_SQL}
            WHERE id=?""", (path, size, duration, subtitles, clip_id))


def mark_failed(db, clip_id: int, error: str, max_attempts: int = 3) -> str:
    """Сбой рендера возвращает клип в очередь, пока не выбраны попытки."""
    db.conn.execute(
        f"""UPDATE clips
                SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'planned' END,
                    last_error=?, updated_at={NOW_SQL}
            WHERE id=?""", (max_attempts, str(error)[:500], clip_id))
    return get_clip(db, clip_id)["status"]


def release_stuck(db, older_than_minutes: int = 15) -> int:
    """Клип, застрявший в `rendering` (процесс убили посреди ffmpeg), возвращается
    в очередь: иначе он молча пропал бы."""
    cutoff = iso(datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes))
    cur = db.conn.execute(
        f"""UPDATE clips SET status='planned', updated_at={NOW_SQL}
            WHERE status='rendering' AND COALESCE(render_started_at,'') < ?""", (cutoff,))
    return cur.rowcount


# ---------- карточка и решения ----------

def ready_for_card(db, limit: int = 5) -> list[sqlite3.Row]:
    return db.conn.execute(
        """SELECT * FROM clips WHERE status='ready' AND card_sent_at IS NULL
           ORDER BY score DESC, id LIMIT ?""", (limit,)).fetchall()


def mark_card(db, clip_id: int, chat_id: int, message_id: int) -> None:
    db.conn.execute(
        f"""UPDATE clips SET status='sent', card_chat_id=?, card_message_id=?,
                card_sent_at={NOW_SQL}, updated_at={NOW_SQL} WHERE id=?""",
        (chat_id, message_id, clip_id))


def clip_of_card(db, message_id: int) -> int | None:
    row = db.conn.execute(
        "SELECT id FROM clips WHERE card_message_id=? ORDER BY id DESC LIMIT 1",
        (message_id,)).fetchone()
    return row["id"] if row else None


def mark_published(db, clip_id: int, moderator_id: int) -> None:
    """V1: публикует руками человек, кнопка фиксирует его решение."""
    db.conn.execute(
        f"""UPDATE clips SET status='published', moderator_id=?, decided_at={NOW_SQL},
                updated_at={NOW_SQL} WHERE id=?""", (moderator_id, clip_id))


def request_rehook(db, clip_id: int, moderator_id: int) -> None:
    """Кнопка «Перегенерить хук»: клип ждёт нового сценария от завода.

    Платный запрос делает контейнер клипов, а не чат модерации: держать
    промпты и ключ в двух процессах — способ однажды разойтись версиями."""
    db.conn.execute(
        f"""UPDATE clips SET status='rehook', moderator_id=?, updated_at={NOW_SQL}
            WHERE id=?""", (moderator_id, clip_id))


def pending_rehook(db, limit: int = 5) -> list[sqlite3.Row]:
    return db.conn.execute(
        "SELECT * FROM clips WHERE status='rehook' ORDER BY id LIMIT ?", (limit,)).fetchall()


def restore_ready(db, clip_id: int) -> None:
    """Перегенерация не удалась — клип возвращается модератору как был.

    Файл на месте (его никто не трогал), поэтому карточка уйдёт заново с тем
    же хуком: молча потерять готовый клип из-за упавшей модели нельзя."""
    db.conn.execute(
        f"""UPDATE clips SET status='ready', card_chat_id=NULL, card_message_id=NULL,
                card_sent_at=NULL, updated_at={NOW_SQL} WHERE id=?""", (clip_id,))


def mark_rejected(db, clip_id: int, moderator_id: int, reason: str | None = None) -> None:
    db.conn.execute(
        f"""UPDATE clips SET status='rejected', moderator_id=?, reject_reason=?,
                decided_at={NOW_SQL}, updated_at={NOW_SQL} WHERE id=?""",
        (moderator_id, reason, clip_id))


# ---------- лимиты и ретеншн ----------

def made_today(db, city: str, tz: str, now_iso: str | None = None) -> int:
    """Сколько клипов города сделано за ЛОКАЛЬНЫЕ сутки (лимит `clips_per_day`).

    Считаются только живые: отклонённый и не собравшийся квоту не съедают —
    иначе один сбой ffmpeg стоил бы городу клипа на весь день."""
    from tgtraff.publisher.schedule import local_day

    now = parse(now_iso) if now_iso else datetime.now(timezone.utc)
    day = local_day(now, tz)
    marks = ",".join("?" * len(COUNTED_STATUSES))
    rows = db.conn.execute(
        f"""SELECT created_at FROM clips WHERE city=? AND status IN ({marks})
              AND created_at BETWEEN ? AND ?""",
        (city, *COUNTED_STATUSES, iso(now - timedelta(days=2)),
         iso(now + timedelta(days=2)))).fetchall()
    return sum(1 for r in rows if local_day(parse(r["created_at"]), tz) == day)


def counts(db) -> dict[str, int]:
    rows = db.conn.execute("SELECT status, COUNT(*) c FROM clips GROUP BY status")
    return {r["status"]: r["c"] for r in rows}


def purge_old(db, days: int = 14) -> int:
    """Ретеншн как у медиа: файл клипа уходит с диска, строка остаётся.

    Строка нужна отчётам (что сделали, что зашло) и правилу «один клип на
    драфт»; сам mp4 после публикации на площадке нам больше не нужен."""
    cutoff = iso(datetime.now(timezone.utc) - timedelta(days=days))
    rows = db.conn.execute(
        "SELECT id, file_path FROM clips WHERE file_path IS NOT NULL "
        "AND COALESCE(rendered_at, created_at) < ?", (cutoff,)).fetchall()
    removed = 0
    for row in rows:
        Path(row["file_path"]).unlink(missing_ok=True)   # файл могли убрать руками
        db.conn.execute(
            f"UPDATE clips SET file_path=NULL, updated_at={NOW_SQL} WHERE id=?", (row["id"],))
        removed += 1
    if removed:
        log.info("ретеншн клипов: снято с диска %s файлов старше %s дней", removed, days)
    return removed
