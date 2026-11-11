#!/usr/bin/env python
"""Что лежит в базе: посты по источникам, свежесть, состояние медиа, лента конвейера.
Используется для проверки критериев приёмки фаз 1 и 2.

    .venv/bin/python scripts/status.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.normalize import normalized_feed  # noqa: E402


def main() -> None:
    cfg = load()
    db = Database(cfg.paths.db)
    c = db.conn

    total = db.count_raw_posts()
    print(f"БД: {cfg.paths.db}")
    print(f"raw_posts: {total}\n")

    print("по типам источников (критерий приёмки фазы 2 — единая таблица):")
    for r in c.execute("""
        SELECT p.source_type, COUNT(*) n, COUNT(DISTINCT p.source_id) srcs,
               SUM(p.has_media) media, MAX(p.published_at) last
        FROM raw_posts p GROUP BY p.source_type ORDER BY n DESC
    """):
        print(f"  {r['source_type']:<12} {r['n']:>6} постов из {r['srcs']:>2} источников, "
              f"с медиа {r['media'] or 0:>5}, свежий {(r['last'] or '—')[:16]}")
    print()

    print(f"{'город':<14} {'источник':<22} {'подп':>5} {'постов':>7} {'фильтр':>7} {'последний пост (UTC)':>21}")
    for r in c.execute("""
        SELECT s.city, s.key, s.joined_at, COUNT(p.id) n, SUM(p.filtered_out) f, MAX(p.published_at) last
        FROM sources s LEFT JOIN raw_posts p ON p.source_id = s.id
        WHERE s.source_type IN ('tg','tg_regional') AND s.status = 'verified'
        GROUP BY s.id ORDER BY n DESC, s.city
    """):
        mark = "да" if r["joined_at"] else "—"
        print(f"{r['city']:<14} @{r['key']:<21} {mark:>5} {r['n']:>7} {r['f'] or 0:>7} "
              f"{(r['last'] or '—')[:19]:>21}")

    print("\nисточники ВК/RSS (поллинг):")
    for r in c.execute("""
        SELECT s.source_type, s.key, s.city, COUNT(p.id) n, MAX(p.published_at) last,
               s.last_polled_at, s.last_error
        FROM sources s LEFT JOIN raw_posts p ON p.source_id = s.id
        WHERE s.source_type IN ('vk','rss','official') AND s.status='verified'
        GROUP BY s.id ORDER BY s.source_type, n DESC
    """):
        mark = "❌" if r["last_error"] else ("✅" if r["n"] else "· ")
        key = r["key"].replace("https://", "")[:44]
        print(f"  {mark} {r['source_type']:<9} {key:<45} {r['n']:>4} записей  {(r['last'] or '—')[:16]}")
        if r["last_error"]:
            print(f"       ошибка: {r['last_error'][:90]}")

    print("\nнормализованная лента (склейка альбомов, спека §2):")
    feed = normalized_feed(db, limit=3000)
    albums = [i for i in feed if i.is_album]
    raw_rows = sum(len(i.raw_post_ids) for i in feed)
    print(f"  {raw_rows} строк raw_posts → {len(feed)} логических постов")
    print(f"  альбомов: {len(albums)}, самый большой — {max((len(a.raw_post_ids) for a in albums), default=0)} сообщений")
    by_type = {}
    for item in feed:
        by_type[item.source_type] = by_type.get(item.source_type, 0) + 1
    print(f"  по типам: {', '.join(f'{k}={v}' for k, v in sorted(by_type.items()))}")

    print("\nмедиа:")
    for r in c.execute("SELECT status, COUNT(*) n, SUM(COALESCE(file_size,0)) b FROM raw_media GROUP BY status"):
        print(f"  {r['status']:<10} {r['n']:>5} файлов, {(r['b'] or 0) / 1024 / 1024:.1f} МБ")

    print("\nмедиа-состояния постов:")
    for r in c.execute("SELECT media_state, COUNT(*) n FROM raw_posts GROUP BY media_state"):
        print(f"  {r['media_state']:<10} {r['n']:>5}")

    # Критерий приёмки фазы 1: пост попадает в raw_posts в течение минуты после публикации.
    # Считаем только via='live' — у догона лаг измеряется сутками и критерий не про него.
    print("\nдоставка ТГ: подписки и поллинг-фолбэк прогрева (QUESTIONS §8):")
    warm = c.execute("""SELECT COUNT(*) n, SUM(joined_at IS NOT NULL) joined FROM sources
                        WHERE source_type IN ('tg','tg_regional') AND status='verified'""").fetchone()
    print(f"  подписано {warm['joined'] or 0} из {warm['n']} — "
          f"{'фолбэк ещё нужен' if (warm['joined'] or 0) < warm['n'] else 'фолбэк выключается сам'}")
    for r in c.execute("SELECT via, COUNT(*) n FROM raw_posts GROUP BY via ORDER BY n DESC"):
        print(f"  via={r['via']:<8} {r['n']:>6}")

    print("\nлаг «опубликовано → в базе», только событийная доставка (сек):")
    lag = c.execute("""
        SELECT COUNT(*) n,
               MIN(julianday(collected_at) - julianday(published_at)) * 86400 mn,
               AVG(julianday(collected_at) - julianday(published_at)) * 86400 av,
               MAX(julianday(collected_at) - julianday(published_at)) * 86400 mx,
               SUM((julianday(collected_at) - julianday(published_at)) * 86400 <= 60) ok
        FROM raw_posts WHERE via = 'live'
    """).fetchone()
    if lag["n"]:
        print(f"  постов: {lag['n']}, мин {lag['mn']:.0f} / средн {lag['av']:.0f} / макс {lag['mx']:.0f}")
        print(f"  уложились в 60 сек: {lag['ok']} из {lag['n']}")
        print("  источники, поймавшие live-посты:")
        for r in c.execute("""
            SELECT s.key, s.joined_at, COUNT(*) n,
                   AVG((julianday(p.collected_at) - julianday(p.published_at)) * 86400) av
            FROM raw_posts p JOIN sources s ON s.id = p.source_id
            WHERE p.via = 'live'
            GROUP BY s.id ORDER BY n DESC
        """):
            print(f"    @{r['key']:<20} {'подписаны' if r['joined_at'] else 'без подписки':<13} "
                  f"{r['n']:>3} постов, средн {r['av']:.0f} с")
    else:
        print("  пока ни одного — ждём новых публикаций в подписанных источниках")

    dupes = c.execute("""
        SELECT COUNT(*) n FROM (
            SELECT source_id, external_id FROM raw_posts GROUP BY source_id, external_id HAVING COUNT(*) > 1
        )""").fetchone()["n"]
    print(f"\nдубли (source_id, external_id): {dupes}")

    print("\nсвежие 10 постов:")
    for r in c.execute("""
        SELECT s.key, p.external_id, p.published_at, p.collected_at, p.has_media, p.media_state,
               substr(replace(COALESCE(p.text,''), char(10), ' '), 1, 60) t
        FROM raw_posts p JOIN sources s ON s.id=p.source_id
        ORDER BY p.collected_at DESC, p.id DESC LIMIT 10
    """):
        media = f" [медиа:{r['media_state']}]" if r["has_media"] else ""
        print(f"  {r['published_at'][:19]} → собран {r['collected_at']} @{r['key']}/{r['external_id']}{media}\n      {r['t']}")
    db.close()


if __name__ == "__main__":
    main()
