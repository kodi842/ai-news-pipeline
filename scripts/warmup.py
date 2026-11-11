#!/usr/bin/env python
"""Плавный прогрев юзербота: подписка на источники из config/sources.yaml
по 5-10 каналов в день, случайные паузы, только человеческие часы.

    .venv/bin/python scripts/warmup.py           # долгоживущий процесс
    .venv/bin/python scripts/warmup.py --once    # одна подписка и выход (для cron)
    .venv/bin/python scripts/warmup.py --status  # что уже сделано
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import (  # noqa: E402
    build_client, ensure_authorized, load, require_free_session, setup_logging,
)
from tgtraff.db import Database  # noqa: E402
from tgtraff.warmup import WarmupPlanner, run_warmup  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="одна подписка и выход")
    ap.add_argument("--status", action="store_true", help="показать состояние прогрева и выйти")
    args = ap.parse_args()

    setup_logging()
    cfg = load()
    db = Database(cfg.paths.db)

    if args.status:
        planner = WarmupPlanner(db, cfg.warmup)
        day = planner.today()
        plan = planner.day_plan(day)
        print(f"день {day}: квота {plan['quota']}, сделано {plan['done']}")
        print("состояние целей:", db.warmup_counts() or "очередь пуста")

        invites = db.conn.execute(
            "SELECT COUNT(*) c FROM warmup_state WHERE kind='invite' AND status='pending'"
        ).fetchone()["c"]
        if invites:
            if planner.invites_allowed():
                print(f"инвайты: {invites} в очереди, карантин пройден — идут в общую квоту")
            else:
                opens = planner.invites_open_at()
                print(f"инвайты: {invites} в очереди, карантин после перелогина — "
                      f"откроются через {planner.invites_open_in_hours():.1f} ч "
                      f"({opens:%d.%m %H:%M} UTC)")
        for row in db.conn.execute(
            "SELECT s.city, w.target, w.kind, w.status, w.joined_at, w.last_error "
            "FROM warmup_state w JOIN sources s ON s.id=w.source_id ORDER BY w.status, s.city"
        ):
            target = row["target"]
            label = target if row["kind"] == "invite" else f"@{target}"
            print(f"  {row['status']:<15} {row['city']:<13} {label:<42} "
                  f"{row['joined_at'] or ''} {row['last_error'] or ''}")
        db.close()
        return

    require_free_session(cfg)
    client = build_client(cfg)
    await client.connect()
    await ensure_authorized(client, cfg)
    try:
        await run_warmup(client, db, cfg, once=args.once)
    finally:
        await client.disconnect()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
