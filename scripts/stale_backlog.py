#!/usr/bin/env python
"""Разовая зачистка бэклога модерации (решение ревью §55, п. 4).

Драфты, ждущие решения дольше `--hours` часов (`new`/`deferred`), получают
статус `stale` («протух»): в канал не идут, из стопки и очереди уходят. Это
НЕ отклонение: кластер не блокируется — снимается снимок фактуры, и §28
вернёт новость, если придёт заметная фактура (первое медиа или +2 источника).
Карточка в админ-боте, если она была отправлена, переписывается в «⏳ протух»
и теряет кнопки.

    python scripts/stale_backlog.py --hours 24 --dry-run   # только цифры
    python scripts/stale_backlog.py --hours 24             # зачистка

Так был зачищен бэклог 2026-09-05 (docs/PROGRESS.md). Дальше протухание
делает сам админ-бот каждым тиком (normal 24 ч, молния 6 ч — settings.yaml,
блок moderation); скрипт остаётся для ручного прогона с другим порогом.
Одна и та же механика: `actions.expire` + `rewrite_card`.

Демоны не останавливаются: база в WAL, транзакции короткие. Bot API из
второго процесса — обычное дело: конфликтует только getUpdates, а здесь
только правки сообщений.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load, setup_logging  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.moderation import actions  # noqa: E402
from tgtraff.moderation.bot import EDIT_PAUSE_SEC, rewrite_card  # noqa: E402

log = logging.getLogger("tgtraff.stale_backlog")

WAITING = ("new", "deferred")


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def waiting_counts(db) -> dict[str, int]:
    rows = db.conn.execute(
        f"SELECT status, COUNT(*) c FROM drafts WHERE {actions.WAITING_SQL} GROUP BY status")
    counts = {s: 0 for s in WAITING}
    counts.update({r["status"]: r["c"] for r in rows})
    return counts


async def run(db, bot, hours: int, city_names: dict[str, str], dry_run: bool,
              now: datetime | None = None) -> dict:
    """Один порог на всё: разовая зачистка не различает молнию и обычный драфт —
    `--hours` задаёт срок обоим."""
    now = now or datetime.now(timezone.utc)
    before = waiting_counts(db)
    pick = actions.expiring if dry_run else actions.expire
    rows = pick(db, now=now, hours=hours, flash_hours=hours)
    verdict = (f"⏳ протух — без решения дольше {hours} ч, в канал не идёт "
               "(вернётся, если придёт заметная фактура — §28)")

    with_card = edited = 0
    by_status = {s: 0 for s in WAITING}
    for row in rows:
        by_status[row["status"]] += 1
        has_card = bool(row["card_chat_id"] and row["card_message_id"])
        with_card += has_card
        if dry_run or not has_card or bot is None:
            continue
        if edited:
            await asyncio.sleep(EDIT_PAUSE_SEC)
        edited += await rewrite_card(bot, row["card_chat_id"], row["card_message_id"], row,
                                     city_names.get(row["city"]), verdict)

    return {
        "now": iso(now), "cutoff": iso(now - timedelta(hours=hours)), "hours": hours,
        "dry_run": dry_run, "before": before, "expired": len(rows),
        "expired_by_status": by_status, "expired_with_card": with_card,
        "staled": 0 if dry_run else len(rows), "cards_rewritten": edited,
        "after": waiting_counts(db),
    }


def render(report: dict) -> str:
    b, a = report["before"], report["after"]
    mode = " (dry-run: база не тронута)" if report["dry_run"] else ""
    return "\n".join([
        f"зачистка бэклога §55 на {report['now']}{mode}: порог {report['hours']} ч, "
        f"граница {report['cutoff']}",
        f"до:      new {b['new']}, deferred {b['deferred']} (итого {b['new'] + b['deferred']})",
        f"старше {report['hours']} ч: {report['expired']} "
        f"(new {report['expired_by_status']['new']}, "
        f"deferred {report['expired_by_status']['deferred']}; "
        f"с карточкой {report['expired_with_card']})",
        f"→ stale: {report['staled']}, карточек переписано в «⏳ протух»: "
        f"{report['cards_rewritten']} из {report['expired_with_card']}",
        f"осталось живых: new {a['new']}, deferred {a['deferred']} "
        f"(итого {a['new'] + a['deferred']})",
    ])


async def amain(args) -> None:
    cfg = load()
    db = Database(cfg.paths.db)
    names = {slug: city.name for slug, city in cfg.cities.items()}
    bot = None
    if not args.dry_run and not args.no_cards:
        if not cfg.admin_bot_token:
            raise SystemExit("Нет BOT_TOKEN_ADMIN в .env — карточки переписать не смогу "
                             "(запустите с --no-cards, чтобы править только базу).")
        from aiogram import Bot

        bot = Bot(cfg.admin_bot_token)
    try:
        print(render(await run(db, bot, args.hours, names, args.dry_run)))
    finally:
        if bot is not None:
            await bot.session.close()
        db.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Разовая зачистка очереди модерации (§55)")
    p.add_argument("--hours", type=int, default=24,
                   help="драфты старше стольких часов протухают (по умолчанию 24)")
    p.add_argument("--dry-run", action="store_true", help="только посчитать, ничего не менять")
    p.add_argument("--no-cards", action="store_true",
                   help="править только базу, карточки в боте не трогать")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    setup_logging(args.log_level)
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
