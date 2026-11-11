#!/usr/bin/env python
"""Коллектор конвейера: Telegram (события + догон), ВК и RSS.

    .venv/bin/python scripts/collector.py --with-warmup   # рабочий режим на время прогрева
    .venv/bin/python scripts/collector.py --only tg       # только Telegram
    .venv/bin/python scripts/collector.py --only vk,rss   # без Telegram (сессия не нужна)

Что крутится в процессе:
  * ТГ: обработчик NewMessage + догон после простоя (фаза 1);
  * ТГ-фолбэк: опрос неподписанных источников, пока идёт прогрев (QUESTIONS §8) —
    выключается сам, когда прогрев подпишет всех;
  * ВК: wall.get батчами через execute раз в 3 мин;
  * RSS: ленты СМИ и ведомств раз в 7 мин;
  * две очереди медиа: Telethon для ТГ и HTTP для ВК/RSS.

Один аккаунт Telegram = один процесс (session-файл держит SQLite-лок). Режим
`--only vk,rss` сессию не трогает — его можно запускать параллельно, например
пока идёт харденинг аккаунта.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telethon.errors import AuthKeyUnregisteredError, UserDeactivatedError  # noqa: E402

from tgtraff.alerts import Alerter  # noqa: E402
from tgtraff.app import (  # noqa: E402
    build_client, ensure_authorized, load, require_free_session, setup_logging,
)
from tgtraff.collector.http_media import HttpMediaDownloader, httpx_feed_fetcher, httpx_fetcher  # noqa: E402
from tgtraff.collector.rss_collector import RssCollector  # noqa: E402
from tgtraff.collector.tg_collector import TgCollector  # noqa: E402
from tgtraff.collector.tg_polling import PollingFallback  # noqa: E402
from tgtraff.collector.vk_collector import VkApi, VkCollector  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.metrics.retention import Retention  # noqa: E402
from tgtraff.metrics.views import TelethonViews, ViewsCollector  # noqa: E402
from tgtraff.warmup import run_warmup  # noqa: E402

log = logging.getLogger("tgtraff")


async def start_feeds(db, cfg, alerts: Alerter) -> list[asyncio.Task]:
    """ВК и RSS: сессия Telegram им не нужна, поэтому живут отдельными задачами."""
    tasks = []

    if cfg.vk_token:
        vk = VkCollector(VkApi(token=cfg.vk_token, api_version=cfg.vk.api_version), db, cfg)
        tasks.append(asyncio.create_task(vk.run()))
    else:
        log.warning("VK_SERVICE_TOKEN не задан — ВК-паблики не собираются "
                    "(vk.com/apps?act=manage → сервисный ключ доступа)")
        await alerts.alert("VK_SERVICE_TOKEN не задан: ВК-источники не собираются")

    rss = RssCollector(httpx_feed_fetcher(cfg.rss.request_timeout_sec), db, cfg)
    tasks.append(asyncio.create_task(rss.run()))

    http_media = HttpMediaDownloader(httpx_fetcher(cfg.collector.media_download_timeout_sec), db, cfg)
    tasks.append(asyncio.create_task(http_media.run()))
    return tasks


async def housekeeping(db, cfg) -> None:
    """Уборка по ретеншну (фаза 6). Живёт у коллектора: он владелец сырых постов
    и медиа. Отрабатывает раз в локальные сутки — метка в базе не даст сделать
    это дважды, кто бы уборку ни позвал."""
    while True:
        try:
            result = Retention(db, cfg.retention).run()
            if result:
                log.info("ретеншн: %s", result.summary())
        except Exception as exc:                  # noqa: BLE001 — уборка не роняет сбор
            log.exception("ретеншн упал: %s", exc)
        await asyncio.sleep(3600)


async def run_telegram(db, cfg, args, alerts: Alerter) -> None:
    require_free_session(cfg)
    client = build_client(cfg)
    await client.connect()
    await ensure_authorized(client, cfg)

    collector = TgCollector(client, db, cfg)
    tasks: list[asyncio.Task] = []
    try:
        if args.with_warmup:
            tasks.append(asyncio.create_task(run_warmup(client, db, cfg)))

        # Пока прогрев не подписал источники, неподписанные читаются опросом.
        fallback = PollingFallback(client, db, cfg,
                                   source_provider=lambda: collector.sources.values())
        tasks.append(asyncio.create_task(fallback.run()))

        # Просмотры собственных постов (фаза 6): Bot API их не отдаёт, а
        # session-файл держит один процесс — поэтому съём здесь. Это чтение
        # счётчика под уже опубликованным постом, публикацией юзербот
        # по-прежнему не занимается.
        if cfg.metrics.enabled:
            views = ViewsCollector(db, TelethonViews(client), cfg.metrics, alerter=alerts)
            tasks.append(asyncio.create_task(views.run()))

        await collector.run()
    except (AuthKeyUnregisteredError, UserDeactivatedError) as e:
        log.error("ЮЗЕРБОТ РАЗЛОГИНЕН/ЗАБЛОКИРОВАН: %s. Нужен повторный вход: scripts/login.py", e)
        await alerts.alert(f"юзербот разлогинен/заблокирован: {e}. Нужен scripts/login.py")
        raise SystemExit(2)
    finally:
        for task in tasks:
            task.cancel()
        await client.disconnect()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-warmup", action="store_true",
                    help="вести плавный прогрев в этом же процессе")
    ap.add_argument("--only", default="tg,vk,rss",
                    help="какие коллекторы поднимать: tg, vk, rss через запятую")
    args = ap.parse_args()

    setup_logging()
    cfg = load()
    db = Database(cfg.paths.db)
    alerts = Alerter.from_env()
    only = {p.strip() for p in args.only.split(",") if p.strip()}
    log.info("коллекторы: %s%s", ", ".join(sorted(only)),
             "; алерты в Bot API включены" if alerts.enabled else "")

    if not only & {"tg", "vk", "rss"}:
        raise SystemExit("нечего запускать: проверьте --only")

    feed_tasks = await start_feeds(db, cfg, alerts) if only & {"vk", "rss"} else []
    # Уборка по ретеншну идёт в любом составе коллекторов: диск засоряется
    # независимо от того, какие ленты сейчас включены.
    feed_tasks.append(asyncio.create_task(housekeeping(db, cfg)))
    try:
        if "tg" in only:
            await run_telegram(db, cfg, args, alerts)
        else:
            await asyncio.gather(*feed_tasks)
    finally:
        for task in feed_tasks:
            task.cancel()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nостановлен оператором")
