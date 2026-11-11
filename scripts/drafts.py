#!/usr/bin/env python
"""Фаза 4: кластеры → драфты (relevance → rewriter → safety).

    .venv/bin/python scripts/drafts.py --run --limit 30          # разобрать очередь
    .venv/bin/python scripts/drafts.py --run --city cherepovets --budget 1
    .venv/bin/python scripts/drafts.py --report --out docs/reports/drafts.md
    .venv/bin/python scripts/drafts.py --status                  # что получилось и почём
    .venv/bin/python scripts/drafts.py --dry-run --limit 20      # без единого запроса к API

Ключ берётся из .env (ANTHROPIC_API_KEY). `--budget` — предохранитель в долларах:
процесс остановится сам, не дожидаясь конца очереди.
"""
import argparse
import logging
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff import drafts as store  # noqa: E402
from tgtraff.app import load, setup_logging, sync_sources  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.llm.client import LlmClient, LlmError  # noqa: E402
from tgtraff.pipeline import Phase4Pipeline, PipelineSettings  # noqa: E402
from tgtraff.rewriter.report import collect_report, render_markdown  # noqa: E402

log = logging.getLogger("tgtraff.drafts")


def build_pipeline(cfg, db, args) -> Phase4Pipeline:
    llm = cfg.llm
    if args.budget:
        llm = replace(llm, budget_usd=args.budget)
    if args.model:
        llm = replace(llm, model=args.model)
    client = LlmClient(llm, api_key=cfg.anthropic_key)

    relevance = cfg.relevance
    rewriter = cfg.rewriter
    safety = cfg.safety
    if args.no_relevance_llm:
        relevance = replace(relevance, use_llm=False)
    if args.no_safety_llm:
        safety = replace(safety, use_llm=False)

    settings = PipelineSettings(relevance=relevance, rewriter=rewriter, safety=safety,
                                media=cfg.media, vision_model=llm.vision_model)
    names = {slug: city.name for slug, city in cfg.cities.items()}
    return Phase4Pipeline(db, client, settings=settings, city_names=names)


def show_status(db) -> None:
    total = db.conn.execute("SELECT COUNT(*) n FROM drafts").fetchone()["n"]
    print(f"драфтов: {total}")
    if total:
        print("\nпо вердикту safety:")
        for r in db.conn.execute("""SELECT COALESCE(safety_verdict,'—') v, COUNT(*) n
                                    FROM drafts GROUP BY v ORDER BY n DESC"""):
            print(f"  {r['v']:<12} {r['n']}")
        print("\nпо категориям:")
        for r in db.conn.execute("""SELECT COALESCE(category,'—') c, COUNT(*) n
                                    FROM drafts GROUP BY c ORDER BY n DESC"""):
            print(f"  {r['c']:<12} {r['n']}")
        print("\nпо медиа:")
        for r in db.conn.execute("""SELECT COALESCE(media_kind,'—') k,
                                           COALESCE(media_reason,'—') r, COUNT(*) n
                                    FROM drafts GROUP BY k, r ORDER BY n DESC"""):
            print(f"  {r['k']:<10} {r['r']:<12} {r['n']}")

    print("\nотсев relevance (кластеры):")
    for r in db.conn.execute("""SELECT relevance, COUNT(*) n FROM clusters
                                WHERE relevance IS NOT NULL GROUP BY relevance
                                ORDER BY n DESC"""):
        print(f"  {r['relevance']:<12} {r['n']}")

    usage = store.usage_total(db)
    print(f"\nпотрачено на модель: ${usage['cost_usd']:.4f} за {usage['calls']} запросов")
    for row in store.usage_by_purpose(db):
        cached = f", из кэша {row['cache_read']}" if row["cache_read"] else ""
        print(f"  {row['purpose']:<10} {row['calls']:>4} запр., "
              f"вход {row['input_tokens']}{cached}, выход {row['output_tokens']}, "
              f"${row['cost_usd']:.4f}")


def within_daily_budget(db, limit: float) -> bool:
    """Дневной предохранитель автозапуска.

    `--budget` ограничивает ОДИН проход, а под таймером проходов сотни в сутки:
    без суточного потолка сорвавшийся цикл выпишет счёт за месяц за ночь.
    Считаем по факту из `llm_usage`, а не по памяти процесса — процесс тут
    живёт минуту."""
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    spent = store.usage_total(db, since)["cost_usd"]
    if spent < limit:
        return True
    log.warning("за сегодня потрачено $%.2f при суточном лимите $%.2f — проход пропущен",
                spent, limit)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Драфты фазы 4 (rewriter + safety)")
    ap.add_argument("--run", action="store_true", help="разобрать очередь кластеров")
    ap.add_argument("--report", action="store_true", help="собрать md для разметки")
    ap.add_argument("--status", action="store_true", help="что получилось и почём")
    ap.add_argument("--city", help="только один город")
    ap.add_argument("--limit", type=int, default=30, help="кластеров за проход")
    ap.add_argument("--since", help="только кластеры свежее ISO-даты")
    ap.add_argument("--min-size", type=int, default=1, help="кластеры от N постов")
    ap.add_argument("--budget", type=float, help="предохранитель, $ на этот прогон")
    ap.add_argument("--daily-budget", type=float, metavar="USD",
                    help="не начинать проход, если за сегодня уже потрачено больше "
                         "(предохранитель автозапуска: спека даёт $1.2-2/день)")
    ap.add_argument("--model", help="переопределить модель")
    ap.add_argument("--out", help="файл отчёта (--report)")
    ap.add_argument("--dry-run", action="store_true",
                    help="только правила: LLM не вызывается, драфты не пишутся")
    ap.add_argument("--no-relevance-llm", action="store_true")
    ap.add_argument("--no-safety-llm", action="store_true")
    args = ap.parse_args()

    setup_logging()
    cfg = load()
    db = Database(cfg.paths.db)

    if args.status and not (args.run or args.report):
        show_status(db)
        return

    if args.dry_run:
        dry_run(db, cfg, args)
        return

    if args.run:
        if args.daily_budget and not within_daily_budget(db, args.daily_budget):
            return
        # Признак official живёт в sources.yaml — без синхронизации медиа-каскад
        # не увидит ведомства (спека §5, ступень 1).
        sync_sources(cfg, db)
        pipeline = build_pipeline(cfg, db, args)
        cities = [args.city] if args.city else list(cfg.active_cities)
        for city in cities:
            try:
                stats = pipeline.run_once(city=city, limit=args.limit, since=args.since,
                                          min_size=args.min_size)
            except LlmError as exc:
                print(f"остановлено: {exc}")
                break
            print(f"{city}: {stats.line()}")
        print("\n" + pipeline.client.report())

    if args.report:
        data = collect_report(db, city=args.city, limit=args.limit, since=args.since)
        text = render_markdown(data)
        if args.out:
            path = Path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, "utf-8")
            print(f"отчёт: {path} ({len(data['drafts'])} драфтов)")
        else:
            print(text)

    if args.status:
        print()
        show_status(db)


def dry_run(db, cfg, args) -> None:
    """Прогон правил без модели: видно, что отсеется бесплатно и на чём."""
    from tgtraff.relevance import rules
    from tgtraff.rewriter.material import collect_material

    cities = [args.city] if args.city else list(cfg.active_cities)
    for city in cities:
        counts: dict[str, int] = {}
        low_facts = 0
        ids = store.pending_clusters(db, city=city, limit=args.limit, since=args.since,
                                     min_size=args.min_size)
        for cluster_id in ids:
            material = collect_material(db, cluster_id)
            verdicts = [rules.check_post(p.raw_text, marked_as_ads=p.marked_as_ads)
                        for p in material.posts]
            drop = bool(verdicts) and all(v.is_drop for v in verdicts)
            key = verdicts[0].reason if drop else "в работу"
            counts[key] = counts.get(key, 0) + 1
            if not drop and material.is_low_facts:
                low_facts += 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "—"
        print(f"{city}: кластеров {len(ids)} ({summary}); из них low_facts: {low_facts}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлен оператором")
