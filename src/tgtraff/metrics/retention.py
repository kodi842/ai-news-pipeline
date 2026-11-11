"""Ретеншн (спека, фаза 6): «`filtered_out`-посты старше 30 дней, медиа по политике».

Зачем это вообще правило фазы, а не украшение: живой замер перед деплоем —
2,8 ГБ медиа за три недели сбора, ~130 МБ в сутки. Диск VPS такой поток
переживёт недолго, а прогон обещан трёхсуточный и без ручных рестартов.

Политика медиа: файл живёт `media_days`, после чего уходит с диска. Запись в
`raw_media` остаётся — в ней `sha256` и `phash`, на которых стоит дедуп, и по
ней видно, что медиа у поста было. Файл, выбранный драфтом, не удаляется
никогда: он нужен и для правки вышедшего поста, и для перевыпуска.

Уборка идёт раз в локальные сутки — по той же меточной идемпотентности, что и
сервисные автопосты: любой процесс может её позвать, дважды она не отработает.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tgtraff.metrics.settings import RetentionSettings
from tgtraff.publisher.schedule import local_day

log = logging.getLogger("tgtraff.retention")

META_KEY = "retention:last_day"


@dataclass(frozen=True)
class RetentionResult:
    filtered_out: int = 0          # удалённых постов, не прошедших фильтр города
    filtered_files: int = 0
    media_files: int = 0           # файлов, снятых с диска по возрасту
    media_bytes: int = 0
    snapshots: int = 0
    llm_rows: int = 0

    @property
    def media_mb(self) -> float:
        return round(self.media_bytes / 1048576, 1)

    def summary(self) -> str:
        return (f"удалено постов вне города: {self.filtered_out}; "
                f"медиа снято с диска: {self.media_files} ({self.media_mb} МБ)")


class Retention:
    def __init__(self, db, settings: RetentionSettings | None = None):
        self.db = db
        self.settings = settings or RetentionSettings()

    def run(self, now: datetime | None = None, force: bool = False) -> RetentionResult | None:
        """Уборка. None — «сегодня уже убирались» или ретеншн выключен."""
        if not self.settings.enabled:
            return None
        now = now or datetime.now(timezone.utc)
        day = local_day(now, self.settings.tz)
        if not force and self.db.get_meta(META_KEY) == day:
            return None

        filtered, filtered_files = self._purge_filtered_out(now)
        files, freed = self._purge_old_media(now)
        result = RetentionResult(
            filtered_out=filtered, filtered_files=filtered_files,
            media_files=files, media_bytes=freed,
            snapshots=self._trim("subs_metrics", "captured_at", self.settings.subs_metrics_days, now),
            llm_rows=self._trim("llm_usage", "created_at", self.settings.llm_usage_days, now))
        self.db.set_meta(META_KEY, day)
        log.info("ретеншн: %s", result.summary())
        return result

    # ---------- спека: filtered_out старше 30 дней ----------

    def _purge_filtered_out(self, now: datetime) -> tuple[int, int]:
        """Пост областного источника, не упомянувший наш город, драфтом не станет
        никогда. Тексты нужны были для калибровки дедупа и relevance (§3) —
        через 30 дней и это уже история."""
        cutoff = _iso(now - timedelta(days=self.settings.filtered_out_days))
        rows = self.db.conn.execute(
            """SELECT m.id, m.file_path FROM raw_media m
               JOIN raw_posts p ON p.id = m.raw_post_id
               WHERE p.filtered_out=1 AND p.published_at < ? AND m.file_path IS NOT NULL""",
            (cutoff,)).fetchall()
        files = sum(_unlink(r["file_path"])[0] for r in rows)
        cur = self.db.conn.execute(
            "DELETE FROM raw_posts WHERE filtered_out=1 AND published_at < ?", (cutoff,))
        return cur.rowcount, files

    # ---------- спека: медиа по политике ----------

    def _purge_old_media(self, now: datetime) -> tuple[int, int]:
        cutoff = _iso(now - timedelta(days=self.settings.media_days))
        rows = self.db.conn.execute(
            """SELECT m.id, m.file_path, m.file_size FROM raw_media m
               JOIN raw_posts p ON p.id = m.raw_post_id
               WHERE m.file_path IS NOT NULL AND p.published_at < ?
                 -- медиа, выбранное драфтом, живёт столько же, сколько драфт:
                 -- по нему правят и перевыпускают вышедший пост
                 AND m.id NOT IN (SELECT media_raw_media_id FROM drafts
                                   WHERE media_raw_media_id IS NOT NULL)
                 AND m.file_path NOT IN (SELECT media_path FROM drafts
                                          WHERE media_path IS NOT NULL)""",
            (cutoff,)).fetchall()
        files = freed = 0
        for row in rows:
            removed, size = _unlink(row["file_path"])
            files += 1                            # запись чистим и если файла уже нет
            if removed:
                freed += size or (row["file_size"] or 0)
            self.db.conn.execute(
                """UPDATE raw_media
                      SET file_path=NULL, file_size=NULL, status='skipped',
                          error='файл снят с диска по ретеншну (фаза 6)',
                          updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    WHERE id=?""", (row["id"],))
        return files, freed

    # ---------- длинные ряды ----------

    def _trim(self, table: str, column: str, days: int, now: datetime) -> int:
        if not days:
            return 0
        cur = self.db.conn.execute(
            f"DELETE FROM {table} WHERE {column} < ?", (_iso(now - timedelta(days=days)),))
        return cur.rowcount


def _unlink(path: str | None) -> tuple[bool, int]:
    """Возвращает (файл был, сколько освободили). Файл могли убрать руками —
    это не ошибка уборки."""
    if not path:
        return False, 0
    file = Path(path)
    try:
        size = file.stat().st_size
    except OSError:
        return False, 0
    try:
        file.unlink()
    except OSError as exc:
        log.warning("не удалось удалить %s: %s", path, exc)
        return False, 0
    return True, size


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
