"""Плавный прогрев юзербота: подписка на источники из sources.yaml.

Зачем медленно: свежий аккаунт, подписавшийся на 40 каналов за час, — типовой
паттерн для антиспама Telegram. Поэтому 5-10 подписок в день, случайные паузы,
только в человеческие часы. Состояние — в БД, так что рестарт скрипта не сбрасывает
план и не устраивает второй заход за день.

Юзербот при этом остаётся read-only: JoinChannel — единственное, что здесь пишется
в Telegram, и это условие чтения источника (см. collector/readonly.py).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telethon.errors import (
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    RPCError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from tgtraff.config import Config, SourceSpec, WarmupSettings
from tgtraff.db import Database

log = logging.getLogger("tgtraff.warmup")

# Сколько ждать после перелогина, прежде чем вступать по инвайтам (решение ревью §15):
# свежая сессия плюс пачка ImportChatInvite — типовой профиль спамера.
INVITE_QUARANTINE_HOURS = 24
SESSION_CREATED_KEY = "session_created_at"

_INVITE = re.compile(r"(?:https?://)?t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)", re.I)


def invite_hash(url: str | None) -> str | None:
    """Хэш приглашения из ссылки любого вида. Публичный t.me/name — не инвайт."""
    if not url:
        return None
    match = _INVITE.match(url.strip())
    return match.group(1) if match else None

# Максимальный шаг сна. Прогрев планируется по стенным часам («завтра в 10:00»),
# а asyncio.sleep отмеряет монотонное время, которое на macOS стоит, пока машина спит.
# Один длинный sleep из-за этого промахивается на сутки, поэтому спим короткими
# шагами и каждый раз сверяемся с календарём.
SLEEP_STEP_SECONDS = 60


async def sleep_until(deadline, *, clock=None, sleep=None, step: float = SLEEP_STEP_SECONDS) -> None:
    """Ждать до момента по стенным часам, переживая засыпание машины."""
    clock = clock or (lambda: datetime.now(deadline.tzinfo))
    sleep = sleep or asyncio.sleep
    while True:
        remaining = (deadline - clock()).total_seconds()
        if remaining <= 0:
            return
        await sleep(min(step, remaining))


class WarmupPlanner:
    """Решает: сколько сегодня, кого следующим, сколько ждать."""

    def __init__(self, db: Database, settings: WarmupSettings, rng: random.Random | None = None):
        self.db = db
        self.s = settings
        self.rng = rng or random.Random()
        self.tz = ZoneInfo(settings.tz)

    # ---- план дня ----

    def daily_quota(self) -> int:
        return self.rng.randint(self.s.per_day_min, self.s.per_day_max)

    def today(self, now: datetime | None = None) -> str:
        return (now or datetime.now(self.tz)).astimezone(self.tz).strftime("%Y-%m-%d")

    def day_plan(self, day: str) -> sqlite3.Row:
        return self.db.warmup_day(day, self.daily_quota)

    def remaining_today(self, day: str) -> int:
        plan = self.day_plan(day)
        return max(0, plan["quota"] - plan["done"])

    def next_target(self, day: str) -> sqlite3.Row | None:
        if self.remaining_today(day) <= 0:
            return None
        pending = self.db.warmup_pending(limit=1, include_invites=self.invites_allowed())
        return pending[0] if pending else None

    # ---- карантин свежей сессии (§15) ----

    def invites_open_at(self) -> datetime | None:
        """Когда можно начинать вступать по инвайтам. None — отметки о перелогине
        нет, значит сессия старая и ждать нечего."""
        created = self.db.get_meta(SESSION_CREATED_KEY)
        if not created:
            return None
        return datetime.fromisoformat(created) + timedelta(hours=INVITE_QUARANTINE_HOURS)

    def invites_allowed(self, now: datetime | None = None) -> bool:
        opens = self.invites_open_at()
        return opens is None or (now or datetime.now(timezone.utc)) >= opens

    def invites_open_in_hours(self, now: datetime | None = None) -> float:
        opens = self.invites_open_at()
        if opens is None:
            return 0.0
        left = (opens - (now or datetime.now(timezone.utc))).total_seconds() / 3600
        return max(0.0, left)

    # ---- очередь целей ----

    def enqueue(self, specs: list[SourceSpec]) -> int:
        """Заводит источники в БД и ставит в очередь прогрева две категории:
        публичные каналы с username и приватные, к которым есть инвайт (§15).
        Запись без того и другого вступить не даёт — в очередь не идёт."""
        queued = 0
        for spec in specs:
            source_id = self.db.upsert_source(spec)
            if spec.is_collectable:
                self.db.ensure_warmup_target(source_id, spec.key, kind="username")
                queued += 1
            elif spec.invites:
                self.db.ensure_warmup_target(source_id, spec.invites[0], kind="invite")
                queued += 1
        return queued

    # ---- время ----

    def in_human_hours(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        return self.s.hour_from <= local.hour < self.s.hour_to

    def window_opens_at(self, now: datetime) -> datetime:
        """Ближайший момент, когда окно открыто (сам now, если открыто уже сейчас)."""
        local = now.astimezone(self.tz)
        if self.in_human_hours(local):
            return local
        target = local.replace(hour=self.s.hour_from, minute=0, second=0, microsecond=0)
        if local.hour >= self.s.hour_to:
            target += timedelta(days=1)
        return target

    def next_window_open(self, now: datetime) -> datetime:
        """Начало завтрашнего окна — когда дневная квота уже выбрана."""
        local = now.astimezone(self.tz)
        return (local + timedelta(days=1)).replace(
            hour=self.s.hour_from, minute=0, second=0, microsecond=0)

    def seconds_until_window(self, now: datetime) -> float:
        """0, если сейчас можно; иначе — сколько спать до ближайшего окна."""
        return max(0.0, (self.window_opens_at(now) - now.astimezone(self.tz)).total_seconds())

    def next_gap_seconds(self) -> int:
        return self.rng.randint(self.s.gap_minutes_min * 60, self.s.gap_minutes_max * 60)


async def run_warmup(client, db: Database, cfg: Config, planner: WarmupPlanner | None = None,
                     once: bool = False) -> None:
    """Основной цикл прогрева. Запускать как долгоживущий процесс (или раз в день с --once)."""
    planner = planner or WarmupPlanner(db, cfg.warmup)

    specs = [s for city in cfg.active() for s in (*city.tg_sources, *city.tg_regional)]
    queued = planner.enqueue(specs)
    # Запасные инвайты живут в конфиге, а не в БД: связываем цель с её спекой.
    specs_by_source = {planner.db.upsert_source(s): s for s in specs if s.invites}
    if not planner.invites_allowed():
        log.info("инвайты в карантине после перелогина — откроются через %.1f ч",
                 planner.invites_open_in_hours())
    counts = db.warmup_counts()
    log.info("цели прогрева: %s в очереди (всего в конфиге %s); состояние: %s",
             queued, len(specs), counts or "пусто")

    while True:
        now = datetime.now(planner.tz)
        day = planner.today(now)

        opens_at = planner.window_opens_at(now)
        if opens_at > now:
            log.info("вне человеческих часов (%02d:00-%02d:00 %s) — ждём до %s",
                     cfg.warmup.hour_from, cfg.warmup.hour_to, cfg.warmup.tz,
                     opens_at.strftime("%d.%m %H:%M"))
            if once:
                return
            await sleep_until(opens_at)
            continue

        target = planner.next_target(day)
        if target is None:
            if not db.warmup_pending():
                log.info("прогрев завершён: все доступные источники обработаны. Итог: %s",
                         db.warmup_counts())
                return
            # дневная квота выбрана — ждём завтрашнего окна
            tomorrow = planner.next_window_open(now)
            log.info("дневная квота (%s) выбрана, следующая порция %s",
                     planner.day_plan(day)["quota"], tomorrow.strftime("%d.%m в %H:%M"))
            if once:
                return
            await sleep_until(tomorrow)
            continue

        await _join_one(client, db, target, day, planner, specs_by_source)

        if once:
            return
        gap = planner.next_gap_seconds()
        log.info("следующая подписка через %d мин", gap // 60)
        await sleep_until(datetime.now(planner.tz) + timedelta(seconds=gap))


async def _join_one(client, db: Database, target, day: str, planner: WarmupPlanner,
                    specs_by_source: dict | None = None) -> None:
    """Одно вступление: публичный канал по username или приватный по инвайту (§15)."""
    plan = planner.day_plan(day)
    marker = f"[{plan['done'] + 1}/{plan['quota']} за {day}]"

    if target["kind"] == "invite":
        spec = (specs_by_source or {}).get(target["source_id"])
        links = spec.invites if spec else [target["target"]]
        await _join_by_invite(client, db, target, day, planner, marker, links)
        return

    await _join_public(client, db, target, day, planner, marker)


async def _join_public(client, db: Database, target, day: str, planner: WarmupPlanner,
                       marker: str) -> None:
    username = target["target"]
    source_id = target["source_id"]
    try:
        entity = await client.get_entity(username)
        if getattr(entity, "left", True) is False:
            db.mark_warmup(source_id, "already_member")
            db.bump_warmup_day(day)
            log.info("%s @%s — уже подписаны", marker, username)
            return

        await client(JoinChannelRequest(entity))
        db.set_source_peer(source_id, getattr(entity, "id", None))
        db.mark_warmup(source_id, "joined")
        db.bump_warmup_day(day)
        log.info("%s @%s — подписались", marker, username)

    except FloodWaitError as e:
        db.mark_warmup(source_id, "failed", f"FloodWait {e.seconds}s")
        log.warning("FloodWait %s c на @%s — прогрев на паузе до конца ожидания", e.seconds, username)
        await sleep_until(datetime.now(planner.tz) + timedelta(seconds=e.seconds + 5))
    except (RPCError, ValueError, TypeError) as e:
        db.mark_warmup(source_id, "failed", f"{type(e).__name__}: {e}")
        log.warning("@%s — не вышло: %s: %s", username, type(e).__name__, e)


async def _join_by_invite(client, db: Database, target, day: str, planner: WarmupPlanner,
                          marker: str, links: list[str]) -> None:
    """Вступление в приватный канал. Протух основной инвайт — пробуем запасной,
    не сработал ни один — источник помечается dead (решение ревью §15):
    бесконечно долбиться в мёртвую ссылку смысла нет, а для антиспама вредно."""
    source_id = target["source_id"]
    row = db.get_source(source_id)
    label = (row["query"] if row else None) or target["target"]
    last_error = None

    for link in links or []:
        h = invite_hash(link)
        if not h:
            last_error = f"не похоже на инвайт: {link}"
            continue
        try:
            result = await client(ImportChatInviteRequest(h))
            chats = getattr(result, "chats", None) or []
            if chats:
                db.set_source_peer(source_id, getattr(chats[0], "id", None))
            db.mark_warmup(source_id, "joined")
            db.bump_warmup_day(day)
            log.info("%s «%s» — вступили по инвайту", marker, label)
            return
        except UserAlreadyParticipantError:
            db.mark_warmup(source_id, "already_member")
            db.bump_warmup_day(day)
            log.info("%s «%s» — уже состоим", marker, label)
            return
        except InviteRequestSentError:
            # Канал принимает по заявке: она уже отправлена и ждёт одобрения админа.
            # Второй инвайт дёргать нельзя — уйдёт вторая заявка, а это ровно тот
            # назойливый паттерн, от которого мы бережём молодой аккаунт.
            db.mark_warmup(source_id, "requested", "заявка отправлена, ждёт одобрения")
            db.bump_warmup_day(day)
            log.info("%s «%s» — заявка на вступление отправлена, ждём одобрения", marker, label)
            return
        except FloodWaitError as e:
            db.mark_warmup(source_id, "failed", f"FloodWait {e.seconds}s")
            log.warning("FloodWait %s c на инвайте «%s» — пауза", e.seconds, label)
            await sleep_until(datetime.now(planner.tz) + timedelta(seconds=e.seconds + 5))
            return
        except (InviteHashExpiredError, InviteHashInvalidError) as e:
            last_error = f"{type(e).__name__}"
            log.warning("инвайт «%s» не сработал (%s) — пробую следующий", label, type(e).__name__)
        except (RPCError, ValueError, TypeError) as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning("инвайт «%s»: %s: %s", label, type(e).__name__, e)

    db.mark_warmup_dead(source_id, last_error or "инвайты не сработали")
    log.warning("«%s» — ни один инвайт не сработал (%s): источник помечен dead", label, last_error)
