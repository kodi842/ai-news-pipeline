#!/usr/bin/env python
"""Проверочная выборка кластеров в текстовом виде (приёмка фазы 3, спека §3).

Критерий спеки: «ложные склейки < ~5% (ручная проверка выборки 50 кластеров)».

    .venv/bin/python scripts/dedup_sample.py --day 2026-09-02
    .venv/bin/python scripts/dedup_sample.py --day 2026-09-02 --format json --out data/sample.json
    .venv/bin/python scripts/dedup_sample.py --only-merged --limit 100

Выборка случайная, но воспроизводимая: `--seed` фиксирует состав.
Читаемая страница с отметками — `scripts/dedup_report.py`.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.dedup.sample import collect, render_markdown  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Выборка кластеров для ручной проверки")
    ap.add_argument("--day", help="сутки в формате YYYY-MM-DD (по умолчанию — вчера)")
    ap.add_argument("--city", help="только один город")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--only-merged", action="store_true",
                    help="брать только кластеры со склейкой (2+ поста)")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--out", help="файл вывода (по умолчанию — stdout)")
    args = ap.parse_args()

    cfg = load()
    db = Database(cfg.paths.db)
    day = args.day or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    data = collect(db, day=day, limit=args.limit, seed=args.seed,
                   only_merged=args.only_merged, city=args.city)
    text = json.dumps(data, ensure_ascii=False, indent=2) if args.format == "json" \
        else render_markdown(data)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"записано: {args.out} ({len(data['clusters'])} кластеров, "
              f"со склейкой {data['sample_merged']})")
    else:
        print(text)
    db.close()


if __name__ == "__main__":
    main()
