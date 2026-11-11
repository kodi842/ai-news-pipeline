"""Решения модератора над драфтом (спека §7) и правило §28.

Кнопки админ-бота — тонкая обвязка над этими функциями: вся логика решений
живёт здесь, поэтому её видно тестами без Telegram.

Состояния драфта:
    new → approved → published        одобрен и вышел
    new → rejected                    отклонён с причиной
    new → deferred → new              отложен, вернётся по /queue
    new|deferred → stale              протух без решения (§55): в канал не идёт
    rejected|stale → superseded       §28: кластер предложен заново

Правило §28 (решение ревью фазы 3): «не новость» — кластер молчит навсегда;
`low_facts` — драфт предлагается повторно, но только при ЗАМЕТНОМ приросте
фактуры: первое медиа или +2 источника. Поэтому при отклонении снимается
снимок кластера (`reoffer_size`, `reoffer_media`) — сравнивать иначе не с чем.

Протухание (решение ревью §55): новость города старше суток мертва. Драфт
без решения 24 ч (молния — 6 ч) уходит в `stale`. Это НЕ отклонение: кластер
не блокируется — снимок фактуры снимается так же, как при «мало фактуры»,
и §28 вернёт новость, если придёт заметная фактура.

Пуш карточкой — только срочное: молния, UGC, safety=manual_only. Остальное
копится и выдаётся по /queue свежими вперёд — модератор тянет очередь, когда
готов, а не тонет в пуше.

Автопубликация (решение владельца §59) — единственный путь в канал мимо
человека, и он узкий: шесть условий разом, любое сомнение возвращает драфт
модератору. Выключается пустым списком категорий, без правки кода.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from tgtraff import drafts as drafts_store
from tgtraff.publisher import store

log = logging.getLogger("tgtraff.moderation")

NOT_NEWS, LOW_FACTS = "not_news", "low_facts"
REJECT_REASONS = {
    NOT_NEWS: "не новость",
    LOW_FACTS: "мало фактуры",
}

# §28: что считается заметным приростом фактуры.
REOFFER_MIN_NEW_SOURCES = 2

# §55: сроки протухания без решения, часов.
STALE_HOURS, STALE_FLASH_HOURS = 24, 6
# §55: что пушится карточкой сразу; остальное ждёт /queue.
PUSH_SQL = ("(urgency = 'flash' OR COALESCE(origin,'news') = 'ugc' "
            "OR safety_verdict = 'manual_only')")
WAITING_SQL = "status IN ('new','deferred')"

# §59: флаги сомнения. Любой из них снимает автопубликацию — выдумка, нехватка
# фактуры и непроверенное медиа требуют глаз.
AUTOPUBLISH_BLOCKING_FLAGS = ("low_facts", "unsupported_facts", "media_unverified")
# Насколько далеко вперёд автопост согласен ждать своего слота. Дальше —
# новость мертва (§55), и решение возвращается модератору.
AUTOPUBLISH_HORIZON_HOURS = 12
# Сколько кандидатов разбираем за тик.
AUTOPUBLISH_PER_TICK = 20

NOW_SQL = drafts_store.NOW_SQL


@dataclass(frozen=True)
class ApproveResult:
    draft_id: int
    queue_id: int
    scheduled_at: datetime
    immediate: bool


@dataclass(frozen=True)
class SourceLink:
    """Источник кластера и ссылка на его публикацию (решение ревью §61.2).

    `private` — приватный ТГ-канал: внутренняя форма t.me/c/<peer>/<msg>
    откроется только тому, кто в канал вступил, и карточка об этом
    предупреждает. `url = None` — ссылки нет, остаётся простое имя."""

    name: str
    url: str | None = None
    private: bool = False


def source_links(db, cluster_id: int | None, limit: int = 8) -> list[SourceLink]:
    """Источники кластера со ссылками на исходные публикации (§61.2).

    Модератор сверяет фактуру по первоисточнику, и до §61 путь туда был
    ручной. Все данные уже лежат в `raw_posts`: ТГ и ВК коллектор пишет
    готовый url, RSS — адрес записи. Пусто у приватного ТГ-канала: username'а
    нет, зато есть разрешённый peer — из него собирается t.me/c/<peer>/<msg>.

    Источник, опубликовавший в кластере несколько раз, показывается один раз —
    по ПЕРВОЙ публикации: она и есть первоисточник новости."""
    if not cluster_id:
        return []                                 # UGC и сервисные посты — не из кластера
    rows = db.conn.execute(
        """SELECT s.key, s.tg_peer_id, p.url, p.external_id, p.source_type
           FROM cluster_members m
           JOIN raw_posts p ON p.id = m.raw_post_id
           JOIN sources s ON s.id = p.source_id
           WHERE m.cluster_id = ?
           ORDER BY p.published_at, p.id""", (cluster_id,)).fetchall()
    out: list[SourceLink] = []
    seen: set[str] = set()
    for r in rows:
        name = _source_name(r["key"])
        if name in seen:
            continue
        seen.add(name)
        out.append(_link(name, r))
        if len(out) >= limit:
            break
    return out


def _link(name: str, row) -> SourceLink:
    if row["url"]:
        return SourceLink(name, row["url"])
    if row["source_type"] in ("tg", "tg_regional") and row["tg_peer_id"]:
        # Приватный канал: публичного адреса нет, внутренняя форма работает
        # только для участников — так карточка и напишет.
        return SourceLink(name, f"https://t.me/c/{row['tg_peer_id']}/{row['external_id']}",
                          private=True)
    return SourceLink(name, None, False)


def _source_name(key: str) -> str:
    """Неразрешённый источник лежит под ключом `?<поисковый запрос>` (db.upsert_source) —
    вопросительный знак служебный, модератору его показывать незачем."""
    return key[1:] if key.startswith("?") else key


# ---------- очередь модератора ----------

def unseen(db, limit: int = 10, city: str | None = None,
           urgent_only: bool = False) -> list[sqlite3.Row]:
    """Драфты, которых модератор ещё не видел (карточка не отправлялась).

    `urgent_only` — правило пуша §55: молния, UGC и manual_only; остальное
    копится в пуле и ждёт /queue."""
    where, params = ["status = 'new'", "card_sent_at IS NULL"], []
    if urgent_only:
        where.append(PUSH_SQL)
    if city:
        where.append("city = ?")
        params.append(city)
    return db.conn.execute(
        f"""SELECT * FROM drafts WHERE {' AND '.join(where)}
            ORDER BY CASE urgency WHEN 'flash' THEN 0 ELSE 1 END, created_at, id
            LIMIT ?""", (*params, limit)).fetchall()


def pool(db, limit: int = 10) -> list[sqlite3.Row]:
    """Пул §55: всё, что ждёт решения и не лежит сейчас карточкой в чате —
    не показанные драфты и отложенные. СВЕЖИЕ ВПЕРЁД: старая новость города
    протухнет раньше, чем до неё дойдут руки."""
    return db.conn.execute(
        """SELECT * FROM drafts
           WHERE (status = 'new' AND card_sent_at IS NULL) OR status = 'deferred'
           ORDER BY created_at DESC, id DESC LIMIT ?""", (limit,)).fetchall()


def queue_summary(db, now: datetime | None = None) -> dict:
    """Сводка для /queue без аргумента: сколько ждёт по городам и категориям,
    сколько из этого уже висит карточками и сколько отложено.

    Отдельной строкой — автопосты за сутки (§59): их модератор не видел, и это
    единственное место, где он замечает их до дайджеста."""
    rows = db.conn.execute(
        f"""SELECT city, COALESCE(category, 'other') category, status,
                   (card_sent_at IS NOT NULL) on_card, COUNT(*) c
            FROM drafts WHERE {WAITING_SQL}
            GROUP BY city, category, status, on_card""").fetchall()
    cities: dict[str, dict] = {}
    total = on_cards = deferred = 0
    for r in rows:
        city = cities.setdefault(r["city"], {"total": 0, "categories": {}})
        city["total"] += r["c"]
        city["categories"][r["category"]] = city["categories"].get(r["category"], 0) + r["c"]
        total += r["c"]
        if r["status"] == "deferred":
            deferred += r["c"]
        elif r["on_card"]:
            on_cards += r["c"]
    day_ago = store.iso((now or datetime.now(timezone.utc)) - timedelta(hours=24))
    return {"total": total, "on_cards": on_cards, "deferred": deferred,
            "pool": total - on_cards, "cities": cities,
            "auto_24h": autopublished_since(db, day_ago)}


def urgent_waiting_count(db) -> int:
    """Сколько СРОЧНОГО ждёт решения: молния, UGC, safety=manual_only (§55).

    Это и есть «модератор не справляется» из §57. Пул несрочного в счёт не
    идёт: после §55 он большой by design — модератор тянет его сам по /queue,
    и алерт на его размер будил человека каждые 15 минут новым числом."""
    return db.conn.execute(
        f"SELECT COUNT(*) c FROM drafts WHERE {WAITING_SQL} AND {PUSH_SQL}").fetchone()["c"]


def pending(db, city: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    """Всё, что ждёт решения: и показанное, и отложенное."""
    where, params = ["status IN ('new','deferred')"], []
    if city:
        where.append("city = ?")
        params.append(city)
    return db.conn.execute(
        f"""SELECT * FROM drafts WHERE {' AND '.join(where)}
            ORDER BY CASE urgency WHEN 'flash' THEN 0 ELSE 1 END, created_at, id
            LIMIT ?""", (*params, limit)).fetchall()


def deferred(db, limit: int = 50) -> list[sqlite3.Row]:
    return db.conn.execute(
        "SELECT * FROM drafts WHERE status='deferred' ORDER BY created_at, id LIMIT ?",
        (limit,)).fetchall()


def counts(db) -> dict[str, int]:
    rows = db.conn.execute("SELECT status, COUNT(*) c FROM drafts GROUP BY status")
    return {r["status"]: r["c"] for r in rows}


def mark_card(db, draft_id: int, chat_id: int, message_id: int) -> None:
    db.conn.execute(
        f"""UPDATE drafts SET card_chat_id=?, card_message_id=?, card_sent_at={NOW_SQL},
                updated_at={NOW_SQL} WHERE id=?""", (chat_id, message_id, draft_id))


# ---------- решения ----------

def approve(db, draft_id: int, moderator_id: int, planner, now: datetime | None = None,
            immediate: bool = False) -> ApproveResult:
    """«Опубликовать»: драфт получает слот и ложится в очередь публикации.

    `immediate` — кнопка «вне очереди, сейчас»: обходит сетку слотов, но НЕ
    переписывает драфт в молнию. Срочность — свойство новости и видна в посте
    префиксом ⚡️; решение модератора о времени текст поста менять не должно.

    Автопубликации в конвейере нет ни при каком вердикте safety — сюда попадают
    только те драфты, по которым модератор нажал кнопку (спека §6, §8)."""
    row = _require(db, draft_id)
    urgency = row["urgency"] or "normal"
    when = planner.plan(db, row["city"], urgency="flash" if immediate else urgency, now=now)
    queue_id = store.enqueue(db, draft_id, row["city"], when, urgency=urgency,
                             moderator_id=moderator_id)
    db.conn.execute(
        f"""UPDATE drafts SET status='approved', moderator_id=?, decided_at={NOW_SQL},
                updated_at={NOW_SQL} WHERE id=?""", (moderator_id, draft_id))
    log.info("драфт #%s одобрен модератором %s на %s", draft_id, moderator_id, when)
    return ApproveResult(draft_id, queue_id, when,
                         immediate=(immediate or urgency == "flash"))


# ---------- §59: автопубликация нейтральной новости ----------

def may_autopublish(row, categories) -> bool:
    """Шесть условий §59. Пост уходит в канал без человека, только если все
    шесть выполнены сразу:

      1) safety = safe;
      2) категория в списке (пустой список = режим выключен целиком);
      3) нет флагов сомнения (мало фактуры, выдумка, непроверенное медиа);
      4) origin = news — UGC всегда руками;
      5) urgency = normal — молнии руками;
      6) обычные слоты с паузами — это уже дело планировщика, см. `autopublish`.
    """
    if not categories:
        return False
    flags = set(drafts_store.decode_list(row["flags"]))
    return bool(
        row["safety_verdict"] == "safe"
        and (row["category"] or "other") in categories
        and not flags & set(AUTOPUBLISH_BLOCKING_FLAGS)
        and (row["origin"] or "news") == "news"
        and (row["urgency"] or "normal") == "normal")


def autopublish_candidates(db, categories, limit: int = AUTOPUBLISH_PER_TICK) -> list[sqlite3.Row]:
    """Драфты, которым §59 разрешает выйти без модератора.

    Только те, что модератор ещё не видел: карточка в чате (или «отложено»)
    значит, что решение уже за человеком — за его спиной пост не выходит.
    Свежие вперёд, как и пул §55: старая новость городу не нужна."""
    if not categories:
        return []                                 # kill-switch: режима нет
    marks = ",".join("?" * len(categories))
    rows = db.conn.execute(
        f"""SELECT * FROM drafts
            WHERE status='new' AND card_sent_at IS NULL AND autopublished_at IS NULL
              AND safety_verdict='safe' AND COALESCE(origin,'news')='news'
              AND COALESCE(urgency,'normal')='normal'
              AND COALESCE(category,'other') IN ({marks})
            ORDER BY created_at DESC, id DESC LIMIT ?""",
        (*categories, limit)).fetchall()
    # Флаги лежат JSON-строкой — их фильтрует Python, а не SQL.
    return [r for r in rows if may_autopublish(r, categories)]


def autopublish(db, draft_id: int, planner, now: datetime | None = None,
                horizon_hours: int = AUTOPUBLISH_HORIZON_HOURS) -> ApproveResult | None:
    """Ставит драфт в очередь без модератора (§59).

    Слот — обычный, с паузами: автопост не лезет вне очереди. Если ближайший
    свободный слот дальше горизонта (дневной потолок города выбран, и очередь
    уехала на завтра), автопост отменяется: сутками позже новость мертва (§55),
    и решать это должен человек. Драфт остаётся в пуле нетронутым."""
    row = _require(db, draft_id)
    now = now or datetime.now(timezone.utc)
    when = planner.plan(db, row["city"], urgency="normal", now=now)
    if when - now > timedelta(hours=horizon_hours):
        log.info("автопост драфта #%s отменён: ближайший слот %s — дальше %s ч",
                 draft_id, when, horizon_hours)
        return None
    queue_id = store.enqueue(db, draft_id, row["city"], when, urgency="normal")
    db.conn.execute(
        f"""UPDATE drafts SET status='approved', moderator_id=NULL, decided_at={NOW_SQL},
                autopublished_at={NOW_SQL}, updated_at={NOW_SQL} WHERE id=?""", (draft_id,))
    log.info("драфт #%s автопубликуется (§59): %s/%s на %s", draft_id, row["city"],
             row["category"], when)
    return ApproveResult(draft_id, queue_id, when, immediate=False)


def autopublished_since(db, since: str) -> int:
    """Сколько постов вышло без человека — счётчик дайджеста и сводки /queue."""
    return db.conn.execute(
        "SELECT COUNT(*) c FROM drafts WHERE autopublished_at >= ?", (since,)).fetchone()["c"]


def reject(db, draft_id: int, moderator_id: int, reason: str) -> None:
    """«Отклонить» с причиной. Причина определяет судьбу кластера (§28)."""
    if reason not in REJECT_REASONS:
        raise ValueError(f"причина отклонения должна быть одной из {sorted(REJECT_REASONS)}")
    row = _require(db, draft_id)
    store.cancel_for_draft(db, draft_id)
    size, media = cluster_facts(db, row["cluster_id"])
    db.conn.execute(
        f"""UPDATE drafts SET status='rejected', reject_reason=?, moderator_id=?,
                decided_at={NOW_SQL}, reoffer_size=?, reoffer_media=?, updated_at={NOW_SQL}
            WHERE id=?""", (reason, moderator_id, size, media, draft_id))
    log.info("драфт #%s отклонён (%s) модератором %s", draft_id, reason, moderator_id)


def defer(db, draft_id: int, moderator_id: int) -> None:
    """«Отложить»: решение не принято, карточка уходит из стопки до /queue."""
    _require(db, draft_id)
    db.conn.execute(
        f"""UPDATE drafts SET status='deferred', moderator_id=?, updated_at={NOW_SQL}
            WHERE id=?""", (moderator_id, draft_id))


def revive(db, draft_id: int) -> None:
    """Отложенный драфт возвращается в стопку — карточка покажется заново."""
    db.conn.execute(
        f"""UPDATE drafts SET status='new', card_sent_at=NULL, updated_at={NOW_SQL}
            WHERE id=? AND status='deferred'""", (draft_id,))


def edit(db, draft_id: int, text: str, moderator_id: int) -> None:
    """«Править»: ответное сообщение модератора заменяет текст драфта.

    Первая строка — хук, остальное — тело (ровно так карточка и показана).
    Одна строка меняет только хук: тело трогать не просили."""
    row = _require(db, draft_id)
    head, _, rest = text.strip().partition("\n")
    title = head.strip() or row["title"]
    body = rest.strip() or row["body"]
    note = _append_note(row["notes"], f"правка модератора {moderator_id}")
    db.conn.execute(
        f"""UPDATE drafts SET title=?, body=?, notes=?, updated_at={NOW_SQL} WHERE id=?""",
        (title, body, note, draft_id))


def mark_published(db, draft_id: int) -> None:
    db.conn.execute(
        f"UPDATE drafts SET status='published', updated_at={NOW_SQL} WHERE id=?", (draft_id,))


# ---------- §55: протухание ----------

def expiring(db, now: datetime | None = None, hours: int = STALE_HOURS,
             flash_hours: int = STALE_FLASH_HOURS, limit: int | None = None) -> list[sqlite3.Row]:
    """Драфты, которые ждут решения дольше срока: normal — `hours`, молния —
    `flash_hours`. Только выборка, база не меняется."""
    now = now or datetime.now(timezone.utc)
    cutoff = store.iso(now - timedelta(hours=hours))
    flash_cutoff = store.iso(now - timedelta(hours=flash_hours))
    return db.conn.execute(
        f"""SELECT * FROM drafts WHERE {WAITING_SQL}
              AND created_at < CASE urgency WHEN 'flash' THEN ? ELSE ? END
            ORDER BY created_at, id LIMIT ?""",
        (flash_cutoff, cutoff, -1 if limit is None else limit)).fetchall()


def expire(db, now: datetime | None = None, hours: int = STALE_HOURS,
           flash_hours: int = STALE_FLASH_HOURS, limit: int | None = None) -> list[sqlite3.Row]:
    """Драфты без решения дольше срока уходят в `stale`.

    Возвращает строки протухших такими, какими они были ДО правки, — с
    координатами карточек: бот перепишет их в «⏳ протух». Снимок кластера
    снимается, как при отклонении за нехваткой фактуры: §28 вернёт новость,
    если фактура придёт."""
    out = []
    for row in expiring(db, now=now, hours=hours, flash_hours=flash_hours, limit=limit):
        size, media = cluster_facts(db, row["cluster_id"])
        cur = db.conn.execute(
            f"""UPDATE drafts SET status='stale', decided_at={NOW_SQL},
                    reoffer_size=?, reoffer_media=?, updated_at={NOW_SQL}
                WHERE id=? AND {WAITING_SQL}""", (size, media, row["id"]))
        if cur.rowcount:                          # иначе решили за секунду до нас
            out.append(row)
    if out:
        log.info("протухло без решения: %s драфтов (%s)", len(out),
                 ", ".join(f"#{r['id']}" for r in out[:10]))
    return out


# ---------- §28: повторное предложение ----------

def cluster_facts(db, cluster_id: int | None) -> tuple[int, int]:
    """Сколько в кластере источников и скачанных медиа — снимок для §28."""
    if not cluster_id:
        return (0, 0)
    size = db.conn.execute(
        "SELECT COUNT(*) c FROM cluster_members WHERE cluster_id=?", (cluster_id,)
    ).fetchone()["c"]
    media = db.conn.execute(
        """SELECT COUNT(*) c FROM raw_media m
           JOIN post_heads ph ON ph.raw_post_id = m.raw_post_id
           JOIN cluster_members cm ON cm.raw_post_id = ph.head_id
           WHERE cm.cluster_id=? AND m.status='done'""", (cluster_id,)).fetchone()["c"]
    return (size, media)


def reoffer_candidates(db, limit: int = 50) -> list[int]:
    """Кластеры, отклонённые из-за нехватки фактуры, которым фактура пришла.

    Решение ревью §28: только причина `low_facts` и только заметный прирост —
    первое медиа или +2 источника. «Не новость» сюда не попадает никогда.
    Протухший драфт (§55) — та же судьба, что у «мало фактуры»: кластер
    не заблокирован и вернётся с фактурой."""
    rows = db.conn.execute(
        """SELECT d.id, d.cluster_id, d.reoffer_size, d.reoffer_media
           FROM drafts d
           WHERE ((d.status='rejected' AND d.reject_reason=?) OR d.status='stale')
             AND d.cluster_id IS NOT NULL
           ORDER BY d.id""", (LOW_FACTS,)).fetchall()
    out: list[int] = []
    for row in rows:
        size, media = cluster_facts(db, row["cluster_id"])
        was_size = row["reoffer_size"] or 0
        was_media = row["reoffer_media"] or 0
        grown = size - was_size >= REOFFER_MIN_NEW_SOURCES
        first_media = media > 0 and was_media == 0
        if grown or first_media:
            out.append(row["cluster_id"])
        if len(out) >= limit:
            break
    return out


def supersede(db, cluster_id: int) -> int:
    """Отклонённый драфт уступает место новому: кластер снова можно рерайтить.

    Сам рерайт делает конвейер фазы 4 — здесь только снимается замок
    «у кластера уже есть драфт»."""
    cur = db.conn.execute(
        f"""UPDATE drafts SET status='superseded', updated_at={NOW_SQL}
            WHERE cluster_id=? AND status IN ('rejected','stale')""", (cluster_id,))
    if cur.rowcount:
        db.conn.execute(
            "UPDATE clusters SET relevance=NULL, relevance_reason=NULL WHERE id=?",
            (cluster_id,))
    return cur.rowcount


# ---------- служебное ----------

def _require(db, draft_id: int) -> sqlite3.Row:
    row = drafts_store.get_draft(db, draft_id)
    if row is None:
        raise LookupError(f"драфта #{draft_id} нет в базе")
    return row


def _append_note(existing: str | None, note: str) -> str:
    return f"{existing} · {note}" if existing else note
