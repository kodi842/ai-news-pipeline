#!/usr/bin/env python
"""Админ-бот модерации + publisher + метрики (спека §7, §8, §9).

    .venv/bin/python scripts/moderation.py                 # рабочий режим
    .venv/bin/python scripts/moderation.py --no-publisher   # только карточки
    .venv/bin/python scripts/moderation.py --once           # один тик, без polling
    .venv/bin/python scripts/moderation.py --status         # что в очередях, куда публикуем

Что крутится в процессе:
  * админ-бот (aiogram): карточки драфтов, кнопки, правка ответным сообщением;
  * карточки клипов завода V1 (VIDEO-SPEC): готовый mp4 + сценарий + кнопки
    [Публиковать] [Отклонить] [Перегенерить хук]. Сборка идёт в контейнере
    `clips`, здесь — только выдача и решения;
  * автопубликация §59: нейтральная новость, прошедшая все шесть условий, идёт
    в очередь мимо карточки (выключатель — пустой safety.autopublish_categories);
  * publisher: очередь → слоты городов → доставки (Telegram, при токене — ВК);
  * сервисные автопосты: утренняя погода и вечерний топ-5;
  * монитор алертов: молчащие источники, протухший токен, переполненная очередь;
  * метрики фазы 6: снимки подписчиков и охвата, инвайт-ссылки под источники
    трафика, счёт вступлений по ним и дневной дайджест модераторам.

Публикация идёт ТОЛЬКО через Bot API (CLAUDE.md), юзербот здесь не участвует.
Пока config/channels.yaml в режиме `mode: test`, боевые каналы недоступны,
даже если их id лежат в .env.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.alerts import Alerter  # noqa: E402
from tgtraff.app import load, setup_logging, sync_sources  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.metrics.digest import DailyDigest  # noqa: E402
from tgtraff.metrics.invites import InviteLinks  # noqa: E402
from tgtraff.metrics.report import cpf_report  # noqa: E402
from tgtraff.metrics.subs import SubsCollector  # noqa: E402
from tgtraff.moderation.bot import AdminBot, connect_bot  # noqa: E402
from tgtraff.moderation.monitor import Monitor  # noqa: E402
from tgtraff.moderation.report import render_preview  # noqa: E402
from tgtraff.publisher import store  # noqa: E402
from tgtraff.publisher.delivery import TelegramDelivery  # noqa: E402
from tgtraff.publisher.pins import Pins  # noqa: E402
from tgtraff.publisher.planner import Planner  # noqa: E402
from tgtraff.publisher.runner import Publisher  # noqa: E402
from tgtraff.publisher.service import ServicePosts  # noqa: E402
from tgtraff.publisher.vk import VkDelivery  # noqa: E402
from tgtraff.publisher.weather import WeatherClient  # noqa: E402

log = logging.getLogger("tgtraff.moderation")


def build_deliveries(cfg, bot, publisher_bot):
    """Адаптеры доставок. ВК появляется только там, где включён и есть токен.

    Токен сообщества — свой у каждого города (§41): ключ выдаёт конкретная
    группа. Поэтому в адаптер уходит фабрика клиентов, а не один клиент."""
    deliveries = {"tg": TelegramDelivery(publisher_bot, cfg.publisher.parse_mode)}
    ready = [slug for slug, c in cfg.channels.cities.items() if c.vk.is_ready]
    if ready:
        from tgtraff.collector.vk_collector import VkApi

        deliveries["vk"] = VkDelivery(api_factory=lambda token: VkApi(
            token=token, api_version=cfg.vk.api_version))
        log.info("кросс-постинг в ВК включён: %s", ", ".join(ready))
    return deliveries


def build_photos(cfg, db):
    """Приём фото от модератора (§61.1). Детектор чужих знаков — тот же
    vision-Haiku, что смотрит медиа кластера; без ключа он не создаётся, и
    тогда КАЖДОЕ присланное фото требует подтверждения кнопкой."""
    from tgtraff.moderation.photo import ModeratorPhoto

    detector = None
    if cfg.llm.enabled:
        from tgtraff.llm.client import LlmClient, LlmError
        from tgtraff.rewriter.watermark import WatermarkDetector

        try:
            detector = WatermarkDetector(LlmClient(cfg.llm, api_key=cfg.anthropic_key),
                                         model=cfg.llm.vision_model)
        except LlmError as exc:
            log.warning("детектор чужих знаков не поднялся (%s) — каждое присланное "
                        "фото будет требовать подтверждения кнопкой", exc)
    return ModeratorPhoto(db, media_dir=cfg.paths.media_dir, detector=detector)


def print_status(cfg, db) -> None:
    mode = "БОЕВОЙ" if cfg.channels.is_live else "ТЕСТОВЫЙ"
    print(f"режим публикации: {mode}")
    for slug, city in cfg.channels.cities.items():
        targets = ", ".join(city.targets()) or "канал не настроен"
        chat = city.tg.chat_id or "—"
        slots = ", ".join(str(s) for s in city.slots)
        print(f"  {slug:14} {targets:12} chat_id={chat:16} слоты {slots} "
              f"лимит {city.daily_limit}/сут")
    drafts = db.conn.execute(
        "SELECT status, COUNT(*) c FROM drafts GROUP BY status").fetchall()
    print("драфты:", ", ".join(f"{r['status']} {r['c']}" for r in drafts) or "пусто")
    print("очередь:", ", ".join(f"{k} {v}" for k, v in sorted(
        store.queue_counts(db).items())) or "пусто")


async def run(cfg, db, args) -> None:
    from aiogram import Bot, Dispatcher, F
    from aiogram.client.default import DefaultBotProperties
    from aiogram.filters import CommandStart, Command, CommandObject
    from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

    if not cfg.admin_bot_token:
        raise SystemExit("Нет BOT_TOKEN_ADMIN в .env — админ-бот не поднять.")

    bot = Bot(cfg.admin_bot_token,
              default=DefaultBotProperties(parse_mode=cfg.publisher.parse_mode))
    publisher_bot = bot
    if cfg.publisher_bot_token and cfg.publisher_bot_token != cfg.admin_bot_token:
        publisher_bot = Bot(cfg.publisher_bot_token,
                            default=DefaultBotProperties(parse_mode=cfg.publisher.parse_mode))

    names = {slug: city.name for slug, city in cfg.cities.items()}
    deliveries = build_deliveries(cfg, bot, publisher_bot)
    planner = Planner(cfg.channels, cfg.city_tz())
    admin = AdminBot(db, bot, cfg.channels, planner, cfg.moderation,
                     city_names=names, deliveries=deliveries,
                     autopublish_categories=cfg.safety.autopublish_categories,
                     photos=build_photos(cfg, db), inbox_dir=cfg.paths.media_dir / "moderator_inbox",
                     clip_settings=cfg.clips)
    # Закрепы ботом (CHANNEL-SETUP §60.5): команда /pins.
    admin.pins = Pins(db, bot, cfg.channels, cfg.publisher.parse_mode)
    alerts = Alerter.from_env()
    publisher = Publisher(db, cfg.channels, deliveries, cfg.publisher, alerter=alerts)
    publisher.on_published = admin.announce_published
    service = ServicePosts(db, cfg.channels, cfg.city_meta(), cfg.publisher,
                           weather=WeatherClient())
    monitor = Monitor(db, alerts, cfg.moderation)

    # Узлы метрик фазы 6, которым нужен Bot API: подписчики, инвайт-ссылки под
    # источники трафика и дневная сводка. Просмотры снимает коллектор (юзербот).
    subs = SubsCollector(db, bot, cfg.channels, cfg.metrics, alerter=alerts)
    invites = InviteLinks(db, bot, cfg.channels, cfg.metrics, city_tz=cfg.city_tz(),
                          alerter=alerts)
    digest = DailyDigest(db, admin.broadcast, cfg.channels, cfg.metrics,
                         city_names=names, city_tz=cfg.city_tz())

    me = await connect_bot(bot)               # ждём сеть, а не падаем в рестарт-луп
    log.info("админ-бот @%s, режим публикации %s, модераторы: %s", me.username,
             "БОЕВОЙ" if cfg.channels.is_live else "ТЕСТОВЫЙ",
             ", ".join(map(str, admin.moderators)) or "не заданы (см. ADMIN_CHAT_IDS)")
    # Процесс печатает свои параметры при старте (правило ревью после YAML-ловушки).
    m = cfg.moderation
    log.info("очередь §55: пуш только срочного (молния, UGC, manual_only), карточек за тик %s; "
             "протухание normal %s ч / flash %s ч, до %s карточек за тик; /queue по умолчанию %s",
             m.cards_per_tick, m.stale_hours, m.stale_flash_hours, m.stale_per_tick,
             m.queue_default)
    log.info("алерт очереди §57: порог %s неразобранного срочного, повтор не чаще раза в %s ч",
             m.queue_alert_size, m.queue_alert_repeat_hours)
    log.info("автопубликация §59: %s", ", ".join(cfg.safety.autopublish_categories)
             or "ВЫКЛЮЧЕНА (пустой autopublish_categories)")
    log.info("клипы V1: %s; публикация ручная, автопаба клипов нет",
             f"карточки включены, {cfg.clips.clips_per_day}/сутки на город"
             if cfg.clips.enabled else "ВЫКЛЮЧЕНЫ (clips.enabled: false)")

    if args.once:
        await admin.expire()
        await admin.autopublish()
        await admin.push_cards()
        await admin.push_clip_cards()
        if not args.no_publisher:
            await service.ensure()
            await publisher.tick()
        await monitor.check()
        if cfg.metrics.enabled:
            await invites.ensure()
            await subs.tick()
            await digest.ensure()
        await bot.session.close()
        return

    dp = Dispatcher()

    @dp.message(CommandStart())
    async def _start(message: Message):
        await admin.handle_start(message.chat.id, message.from_user.id,
                                 message.from_user.username)

    @dp.message(Command("queue"))
    async def _queue(message: Message, command: CommandObject):
        # /queue — сводка, /queue N — выдать N свежих (§55).
        await admin.handle_queue(message.chat.id, message.from_user.id, command.args)

    @dp.message(Command("pins"))
    async def _pins(message: Message, command: CommandObject):
        # /pins — план и кнопка; /pins <город> — только он (CHANNEL-SETUP §60.5).
        await admin.handle_pins(message.chat.id, message.from_user.id, command.args)

    @dp.message(Command("status"))
    async def _status(message: Message):
        await admin.handle_status(message.chat.id, message.from_user.id)

    @dp.message(Command("digest"))
    async def _digest(message: Message):
        await admin.handle_digest(message.chat.id, message.from_user.id, digest)

    @dp.message(Command("cpf"))
    async def _cpf(message: Message):
        await admin.handle_cpf(message.chat.id, message.from_user.id,
                               cpf_report(db, city_tz=cfg.city_tz()).render())

    @dp.message(F.reply_to_message, F.photo | F.document)
    async def _photo_reply(message: Message):
        """Ответ на карточку ФОТОГРАФИЕЙ — новое медиа драфта (§61.1).

        Первая версия — одно фото: `photo` (сжатое) или image-документ
        (отправленное «файлом», без сжатия). Видео — потом, по надобности;
        документ другого типа проходит дальше как обычная правка текста."""
        file_id = None
        if message.photo:
            file_id = message.photo[-1].file_id           # самый крупный размер
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
        if file_id is None:
            return await _reply(message)
        await admin.handle_photo_reply(
            message.from_user.id, message.chat.id, message.reply_to_message.message_id,
            file_id, caption=message.caption)

    @dp.message(F.reply_to_message)
    async def _reply(message: Message):
        handled = await admin.handle_reply(
            message.from_user.id, message.chat.id,
            message.reply_to_message.message_id, message.text or message.caption or "")
        if not handled:
            log.debug("ответ не привязан к карточке — пропущен")

    @dp.my_chat_member()
    async def _membership(event: ChatMemberUpdated):
        await admin.handle_my_chat_member(
            event.chat.id, event.chat.title, event.chat.type,
            event.new_chat_member.status)

    @dp.chat_member()
    async def _joined(event: ChatMemberUpdated):
        """Прирост канала с точностью до площадки (спека §9): Telegram сам
        говорит, по какой ссылке пришёл человек."""
        if event.new_chat_member.status not in ("member", "administrator", "creator"):
            return
        if event.old_chat_member.status in ("member", "administrator", "creator"):
            return                                # смена роли, а не новый подписчик
        invite = getattr(event, "invite_link", None)
        invites.joined(event.chat.id, getattr(invite, "invite_link", None))

    @dp.callback_query()
    async def _callback(query: CallbackQuery):
        answer = await admin.handle_callback(
            query.from_user.id, query.message.chat.id, query.message.message_id,
            query.data or "")
        await query.answer(answer[:200])

    async def cards_loop():
        """Сначала протухание, потом автопубликация, потом пуш: протухшему
        карточка уже не нужна, а автопост её не получает вовсе (§59)."""
        while True:
            try:
                await admin.expire()
                await admin.autopublish()
                await admin.push_cards()
                # Клипы завода V1: собранные mp4 ждут карточки в том же чате.
                await admin.push_clip_cards()
            except Exception as exc:              # noqa: BLE001
                log.exception("выкладка карточек упала: %s", exc)
            await asyncio.sleep(cfg.moderation.poll_seconds)

    async def publish_loop():
        while True:
            try:
                await service.ensure()
                await publisher.tick()
            except Exception as exc:              # noqa: BLE001
                log.exception("тик publisher'а упал: %s", exc)
            await asyncio.sleep(cfg.publisher.poll_seconds)

    async def metrics_loop():
        """Снимки канала, недостающие инвайт-ссылки и дневная сводка."""
        while True:
            try:
                await invites.ensure()
                await subs.tick()
                await digest.ensure()
            except Exception as exc:              # noqa: BLE001
                log.exception("метрики упали: %s", exc)
            await asyncio.sleep(cfg.metrics.subs_poll_seconds)

    async def monitor_loop():
        while True:
            try:
                await monitor.check()
            except Exception as exc:              # noqa: BLE001
                log.exception("монитор упал: %s", exc)
            await asyncio.sleep(cfg.moderation.monitor_seconds)

    tasks = [asyncio.create_task(cards_loop()), asyncio.create_task(monitor_loop())]
    if cfg.metrics.enabled:
        tasks.append(asyncio.create_task(metrics_loop()))
    if not args.no_publisher:
        tasks.append(asyncio.create_task(publish_loop()))
    try:
        # `chat_member` Telegram по умолчанию не шлёт — без него не было бы
        # видно, по какой ссылке пришёл подписчик (а значит, и CPF площадки).
        await dp.start_polling(bot, handle_signals=False,
                               allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        await bot.session.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Модерация и публикация (фаза 5)")
    p.add_argument("--no-publisher", action="store_true",
                   help="только карточки и алерты, без публикации")
    p.add_argument("--once", action="store_true", help="один тик и выход")
    p.add_argument("--status", action="store_true", help="показать очереди и каналы")
    p.add_argument("--preview", type=int, metavar="N",
                   help="показать N карточек и постов как они выйдут (без сети)")
    p.add_argument("--out", help="куда положить предпросмотр (по умолчанию — в консоль)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    setup_logging(args.log_level)
    cfg = load()
    db = Database(cfg.paths.db)
    sync_sources(cfg, db)
    try:
        if args.status:
            print_status(cfg, db)
            return
        if args.preview:
            text = render_preview(db, cfg.channels,
                                  {s: c.name for s, c in cfg.cities.items()},
                                  limit=args.preview)
            if args.out:
                Path(args.out).write_text(text, "utf-8")
                print(f"предпросмотр записан: {args.out}")
            else:
                print(text)
            return
        asyncio.run(run(cfg, db, args))
    except KeyboardInterrupt:
        log.info("остановлено вручную")
    finally:
        db.close()


if __name__ == "__main__":
    main()
