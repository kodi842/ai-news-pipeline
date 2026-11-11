#!/usr/bin/env python
"""Фаза 5: бот «Прислать новость» (UGC-приём).

    .venv/bin/python scripts/ugc.py            # рабочий режим
    .venv/bin/python scripts/ugc.py --status   # сколько заявок и от кого

Один бот на всю сеть: город приходит deep-link'ом из кнопки под постом
(`?start=mgn|nvk|che`). Присланное попадает в ту же очередь модерации
с пометкой `ugc` — отдельной очереди у UGC нет (спека, приёмка фазы 5).
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load, setup_logging  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.moderation.ugc import UgcIntake  # noqa: E402
from tgtraff.moderation.ugc_bot import UgcBot  # noqa: E402

log = logging.getLogger("tgtraff.ugc")


def deep_links(cfg) -> dict[str, str]:
    return {c.deep_link: slug for slug, c in cfg.channels.cities.items()}


def print_status(cfg, db) -> None:
    rows = db.conn.execute(
        "SELECT city, COUNT(*) senders, SUM(submissions) subs FROM ugc_senders "
        "GROUP BY city").fetchall()
    print("UGC-отправители:")
    for r in rows:
        print(f"  {r['city'] or '—':14} читателей {r['senders']:3}  заявок {r['subs'] or 0}")
    drafts = db.conn.execute(
        "SELECT status, COUNT(*) c FROM drafts WHERE origin='ugc' GROUP BY status").fetchall()
    print("UGC-драфты:", ", ".join(f"{r['status']} {r['c']}" for r in drafts) or "пока нет")


async def run(cfg, db) -> None:
    from aiogram import Bot, Dispatcher, F
    from aiogram.client.default import DefaultBotProperties
    from aiogram.filters import CommandStart, CommandObject
    from aiogram.types import CallbackQuery, Message

    if not cfg.ugc_bot_token:
        raise SystemExit("Нет BOT_TOKEN_UGC в .env — UGC-бот не поднять.")

    bot = Bot(cfg.ugc_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    intake = UgcIntake(db, deep_links(cfg), media_dir=cfg.paths.media_dir)
    ugc = UgcBot(db, bot, intake, city_names={s: c.name for s, c in cfg.cities.items()},
                 download_dir=cfg.paths.media_dir / "ugc_inbox")

    from tgtraff.moderation.bot import connect_bot

    me = await connect_bot(bot)               # ждём сеть, а не падаем в рестарт-луп
    log.info("UGC-бот @%s поднят; города: %s", me.username, ", ".join(deep_links(cfg)))

    dp = Dispatcher()

    @dp.message(CommandStart())
    async def _start(message: Message, command: CommandObject):
        await ugc.handle_start(message.chat.id, message.from_user.id,
                               message.from_user.username, command.args)

    @dp.callback_query()
    async def _city(query: CallbackQuery):
        await ugc.handle_city_choice(query.from_user.id, query.message.chat.id,
                                     query.data or "")
        await query.answer()

    @dp.message(F.photo | F.video | F.text | F.document)
    async def _submission(message: Message):
        file_id, kind = None, None
        if message.photo:
            file_id, kind = message.photo[-1].file_id, "photo"
        elif message.video:
            file_id, kind = message.video.file_id, "video"
        elif message.document:
            file_id, kind = message.document.file_id, "document"
        await ugc.handle_submission(message.chat.id, message.from_user.id,
                                    message.from_user.username,
                                    message.caption or message.text or "",
                                    file_id=file_id, media_kind=kind)

    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        await bot.session.close()


def main() -> None:
    p = argparse.ArgumentParser(description="UGC-бот «Прислать новость» (фаза 5)")
    p.add_argument("--status", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    setup_logging(args.log_level)
    cfg = load()
    db = Database(cfg.paths.db)
    try:
        if args.status:
            print_status(cfg, db)
            return
        asyncio.run(run(cfg, db))
    except KeyboardInterrupt:
        log.info("остановлено вручную")
    finally:
        db.close()


if __name__ == "__main__":
    main()
