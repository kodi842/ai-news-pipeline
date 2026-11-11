#!/usr/bin/env python
"""Калибровка дедупа на живой ленте (спека §3: «порог калибруется, старт 0.85»).

Работает на КОПИИ базы: боевые кластеры не трогаются, коллектор продолжает писать.

    # что даёт каждый порог
    .venv/bin/python scripts/dedup_calibrate.py --sweep 0.78,0.82,0.85,0.88,0.92

    # все склейки одного прогона глазами (для оценки ложных)
    .venv/bin/python scripts/dedup_calibrate.py --threshold 0.85 --merges

    # что ловят предохранители: рубрики, шаблонные сводки, цепочки
    .venv/bin/python scripts/dedup_calibrate.py --threshold 0.85 --no-guards --merges
"""
import argparse
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load, setup_logging  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.dedup.cluster import Deduplicator  # noqa: E402
from tgtraff.dedup.embed import LocalEmbedder  # noqa: E402
from tgtraff.dedup.runner import run_once  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def copy_db(src: Path) -> Path:
    """Копия базы вместе с WAL: иначе свежие посты в копию не попадут."""
    tmp = Path(tempfile.mkdtemp(prefix="dedup-calib-")) / "calib.db"
    shutil.copy(src, tmp)
    for suffix in ("-wal", "-shm"):
        extra = Path(str(src) + suffix)
        if extra.exists():
            shutil.copy(extra, str(tmp) + suffix)
    return tmp


def reset(db) -> None:
    db.conn.execute("DELETE FROM cluster_members")
    db.conn.execute("DELETE FROM clusters")
    db.conn.execute("DELETE FROM post_signatures")


def run(db, settings, embedder, cities, since, limit):
    reset(db)
    dedup = Deduplicator(db, embedder, settings)
    totals = {}
    for city in cities:
        stats = run_once(db, dedup, city=city, limit=limit, since=since)
        totals[city] = stats
    return totals


def metrics(db) -> dict:
    c = db.conn
    one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "кластеров": one("SELECT COUNT(*) FROM clusters"),
        "постов": one("SELECT COUNT(*) FROM cluster_members"),
        "склеек": one("SELECT COUNT(*) FROM cluster_members WHERE reason <> 'new'"),
        "кластеров 2+": one("SELECT COUNT(*) FROM clusters WHERE size > 1"),
        "кластеров 3+": one("SELECT COUNT(*) FROM clusters WHERE size > 2"),
        "макс. размер": one("SELECT COALESCE(MAX(size),0) FROM clusters"),
        "кросс-платформенных": one("""
            SELECT COUNT(*) FROM clusters cl WHERE (
                SELECT COUNT(DISTINCT p.source_type) FROM cluster_members m
                JOIN raw_posts p ON p.id = m.raw_post_id WHERE m.cluster_id = cl.id) > 1"""),
        "семантических": one("SELECT COUNT(*) FROM cluster_members WHERE reason='semantic'"),
        "по картинке": one("SELECT COUNT(*) FROM cluster_members WHERE reason='phash'"),
        "точных": one("SELECT COUNT(*) FROM cluster_members WHERE reason='exact_text'"),
    }


def print_merges(db, limit_text: int = 110) -> None:
    rows = db.conn.execute("""
        SELECT cl.id, cl.size, cl.city, m.reason, m.similarity, s.key src, p.source_type,
               p.published_at, substr(replace(replace(p.text,char(10),' '),char(13),' '),1,?) t
        FROM clusters cl JOIN cluster_members m ON m.cluster_id = cl.id
        JOIN raw_posts p ON p.id = m.raw_post_id JOIN sources s ON s.id = p.source_id
        WHERE cl.size > 1 ORDER BY cl.city, cl.id, m.added_at""", (limit_text,)).fetchall()
    current = None
    for r in rows:
        if r["id"] != current:
            current = r["id"]
            print(f"\n--- #{r['id']} {r['city']} n={r['size']}")
        sim = f"{r['similarity']:.2f}" if r["similarity"] is not None else "  — "
        print(f" {r['reason'][:4]:<4} {sim} {r['src'][:18]:<18} {r['published_at'][5:16]} {r['t']}")


def print_pairs(db, reason: str = "semantic", chars: int = 150) -> None:
    """Пары «пост → к чему присоединили», от самых похожих к менее похожим.

    Это и есть материал ручной оценки: по каждой паре видно, одна ли новость,
    а порядок по similarity показывает, где проходит граница правды."""
    rows = db.conn.execute("""
        SELECT m.similarity, m.cluster_id, sa.key src_a, sb.key src_b,
               substr(replace(replace(a.text,char(10),' '),char(13),' '),1,?) ta,
               substr(replace(replace(b.text,char(10),' '),char(13),' '),1,?) tb,
               a.published_at pa, b.published_at pb
        FROM cluster_members m
        JOIN raw_posts a ON a.id = m.raw_post_id
        JOIN raw_posts b ON b.id = m.matched_raw_post_id
        JOIN sources sa ON sa.id = a.source_id
        JOIN sources sb ON sb.id = b.source_id
        WHERE m.reason = ? ORDER BY m.similarity DESC""", (chars, chars, reason)).fetchall()
    print(f"\nпар «{reason}»: {len(rows)}")
    for i, r in enumerate(rows, 1):
        print(f"\n[{i:>3}] {r['similarity']:.3f}  #{r['cluster_id']}")
        print(f"      A {r['src_b'][:20]:<20} {r['pb'][5:16]} {r['tb']}")
        print(f"      B {r['src_a'][:20]:<20} {r['pa'][5:16]} {r['ta']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Калибровка порога дедупа на живой ленте")
    ap.add_argument("--since", default=None, help="ISO-дата начала окна (по умолчанию — двое суток)")
    ap.add_argument("--city", help="только один город")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--sweep", help="пороги через запятую")
    ap.add_argument("--threshold", type=float, help="один порог")
    ap.add_argument("--merges", action="store_true", help="показать все склейки")
    ap.add_argument("--pairs", metavar="REASON", nargs="?", const="semantic",
                    help="показать пары склеек одного типа (semantic|phash|exact_text)")
    ap.add_argument("--no-guards", action="store_true",
                    help="выключить предохранители (рубрики, имена, центроид)")
    ap.add_argument("--entity-min", type=int, help="минимум имён для проверки (по умолчанию из конфига)")
    args = ap.parse_args()

    setup_logging("WARNING")
    cfg = load()
    since = args.since or "2026-08-31T19:00:00+00:00"
    cities = [args.city] if args.city else list(cfg.active_cities)

    base = cfg.dedup
    if args.no_guards:
        base = replace(base, same_source_daily_guard=False, entity_guard=False,
                       centroid_margin=1.0)
    if args.entity_min is not None:
        base = replace(base, entity_guard_min=args.entity_min)

    src = copy_db(cfg.paths.db)
    db = Database(src)
    embedder = LocalEmbedder(base, root=ROOT)

    thresholds = ([float(t) for t in args.sweep.split(",")] if args.sweep
                  else [args.threshold or base.threshold])

    print(f"лента: с {since}, города: {', '.join(cities)}")
    print(f"модель: {base.model}")
    print(f"предохранители: {'ВЫКЛЮЧЕНЫ' if args.no_guards else 'включены'}\n")

    rows = []
    for threshold in thresholds:
        settings = replace(base, threshold=threshold)
        run(db, settings, embedder, cities, since, args.limit)
        rows.append((threshold, metrics(db)))

    keys = list(rows[0][1])
    width = max(len(k) for k in keys) + 2
    print(f"{'показатель':<{width}}" + "".join(f"{t:>10.2f}" for t, _ in rows))
    for key in keys:
        print(f"{key:<{width}}" + "".join(f"{m[key]:>10}" for _, m in rows))

    if args.merges:
        print_merges(db)
    if args.pairs:
        print_pairs(db, args.pairs)

    db.close()
    shutil.rmtree(src.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
