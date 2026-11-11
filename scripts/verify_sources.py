#!/usr/bin/env python
"""Верификация источников из config/sources.yaml (задача фаз 1-2 по спеке).

    .venv/bin/python scripts/verify_sources.py --rss      # ленты СМИ и ведомств (HTTP)
    .venv/bin/python scripts/verify_sources.py --tg       # каналы по названию (юзербот)
    .venv/bin/python scripts/verify_sources.py --vk       # паблики ВК (VK API, нужен токен)
    .venv/bin/python scripts/verify_sources.py --all

Скрипт ничего не переписывает в конфиге: он печатает отчёт, а решение о правках
принимает человек (sources.yaml ведёт стратегическая сессия). Для --tg нужен
свободный session-файл: коллектор на время проверки остановить.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load, require_free_session, setup_logging  # noqa: E402
from tgtraff.collector.rss_collector import parse_feed  # noqa: E402
from tgtraff.verify import (  # noqa: E402
    autodiscovered_feeds, feed_candidates, looks_like_feed, normalise_username, score_channel,
    vk_candidates,
)

OK, FAIL, WARN = "✅", "❌", "⚠️ "


def feed_sources(cfg, only_unresolved: bool):
    for city in cfg.active():
        for spec in (*city.rss_sources, *city.official):
            if spec.key and (spec.status == "resolve" or not only_unresolved):
                yield spec


async def verify_rss(cfg, only_unresolved: bool) -> None:
    import httpx

    print("\n=== RSS и сайты ведомств ===", flush=True)
    headers = {"User-Agent": cfg.rss.user_agent}
    limits = httpx.Limits(max_connections=20)
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers,
                                 limits=limits, verify=False) as client:
        specs = list(feed_sources(cfg, only_unresolved))
        results = await asyncio.gather(*(_find_feed(client, s.key) for s in specs))

    for spec, found in zip(specs, results):
        print(f"\n{spec.city}: {spec.key}")
        if spec.note:
            print(f"  {spec.note}")
        if found:
            url, entries = found
            newest = max((e.get("published", "") or "") for e in entries) if entries else ""
            print(f"  {OK} лента: {url}")
            print(f"     записей: {len(entries)}, свежая: {newest or 'дата не указана'}")
            print(f"     пример: {(entries[0].get('title') or '')[:70]}")
        else:
            print(f"  {FAIL} ленту не нашёл — либо её нет (парсинг HTML — отдельная задача), "
                  f"либо адрес сменился")


async def _probe(client, url: str):
    try:
        r = await client.get(url)
    except Exception:
        return None
    if r.status_code == 200 and looks_like_feed(r.content):
        entries = parse_feed(r.content)
        if entries:
            return url, entries
    return None


async def _find_feed(client, url: str):
    """Сначала спрашиваем у самой страницы (autodiscovery), потом перебираем типовые пути.
    Кандидатов проверяем параллельно: их до двух десятков на источник."""
    candidates = []
    try:
        page = await client.get(url if "://" in url else f"https://{url}")
        if page.status_code == 200:
            if looks_like_feed(page.content):
                entries = parse_feed(page.content)
                if entries:
                    return str(page.url), entries
            candidates.extend(autodiscovered_feeds(page.text, str(page.url)))
    except Exception:
        pass

    candidates.extend(c for c in feed_candidates(url) if c not in candidates)
    found = await asyncio.gather(*(_probe(client, c) for c in candidates))
    # Порядок кандидатов осмысленный: то, что объявил сам сайт, идёт первым.
    return next((f for f in found if f), None)


async def verify_tg(cfg) -> None:
    from telethon.tl.functions.contacts import SearchRequest

    from tgtraff.app import build_client, ensure_authorized

    print("\n=== ТГ-каналы со статусом resolve ===")
    require_free_session(cfg)
    client = build_client(cfg)          # read-only: поиск и резолв — чтение
    await client.connect()
    await ensure_authorized(client, cfg)
    try:
        for city in cfg.active():
            for spec in (*city.tg_sources, *city.tg_regional):
                if spec.status != "resolve" or not spec.query:
                    continue
                print(f"\n{city.slug}: «{spec.query}»")
                print(f"  {spec.note or ''}".rstrip())
                try:
                    res = await client(SearchRequest(q=spec.query, limit=10))
                except Exception as e:
                    print(f"  {FAIL} поиск не удался: {type(e).__name__}: {e}")
                    continue

                ranked = sorted(
                    ((score_channel(getattr(c, "title", ""), spec.query), c) for c in res.chats),
                    key=lambda p: p[0], reverse=True)
                hits = [(s, c) for s, c in ranked if s > 0]
                if not hits:
                    print(f"  {FAIL} поиск ничего похожего не дал "
                          f"(канал приватный или называется иначе — искать инвайт на TGStat)")
                for score, chat in hits[:3]:
                    username = normalise_username(getattr(chat, "username", None))
                    mark = OK if username else WARN
                    print(f"  {mark} {getattr(chat, 'title', '?')} "
                          f"@{username or '— приватный, нужен инвайт'} (совпадение {score})")
    finally:
        await client.disconnect()


async def _resolve_by_candidates(api, spec) -> None:
    """`groups.search` сервисному ключу недоступен (нужен пользовательский токен,
    он запрещён решением ревью §16), поэтому проверяем правдоподобные адреса
    через resolveScreenName + groups.getById."""
    from tgtraff.collector.vk_collector import VkError

    candidates = vk_candidates(spec.query, note=spec.note, city=spec.city)
    print(f"  проверяю {len(candidates)} кандидатов: {', '.join(candidates[:6])}"
          f"{'…' if len(candidates) > 6 else ''}")
    hits = []
    for name in candidates:
        try:
            resolved = await api.call("utils.resolveScreenName", screen_name=name)
        except VkError:
            continue
        await asyncio.sleep(0.35)
        if not resolved or resolved.get("type") != "group":
            continue
        try:
            groups = await api.call("groups.getById", group_ids=name,
                                    fields="members_count,is_closed")
        except VkError:
            continue
        await asyncio.sleep(0.35)
        group = (groups.get("groups") if isinstance(groups, dict) else groups)[0]
        hits.append((name, group))
        print(f"  {OK} vk.com/{name} — «{group.get('name')}», "
              f"{group.get('members_count', '?')} участников, "
              f"{'закрытая' if group.get('is_closed') else 'открытая'}")
    if not hits:
        print(f"  {FAIL} ни один кандидат не существует — по решению ревью статус dead")


async def verify_vk(cfg) -> None:
    from tgtraff.collector.vk_collector import VkApi, VkError

    print("\n=== ВК-паблики ===")
    if not cfg.vk_token:
        print(f"  {WARN} нет VK_SERVICE_TOKEN в .env — проверка пропущена")
        return

    api = VkApi(token=cfg.vk_token, api_version=cfg.vk.api_version)
    for city in cfg.active():
        for spec in city.vk_sources:
            label = spec.key or f"«{spec.query}»"
            print(f"\n{city.slug}: {label}")
            try:
                if spec.key:                       # адрес известен — проверяем, что он живой
                    groups = await api.call("groups.getById", group_ids=spec.key,
                                            fields="members_count,is_closed")
                    group = (groups.get("groups") if isinstance(groups, dict) else groups)[0]
                    print(f"  {OK} {group.get('name')} — {group.get('members_count', '?')} участников, "
                          f"{'закрытая' if group.get('is_closed') else 'открытая'}")
                else:                              # адреса нет — подбираем
                    await _resolve_by_candidates(api, spec)
            except VkError as e:
                print(f"  {FAIL} {e}")
            await asyncio.sleep(0.4)               # 3 запроса/с у сервисного ключа


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rss", action="store_true")
    ap.add_argument("--tg", action="store_true")
    ap.add_argument("--vk", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--include-verified", action="store_true",
                    help="проверять и уже подтверждённые адреса, не только resolve")
    args = ap.parse_args()

    setup_logging("WARNING")
    cfg = load()
    if args.all or args.rss:
        await verify_rss(cfg, only_unresolved=not args.include_verified)
    if args.all or args.vk:
        await verify_vk(cfg)
    if args.all or args.tg:
        await verify_tg(cfg)


if __name__ == "__main__":
    asyncio.run(main())
