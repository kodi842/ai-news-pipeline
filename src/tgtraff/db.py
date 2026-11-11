"""SQLite-хранилище конвейера.

Две гарантии фазы 1 держатся здесь, а не в коде коллектора:
  * «не дублирует» — UNIQUE(source_id, external_id) + INSERT OR IGNORE;
  * «не теряет»    — max(external_id) на источник для догона после простоя
                     и очередь незакачанного медиа (pending/failed).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class InsertResult:
    id: int
    is_new: bool


@dataclass(frozen=True)
class PendingMedia:
    media_id: int
    raw_post_id: int
    ord: int
    kind: str
    attempts: int
    source_key: str
    source_city: str
    source_type: str
    external_id: str
    source_url: str | None = None


def _now() -> str:
    return "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    # ---------- schema ----------

    # Колонки, добавленные после первого релиза схемы: CREATE TABLE IF NOT EXISTS
    # их на живой базе не создаст, поэтому доводим ALTER'ом.
    _ADDED_COLUMNS = (
        ("raw_posts", "via", "TEXT NOT NULL DEFAULT 'live'"),
        # фаза 2: источники, которые опрашиваются по HTTP, и медиа по прямой ссылке
        ("sources", "last_polled_at", "TEXT"),
        ("sources", "last_error", "TEXT"),
        ("sources", "http_etag", "TEXT"),
        ("sources", "http_last_modified", "TEXT"),
        ("raw_media", "source_url", "TEXT"),
        ("warmup_state", "kind", "TEXT NOT NULL DEFAULT 'username'"),
        # фаза 3: дедуп — хэш картинки и «почему склеили» у участника кластера
        ("raw_media", "phash", "TEXT"),
        ("clusters", "size", "INTEGER NOT NULL DEFAULT 1"),
        ("cluster_members", "similarity", "REAL"),
        ("cluster_members", "reason", "TEXT"),
        ("cluster_members", "matched_raw_post_id", "INTEGER"),
        ("post_signatures", "entities", "TEXT"),
        # фаза 4: официальность источника (медиа-каскад §5), вердикт вотермарк-детектора,
        # relevance-решение по кластеру и всё, что rewriter кладёт в драфт.
        ("sources", "official", "INTEGER NOT NULL DEFAULT 0"),
        ("raw_media", "watermark", "TEXT"),
        ("raw_media", "watermark_owner", "TEXT"),
        ("raw_media", "watermark_checked_at", "TEXT"),
        ("clusters", "relevance", "TEXT"),
        ("clusters", "relevance_reason", "TEXT"),
        ("clusters", "relevance_at", "TEXT"),
        ("drafts", "relevance", "TEXT"),
        ("drafts", "safety_reasons", "TEXT"),
        ("drafts", "media_kind", "TEXT"),
        ("drafts", "media_reason", "TEXT"),
        ("drafts", "media_raw_media_id", "INTEGER"),
        ("drafts", "sources", "TEXT"),
        ("drafts", "model", "TEXT"),
        ("drafts", "cost_usd", "REAL"),
        ("drafts", "notes", "TEXT"),
        # фаза 5: модерация (кто и почему решил), район для хэштега поста,
        # происхождение драфта (конвейер / UGC / сервисный автопост) и снимок
        # кластера на момент отклонения — по нему §28 понимает, что фактура
        # заметно приросла и драфт можно предложить повторно.
        ("drafts", "district", "TEXT"),
        ("drafts", "origin", "TEXT NOT NULL DEFAULT 'news'"),
        ("drafts", "reject_reason", "TEXT"),
        ("drafts", "moderator_id", "INTEGER"),
        ("drafts", "decided_at", "TEXT"),
        ("drafts", "reoffer_size", "INTEGER"),
        ("drafts", "reoffer_media", "INTEGER"),
        ("drafts", "ugc_user_id", "INTEGER"),
        ("drafts", "ugc_username", "TEXT"),
        # Карточка драфта в чате модератора: по ней бот правит сообщение после
        # решения, а `card_sent_at` отвечает на вопрос «что ещё не показывали».
        ("drafts", "card_chat_id", "INTEGER"),
        ("drafts", "card_message_id", "INTEGER"),
        ("drafts", "card_sent_at", "TEXT"),
        # §59: автопубликация нейтральной новости. Отметка отвечает на вопрос
        # «этот пост вышел без человека» — по ней считаются автопосты в
        # дайджесте и в сводке /queue, и по ней же разбирают инцидент.
        ("drafts", "autopublished_at", "TEXT"),
        ("published", "extra_message_ids", "TEXT"),
        ("published", "edited_at", "TEXT"),
        # Просмотры собственных постов собирает фаза 6 (метрики); вечерний
        # топ-5 читает эту колонку, а пока её нет — считает по числу источников.
        ("published", "views", "INTEGER"),
        # фаза 6: метрики. Когда просмотры снимали в последний раз — по этому
        # полю видно, работает ли съём вообще (пустая колонка views у свежего
        # поста — это «ещё не смотрели», а не «ноль просмотров»).
        ("published", "views_at", "TEXT"),
        # Инвайт-ссылки под источники трафика (спека §9): своя ссылка каждому
        # посеву, чтобы CPF считался по площадкам, а не в среднем по больнице.
        ("invite_links", "chat_id", "TEXT"),
        ("invite_links", "kind", "TEXT"),
        ("invite_links", "cost_rub", "REAL NOT NULL DEFAULT 0"),
        ("invite_links", "revoked", "INTEGER NOT NULL DEFAULT 0"),
        ("invite_links", "note", "TEXT"),
        # Снимок канала: сколько постов попало в окно охвата — без этого ER
        # нельзя пересчитать, а только поверить записанному числу.
        ("subs_metrics", "posts_24h", "INTEGER"),
    )

    def _init_schema(self) -> None:
        # Сначала доводим колонки уже существующих таблиц: в schema.sql есть
        # индексы по новым колонкам, и на живой базе скрипт упал бы на них
        # раньше, чем миграция успела бы отработать.
        self._migrate_columns()
        self.conn.executescript(SCHEMA_PATH.read_text("utf-8"))
        self._migrate_columns()
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))

    def _migrate_columns(self) -> None:
        for table, column, decl in self._ADDED_COLUMNS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not cols:          # таблицы ещё нет — её создаст schema.sql уже с колонкой
                continue
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def schema_version(self) -> int:
        return self.conn.execute("SELECT version FROM schema_version").fetchone()[0]

    def close(self) -> None:
        self.conn.close()

    # ---------- служебные отметки ----------

    def set_meta(self, key: str, value: str) -> None:
        """Отметки уровня установки: когда заведена сессия и т.п."""
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ---------- sources ----------

    def upsert_source(self, spec) -> int:
        """Заводит/обновляет источник. Ключ — (город, тип, адрес): один и тот же
        областной канал в двух городах — два разных источника со своим фильтром."""
        key = spec.key or f"?{spec.query}"
        self.conn.execute(
            f"""
            INSERT INTO sources (city, source_type, key, query, title, note, status, priority,
                                 city_filter, official)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (city, source_type, key) DO UPDATE SET
                query=excluded.query, title=excluded.title, note=excluded.note,
                status=excluded.status, priority=excluded.priority,
                city_filter=excluded.city_filter, official=excluded.official,
                updated_at={_now()}
            """,
            (spec.city, spec.source_type, key, spec.query, spec.title, spec.note,
             spec.status, spec.priority, spec.city_filter, int(getattr(spec, "is_official", False))),
        )
        return self.conn.execute(
            "SELECT id FROM sources WHERE city=? AND source_type=? AND key=?",
            (spec.city, spec.source_type, key),
        ).fetchone()["id"]

    def get_source(self, source_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()

    def mark_source_joined(self, source_id: int) -> None:
        self.conn.execute(f"UPDATE sources SET joined_at={_now()}, updated_at={_now()} WHERE id=?", (source_id,))

    def set_source_peer(self, source_id: int, peer_id: int) -> None:
        self.conn.execute("UPDATE sources SET tg_peer_id=? WHERE id=?", (peer_id, source_id))

    def mark_source_polled(self, source_id: int, etag: str | None = None,
                           last_modified: str | None = None) -> None:
        """Удачный опрос: обновляем время и заголовки условного GET, ошибку снимаем."""
        self.conn.execute(
            f"""UPDATE sources SET last_polled_at={_now()}, last_error=NULL,
                    http_etag=COALESCE(?, http_etag),
                    http_last_modified=COALESCE(?, http_last_modified)
                WHERE id=?""",
            (etag, last_modified, source_id),
        )

    def mark_source_error(self, source_id: int, error: str) -> None:
        """Источник недоступен. Это состояние источника, а не сбой процесса:
        поллер идёт дальше, а алерт (фаза 5) читает last_error."""
        self.conn.execute(
            f"UPDATE sources SET last_polled_at={_now()}, last_error=? WHERE id=?",
            (error[:500], source_id),
        )

    # ---------- raw posts ----------

    def insert_raw_post(
        self,
        source_id: int,
        source_type: str,
        external_id: str,
        published_at: str,
        text: str | None = None,
        url: str | None = None,
        grouped_id: str | None = None,
        edited_at: str | None = None,
        views: int | None = None,
        fwd_from: str | None = None,
        has_media: int = 0,
        media_state: str = "none",
        filtered_out: int = 0,
        filter_reason: str | None = None,
        raw_json: str | None = None,
        via: str = "live",
    ) -> InsertResult:
        """Идемпотентная запись поста. Повтор того же (source_id, external_id) —
        не ошибка и не дубль: возвращается существующий id с is_new=False."""
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO raw_posts
                (source_id, source_type, external_id, grouped_id, text, url, published_at,
                 edited_at, views, fwd_from, has_media, media_state, filtered_out, filter_reason,
                 raw_json, via)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (source_id, source_type, str(external_id), grouped_id, text, url, published_at,
             edited_at, views, fwd_from, has_media, media_state, filtered_out, filter_reason,
             raw_json, via),
        )
        if cur.rowcount:
            post_id = cur.lastrowid
            self.conn.execute(
                "UPDATE sources SET last_post_at=MAX(COALESCE(last_post_at,''), ?) WHERE id=?",
                (published_at, source_id),
            )
            return InsertResult(id=post_id, is_new=True)

        row = self.conn.execute(
            "SELECT id FROM raw_posts WHERE source_id=? AND external_id=?",
            (source_id, str(external_id)),
        ).fetchone()
        if row is None:  # источника нет — INSERT OR IGNORE проглотил бы FK, поэтому проверяем явно
            raise sqlite3.IntegrityError(f"raw_post не записан: нет источника id={source_id}")
        return InsertResult(id=row["id"], is_new=False)

    def last_seen_external_id(self, source_id: int) -> int | None:
        """Максимальный уже собранный message_id источника — точка догона после простоя."""
        row = self.conn.execute(
            "SELECT MAX(CAST(external_id AS INTEGER)) AS m FROM raw_posts WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return row["m"]

    def count_raw_posts(self, source_id: int | None = None) -> int:
        if source_id is None:
            return self.conn.execute("SELECT COUNT(*) c FROM raw_posts").fetchone()["c"]
        return self.conn.execute(
            "SELECT COUNT(*) c FROM raw_posts WHERE source_id=?", (source_id,)
        ).fetchone()["c"]

    # ---------- media ----------

    def add_media(self, raw_post_id: int, ord: int, kind: str, source_url: str | None = None) -> int:
        """source_url заполняют ВК и RSS: их медиа качается обычным HTTP.
        У Telegram ссылки нет — файл тянется Telethon по самому сообщению."""
        self.conn.execute(
            "INSERT OR IGNORE INTO raw_media (raw_post_id, ord, kind, source_url) VALUES (?,?,?,?)",
            (raw_post_id, ord, kind, source_url),
        )
        return self.conn.execute(
            "SELECT id FROM raw_media WHERE raw_post_id=? AND ord=?", (raw_post_id, ord)
        ).fetchone()["id"]

    def pending_media(self, max_attempts: int = 3, limit: int = 200,
                      source_types: tuple[str, ...] | None = None) -> list[PendingMedia]:
        """Медиа, которое ещё надо скачать: не докачанное или упавшее в пределах лимита попыток.
        Переживает рестарт — очередь лежит в БД, а не в памяти процесса.

        Свежее вперёд: исторический догон не должен задерживать медиа сегодняшней новости
        (скорость публикации ЧП — конкурентное преимущество, спека «Контекст»).

        source_types сужает очередь под конкретного качальщика: Telethon умеет только
        свои источники, ВК и RSS качаются обычным HTTP."""
        type_filter, params = "", [max_attempts]
        if source_types:
            type_filter = f" AND p.source_type IN ({','.join('?' * len(source_types))})"
            params.extend(source_types)
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT m.id AS media_id, m.raw_post_id, m.ord, m.kind, m.attempts,
                   s.key AS source_key, s.city AS source_city, p.source_type,
                   p.external_id, m.source_url
            FROM raw_media m
            JOIN raw_posts p ON p.id = m.raw_post_id
            JOIN sources   s ON s.id = p.source_id
            WHERE m.status IN ('pending','failed') AND m.attempts < ?{type_filter}
            ORDER BY p.published_at DESC, m.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [PendingMedia(**dict(r)) for r in rows]

    def skip_stale_media(self, cutoff_iso: str, reason: str = "исторический догон") -> int:
        """Снимает с очереди медиа постов старше cutoff. Конвейеру нужны свежие новости,
        а всплеск в тысячи скачиваний на новом аккаунте — риск для самого аккаунта."""
        cur = self.conn.execute(
            f"""UPDATE raw_media SET status='skipped', error=?, updated_at={_now()}
                WHERE status IN ('pending','failed')
                  AND raw_post_id IN (SELECT id FROM raw_posts WHERE published_at < ?)""",
            (reason, cutoff_iso),
        )
        n = cur.rowcount
        if n:
            self.conn.execute(
                """UPDATE raw_posts SET media_state='skipped'
                   WHERE published_at < ? AND media_state IN ('pending','failed')""",
                (cutoff_iso,),
            )
        return n

    def purge_filtered_media(self) -> int:
        """Удаляет с диска медиа постов, не прошедших фильтр города (решение ревью §17).

        Такой пост драфтом не станет никогда — его картинка только занимает место.
        Сам пост остаётся: тексты отфильтрованного нужны для калибровки dedup
        и relevance в фазах 3-4 (QUESTIONS §3)."""
        rows = self.conn.execute(
            """SELECT m.id, m.file_path FROM raw_media m
               JOIN raw_posts p ON p.id = m.raw_post_id
               WHERE p.filtered_out = 1 AND m.status = 'done'"""
        ).fetchall()
        removed = 0
        for row in rows:
            if row["file_path"]:
                Path(row["file_path"]).unlink(missing_ok=True)   # файл могли убрать руками
            self.conn.execute(
                f"""UPDATE raw_media SET status='skipped', file_path=NULL, file_size=NULL,
                        error='медиа отфильтрованного поста удалено (ретеншн §17)',
                        updated_at={_now()}
                    WHERE id=?""",
                (row["id"],),
            )
            removed += 1
        return removed

    def mark_media_downloaded(self, media_id: int, path: str, size: int, sha256: str, mime: str | None) -> None:
        self.conn.execute(
            f"""UPDATE raw_media SET status='done', file_path=?, file_size=?, sha256=?, mime_type=?,
                error=NULL, updated_at={_now()} WHERE id=?""",
            (path, size, sha256, mime, media_id),
        )
        self._refresh_post_media_state(media_id)

    def mark_media_failed(self, media_id: int, error: str) -> None:
        self.conn.execute(
            f"""UPDATE raw_media SET status='failed', attempts=attempts+1, error=?,
                updated_at={_now()} WHERE id=?""",
            (error[:500], media_id),
        )
        self._refresh_post_media_state(media_id)

    def mark_media_skipped(self, media_id: int, reason: str) -> None:
        self.conn.execute(
            f"UPDATE raw_media SET status='skipped', error=?, updated_at={_now()} WHERE id=?",
            (reason[:500], media_id),
        )
        self._refresh_post_media_state(media_id)

    def _refresh_post_media_state(self, media_id: int) -> None:
        """media_state поста — агрегат состояний его медиа (для алертов и фазы 4)."""
        row = self.conn.execute(
            """
            SELECT m2.raw_post_id AS pid,
                   SUM(m2.status='done')                                   AS done,
                   SUM(m2.status IN ('pending') OR
                       (m2.status='failed' AND m2.attempts < 3))           AS waiting,
                   COUNT(*)                                                AS total
            FROM raw_media m2
            WHERE m2.raw_post_id = (SELECT raw_post_id FROM raw_media WHERE id=?)
            GROUP BY m2.raw_post_id
            """,
            (media_id,),
        ).fetchone()
        if row is None:
            return
        if row["waiting"]:
            state = "pending"
        elif row["done"] == row["total"]:
            state = "done"
        elif row["done"]:
            state = "partial"
        else:
            state = "failed"
        self.conn.execute("UPDATE raw_posts SET media_state=? WHERE id=?", (state, row["pid"]))

    # ---------- warmup ----------

    def ensure_warmup_target(self, source_id: int, target: str, kind: str = "username") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO warmup_state (source_id, target, kind) VALUES (?,?,?)",
            (source_id, target, kind),
        )

    def warmup_pending(self, limit: int = 100, include_invites: bool = True) -> list[sqlite3.Row]:
        """Очередь прогрева. Инвайты идут ВПЕРЕМЕШКУ с публичными подписками
        (решение ревью §15): пачка вступлений по инвайтам подряд — заметный
        для антиспама паттерн, поэтому внутри каждого вида держим свой порядок
        по приоритету, а наружу выдаём их чередуя.

        include_invites=False — карантин свежей сессии: сутки после перелогина
        вступаем только в публичные каналы."""
        kinds = "" if include_invites else " AND w.kind = 'username'"
        return self.conn.execute(
            f"""
            SELECT * FROM (
                SELECT w.*, s.city, s.priority,
                       ROW_NUMBER() OVER (
                           PARTITION BY w.kind
                           ORDER BY CASE s.priority WHEN 'high' THEN 0 WHEN 'mid' THEN 1 ELSE 2 END,
                                    w.id
                       ) AS rn
                FROM warmup_state w JOIN sources s ON s.id = w.source_id
                WHERE w.status = 'pending' AND w.attempts < 3{kinds}
            )
            ORDER BY rn, CASE kind WHEN 'username' THEN 0 ELSE 1 END, id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def warmup_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM warmup_state GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    def mark_warmup(self, source_id: int, status: str, error: str | None = None,
                    max_attempts: int = 3) -> None:
        """Ошибка возвращает цель в очередь, пока не выбраны попытки, — сеть моргает,
        канал может быть временно недоступен. После лимита цель окончательно failed."""
        if status == "failed":
            self.conn.execute(
                f"""UPDATE warmup_state SET attempts = attempts + 1, last_error = ?,
                        status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END,
                        updated_at = {_now()}
                    WHERE source_id = ?""",
                (error[:500] if error else None, max_attempts, source_id),
            )
            return

        joined = f", joined_at={_now()}" if status in ("joined", "already_member") else ""
        self.conn.execute(
            f"UPDATE warmup_state SET status=?, last_error=?{joined}, updated_at={_now()} WHERE source_id=?",
            (status, error[:500] if error else None, source_id),
        )
        if status in ("joined", "already_member"):
            self.mark_source_joined(source_id)

    def mark_warmup_dead(self, source_id: int, error: str) -> None:
        """Цель окончательно недостижима (мёртвые инвайты, §15): из очереди выбывает,
        ретраев больше не будет."""
        self.conn.execute(
            f"""UPDATE warmup_state SET status='dead', last_error=?, updated_at={_now()}
                WHERE source_id=?""",
            (error[:500], source_id),
        )
        self.conn.execute("UPDATE sources SET status='dead', updated_at=" + _now() + " WHERE id=?",
                          (source_id,))

    def warmup_day(self, day: str, quota_chooser) -> sqlite3.Row:
        """План на день. Квота выбирается один раз и хранится — рестарт её не пересматривает."""
        row = self.conn.execute("SELECT * FROM warmup_days WHERE day=?", (day,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO warmup_days (day, quota) VALUES (?,?)", (day, quota_chooser())
            )
            row = self.conn.execute("SELECT * FROM warmup_days WHERE day=?", (day,)).fetchone()
        return row

    def bump_warmup_day(self, day: str) -> None:
        self.conn.execute("UPDATE warmup_days SET done = done + 1 WHERE day=?", (day,))
