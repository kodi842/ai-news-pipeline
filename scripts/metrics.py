#!/usr/bin/env python
"""Фаза 6: метрики, инвайт-ссылки, CPF-отчёт, ретеншн (спека §9).

    .venv/bin/python scripts/metrics.py --status            # что снято, а что нет
    .venv/bin/python scripts/metrics.py --links             # завести недостающие ссылки
    .venv/bin/python scripts/metrics.py --cpf               # отчёт по площадкам
    .venv/bin/python scripts/metrics.py --spend vk_seeding=15000 --city cherepovets
    .venv/bin/python scripts/metrics.py --subs              # снимок подписчиков сейчас
    .venv/bin/python scripts/metrics.py --join-channels     # подписать юзербота (один раз)
    .venv/bin/python scripts/metrics.py --digest            # показать сводку дня
    .venv/bin/python scripts/metrics.py --digest --send     # и отправить модераторам
    .venv/bin/python scripts/metrics.py --retention --force # уборка прямо сейчас

Просмотры своих постов снимает ЮЗЕРБОТ (Bot API их не отдаёт, QUESTIONS §39) —
это делает процесс коллектора, который держит session-файл. Здесь тот же съём
доступен разово (`--views`) и только при свободной сессии.

В рабочем режиме ничего из этого запускать руками не нужно: съём просмотров
живёт в коллекторе, снимки/ссылки/дайджест/уборка — в процессе модерации.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.alerts import Alerter  # noqa: E402
from tgtraff.app import load, setup_logging  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.metrics import store  # noqa: E402
from tgtraff.metrics.digest import DailyDigest  # noqa: E402
from tgtraff.metrics.invites import InviteLinks  # noqa: E402
from tgtraff.metrics.report import cpf_report  # noqa: E402
from tgtraff.metrics.retention import Retention  # noqa: E402
from tgtraff.metrics.subs import SubsCollector  # noqa: E402

log = logging.getLogger("tgtraff.metrics")


def city_names(cfg) -> dict[str, str]:
    return {slug: city.name for slug, city in cfg.cities.items()}


def make_bot(cfg):
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties

    if not cfg.admin_bot_token:
        raise SystemExit("Нет BOT_TOKEN_ADMIN в .env — Bot API недоступен.")
    return Bot(cfg.admin_bot_token, default=DefaultBotProperties(parse_mode="HTML"))


def make_digest(cfg, db, notify):
    return DailyDigest(db, notify, cfg.channels, cfg.metrics,
                       city_names=city_names(cfg), city_tz=cfg.city_tz())


# ---------- команды ----------

def show_status(cfg, db) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    total, measured = store.views_coverage(db, now)
    print(f"просмотры: снято у {measured} из {total} постов последних суток")
    for slug in cfg.channels.cities:
        snap = store.last_snapshot(db, slug)
        growth = store.growth(db, slug, now)
        if snap is None:
            print(f"  {slug:14} снимков ещё нет")
            continue
        delta = f" ({growth:+d} за сутки)" if growth is not None else ""
        er = f"{snap['er'] * 100:.1f}%" if snap["er"] else "—"
        print(f"  {slug:14} подписчиков {snap['subscribers']}{delta}, "
              f"охват за сутки {snap['views_24h']}, ER {er} "
              f"(снимок {snap['captured_at']})")
    rows = store.links(db)
    print(f"\nинвайт-ссылок заведено: {len(rows)}")
    for row in rows:
        print(f"  {row['city']:14} {row['name']:<12} {row['kind'] or '—':<8} "
              f"вступлений {row['joins_total']:<5} {row['url'] or 'ссылка не создана'}")


def set_spend(db, cfg, pairs: list[str], city: str | None) -> None:
    """`--spend vk_seeding=15000` — сколько заплачено за площадку.

    Автоматически это знание взять неоткуда: цифры живут в кабинете VK Ads и
    в переписке с админами пабликов."""
    for pair in pairs:
        name, _, amount = pair.partition("=")
        found = [r for r in store.links(db, city) if r["name"] == name.strip()]
        if not found:
            print(f"  ссылки «{name}» нет — заведите её: --links")
            continue
        for row in found:
            store.set_cost(db, row["id"], float(amount))
            print(f"  {row['city']}: {row['name']} — {float(amount):.0f} ₽")


async def run_links(cfg, db) -> None:
    bot = make_bot(cfg)
    links = InviteLinks(db, bot, cfg.channels, cfg.metrics, city_tz=cfg.city_tz(),
                        alerter=Alerter.from_env())
    created = await links.ensure()
    print(f"создано ссылок: {len(created)}")
    for row in created:
        print(f"  {row['city']:14} {row['name']:<12} {row['url']}")
    if not created:
        print("  все ссылки уже заведены (или каналы ещё не настроены)")
    await bot.session.close()


async def run_subs(cfg, db) -> None:
    bot = make_bot(cfg)
    saved = await SubsCollector(db, bot, cfg.channels, cfg.metrics,
                                alerter=Alerter.from_env()).tick()
    print(f"снимков записано: {saved}")
    await bot.session.close()


async def run_join(cfg, db) -> None:
    """Подписать юзербота на НАШИ каналы — иначе просмотры не снять.

    Просмотры видит только клиент MTProto, и приватный канал он читает, лишь
    будучи подписчиком. Вход идёт по отдельной служебной ссылке: попади он на
    ссылку площадки — испортил бы её статистику одним лишним «подписчиком».
    Это по-прежнему чтение: подписка на канал — условие чтения, а не рассылка
    (тот же случай, что и вступление в источники на прогреве)."""
    from tgtraff.app import build_client, ensure_authorized, require_free_session

    bot = make_bot(cfg)
    invites = InviteLinks(db, bot, cfg.channels, cfg.metrics, city_tz=cfg.city_tz())
    targets = {}
    for slug, city in cfg.channels.cities.items():
        if city.tg.chat_id:
            targets[slug] = await invites.service_link(slug)
    await bot.session.close()

    require_free_session(cfg)
    client = build_client(cfg)
    await client.connect()
    await ensure_authorized(client, cfg)
    try:
        for slug, url in targets.items():
            if not url:
                print(f"  {slug}: служебной ссылки нет — канал не настроен?")
                continue
            print(f"  {slug}: {await _join(client, url)}")
    finally:
        await client.disconnect()


async def _join(client, url: str) -> str:
    from telethon.errors import UserAlreadyParticipantError
    from telethon.tl.functions.messages import ImportChatInviteRequest

    hash_ = url.rstrip("/").rsplit("/+", 1)[-1]
    try:
        await client(ImportChatInviteRequest(hash_))
        return "подписан"
    except UserAlreadyParticipantError:
        return "уже подписан"
    except Exception as exc:                      # noqa: BLE001
        return f"не вышло: {type(exc).__name__}: {exc}"


async def run_views(cfg, db) -> None:
    """Разовый съём просмотров. В рабочем режиме этим занят коллектор."""
    from tgtraff.app import build_client, ensure_authorized, require_free_session
    from tgtraff.metrics.views import TelethonViews, ViewsCollector

    require_free_session(cfg)
    client = build_client(cfg)
    await client.connect()
    await ensure_authorized(client, cfg)
    try:
        saved = await ViewsCollector(db, TelethonViews(client), cfg.metrics,
                                     alerter=Alerter.from_env()).tick()
        print(f"просмотры обновлены у {saved} постов")
    finally:
        await client.disconnect()


async def run_digest(cfg, db, send: bool) -> None:
    if not send:
        print(make_digest(cfg, db, _noop).build())
        return
    bot = make_bot(cfg)
    moderators = cfg.moderation.moderators or ()
    if not moderators:
        raise SystemExit("Некому отправлять: пусто в ADMIN_CHAT_IDS.")

    async def notify(text: str) -> None:
        for chat_id in moderators:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                   disable_web_page_preview=True)

    text = await make_digest(cfg, db, notify).ensure()
    if text is None:                              # вне часа или уже отправляли — шлём принудительно
        text = make_digest(cfg, db, notify).build()
        await notify(text)
    print("дайджест отправлен")
    await bot.session.close()


async def _noop(text: str) -> None:
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Метрики и ретеншн (фаза 6)")
    ap.add_argument("--status", action="store_true", help="что снято, а что нет")
    ap.add_argument("--links", action="store_true", help="завести недостающие инвайт-ссылки")
    ap.add_argument("--cpf", action="store_true", help="CPF-отчёт по площадкам")
    ap.add_argument("--spend", nargs="+", metavar="ИМЯ=РУБЛИ",
                    help="сколько заплачено за площадку")
    ap.add_argument("--city", help="только один город")
    ap.add_argument("--subs", action="store_true", help="снимок подписчиков сейчас")
    ap.add_argument("--views", action="store_true", help="разовый съём просмотров (юзербот)")
    ap.add_argument("--join-channels", action="store_true",
                    help="подписать юзербота на наши каналы (нужно один раз, "
                         "иначе просмотры не снимаются)")
    ap.add_argument("--digest", action="store_true", help="сводка дня")
    ap.add_argument("--send", action="store_true", help="и отправить её модераторам")
    ap.add_argument("--retention", action="store_true", help="уборка по ретеншну")
    ap.add_argument("--force", action="store_true", help="уборка даже если сегодня уже была")
    ap.add_argument("--out", help="файл для отчёта")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    setup_logging(args.log_level)
    cfg = load()
    db = Database(cfg.paths.db)
    try:
        if args.spend:
            set_spend(db, cfg, args.spend, args.city)
        if args.links:
            asyncio.run(run_links(cfg, db))
        if args.subs:
            asyncio.run(run_subs(cfg, db))
        if args.join_channels:
            asyncio.run(run_join(cfg, db))
        if args.views:
            asyncio.run(run_views(cfg, db))
        if args.retention:
            result = Retention(db, cfg.retention).run(force=args.force)
            print(result.summary() if result else "сегодня уборка уже была (--force — повторить)")
        if args.cpf:
            text = cpf_report(db, city=args.city, city_tz=cfg.city_tz()).render()
            _emit(text, args.out)
        if args.digest:
            asyncio.run(run_digest(cfg, db, args.send))
        if not any((args.status, args.links, args.cpf, args.spend, args.subs, args.views,
                    args.digest, args.retention, args.join_channels)):
            args.status = True
        if args.status:
            show_status(cfg, db)
    except KeyboardInterrupt:
        log.info("остановлено вручную")
    finally:
        db.close()


def _emit(text: str, out: str | None) -> None:
    if out:
        Path(out).write_text(text, "utf-8")
        print(f"отчёт записан: {out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
