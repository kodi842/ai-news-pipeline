#!/usr/bin/env python
"""Дедуп ленты: склейка одной новости из разных источников в кластер (спека §3).

    .venv/bin/python scripts/dedup.py --backfill            # разобрать накопленное
    .venv/bin/python scripts/dedup.py --watch               # фоновый режим
    .venv/bin/python scripts/dedup.py --status              # что получилось
    .venv/bin/python scripts/dedup.py --reset --yes         # снести кластеры и начать заново

Отдельный процесс от коллектора: модель эмбеддингов занимает память и считает
на CPU, а коллектор обязан оставаться отзывчивым. Общаются через базу.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load, setup_logging  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.dedup.cluster import Deduplicator  # noqa: E402
from tgtraff.dedup.embed import LocalEmbedder  # noqa: E402
from tgtraff.dedup.runner import close_stale, run_once, watch  # noqa: E402
from tgtraff.dedup.settings import DedupSettings  # noqa: E402

log = logging.getLogger("tgtraff.dedup")


def build(cfg, args) -> tuple[DedupSettings, LocalEmbedder]:
    settings = cfg.dedup
    if args.threshold is not None:
        settings = replace_setting(settings, threshold=args.threshold)
    if args.model:
        settings = replace_setting(settings, model=args.model)
    return settings, LocalEmbedder(settings, root=Path(__file__).resolve().parents[1])


def replace_setting(settings: DedupSettings, **kw) -> DedupSettings:
    from dataclasses import replace
    return replace(settings, **kw)


def show_status(db) -> None:
    c = db.conn
    total = c.execute("SELECT COUNT(*) n FROM clusters").fetchone()["n"]
    members = c.execute("SELECT COUNT(*) n FROM cluster_members").fetchone()["n"]
    print(f"кластеров: {total}, постов в них: {members}")
    if not total:
        return

    print("\nразмеры кластеров:")
    for r in c.execute("""SELECT size, COUNT(*) n FROM clusters GROUP BY size ORDER BY size"""):
        print(f"  {r['size']} пост(ов): {r['n']} кластеров")

    print("\nсклейки по причине:")
    for r in c.execute("""SELECT reason, COUNT(*) n FROM cluster_members
                          WHERE reason <> 'new' GROUP BY reason ORDER BY n DESC"""):
        print(f"  {r['reason']:<12} {r['n']}")

    print("\nкросс-платформенные кластеры (критерий приёмки фазы 3):")
    for r in c.execute("""
        SELECT cl.city, COUNT(*) n FROM clusters cl
        WHERE (SELECT COUNT(DISTINCT p.source_type) FROM cluster_members m
               JOIN raw_posts p ON p.id = m.raw_post_id WHERE m.cluster_id = cl.id) >= 2
        GROUP BY cl.city ORDER BY n DESC"""):
        print(f"  {r['city']:<15} {r['n']}")

    print("\nсамые крупные кластеры:")
    for r in c.execute("""
        SELECT cl.id, cl.city, cl.size, p.text,
               (SELECT GROUP_CONCAT(DISTINCT s2.key) FROM cluster_members m2
                JOIN raw_posts p2 ON p2.id = m2.raw_post_id
                JOIN sources s2 ON s2.id = p2.source_id WHERE m2.cluster_id = cl.id) srcs
        FROM clusters cl JOIN raw_posts p ON p.id = cl.lead_raw_post_id
        ORDER BY cl.size DESC, cl.id LIMIT 10"""):
        text = " ".join((r["text"] or "").split())[:80]
        print(f"  #{r['id']:<5} {r['city']:<14} {r['size']} постов  {text}")
        print(f"         источники: {r['srcs']}")


def reset(db) -> None:
    db.conn.execute("DELETE FROM cluster_members")
    db.conn.execute("DELETE FROM clusters")
    db.conn.execute("DELETE FROM post_signatures")
    print("кластеры, участники и подписи удалены (медиа-хэши остались — они не зависят от порога)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Дедуп ленты (фаза 3)")
    ap.add_argument("--backfill", action="store_true", help="разобрать накопленную ленту")
    ap.add_argument("--watch", action="store_true", help="фоновый режим")
    ap.add_argument("--status", action="store_true", help="показать состояние кластеров")
    ap.add_argument("--reset", action="store_true", help="удалить кластеры и подписи")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждения у --reset")
    ap.add_argument("--city", help="только один город")
    ap.add_argument("--since", help="только посты свежее ISO-даты")
    ap.add_argument("--limit", type=int, default=2000, help="постов за проход")
    ap.add_argument("--threshold", type=float, help="переопределить cosine-порог")
    ap.add_argument("--model", help="переопределить модель эмбеддингов")
    args = ap.parse_args()

    setup_logging()
    cfg = load()
    db = Database(cfg.paths.db)

    if args.reset:
        if not args.yes:
            raise SystemExit("--reset удаляет все кластеры. Повторите с --yes")
        reset(db)
        if not (args.backfill or args.watch or args.status):
            return

    if args.status and not (args.backfill or args.watch):
        show_status(db)
        return

    settings, embedder = build(cfg, args)
    dedup = Deduplicator(db, embedder, settings)
    cities = [args.city] if args.city else list(cfg.active_cities)
    log.info("порог %.2f, окно %s ч, модель %s", settings.threshold,
             settings.window_hours, settings.model)

    if args.watch:
        watch(db, dedup, cities=cities)
        return

    if args.backfill:
        for city in cities:
            stats = run_once(db, dedup, city=city, limit=args.limit, since=args.since)
            print(f"{city}: {stats.line()}")
        closed = close_stale(db, dedup)
        if closed:
            print(f"закрыто кластеров вне окна: {closed}")
        if args.status:
            print()
            show_status(db)
        return

    ap.print_help()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлен оператором")
