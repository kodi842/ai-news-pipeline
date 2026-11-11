#!/usr/bin/env python
"""Завод коротких видео (docs/VIDEO-SPEC.md, фаза V1).

    .venv/bin/python scripts/clips.py                    # рабочий цикл
    .venv/bin/python scripts/clips.py --once             # один проход и выход
    .venv/bin/python scripts/clips.py --plan-only        # только скор+сценарий
    .venv/bin/python scripts/clips.py --render-only      # только собрать очередь
    .venv/bin/python scripts/clips.py --status           # что в очереди клипов
    .venv/bin/python scripts/clips.py --candidates       # кто мог бы стать клипом
    .venv/bin/python scripts/clips.py --draft 911        # клип по конкретному драфту
    .venv/bin/python scripts/clips.py --daily-budget 1.0 # предохранитель по деньгам
    .venv/bin/python scripts/clips.py --once --any-status --max-age-hours 240 \
        --limit 10 --render-limit 10      # калибровочный прогон по живой ленте

Что делает проход:
  1. берёт опубликованные safe-драфты без клипа (свежие, фавориты вперёд);
  2. клип-фильтр строже канального — жесть и сомнительная фактура выбывают;
  3. выбирает медиа: чужой знак в клип не идёт, замены обложкой у клипа нет;
  4. LLM: виральность 1-10, затем сценарий (хук ≤6 слов, факты, CTA);
  5. рендер ffmpeg очередью, ПАРАЛЛЕЛЬНОСТЬ 1;
  6. готовый mp4 ждёт карточки — её отправляет процесс модерации.

Публикация в V1 ручная: клип уходит на площадки руками модератора (осознанный
шаг спеки — сначала смотрим качество глазами).
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff import drafts as drafts_store  # noqa: E402
from tgtraff.app import load, setup_logging  # noqa: E402
from tgtraff.clips import clipfilter, store as clip_store  # noqa: E402
from tgtraff.clips.builder import ClipBuilder  # noqa: E402
from tgtraff.clips.factory import ClipFactory  # noqa: E402
from tgtraff.clips.score import ClipScorer  # noqa: E402
from tgtraff.clips.script import ClipScriptWriter  # noqa: E402
from tgtraff.clips.speech import Transcriber, Voice  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.llm.client import LlmClient, LlmError  # noqa: E402

log = logging.getLogger("tgtraff.clips")


def build_factory(cfg, db, client) -> ClipFactory:
    """Завод целиком. Детектор чужих знаков — тот же vision-Haiku, что смотрит
    медиа кластера: без него видео уйдёт с пометкой «знак не проверен»."""
    from tgtraff.rewriter.watermark import WatermarkDetector

    detector = WatermarkDetector(client, model=cfg.llm.vision_model) if client else None
    builder = ClipBuilder(cfg.clips,
                          transcriber=Transcriber(cfg.clips),
                          voice=Voice(cfg.clips))
    return ClipFactory(
        db, cfg.clips,
        scorer=ClipScorer(client, cfg.clips) if client else None,
        writer=ClipScriptWriter(client, cfg.clips) if client else None,
        builder=builder, detector=detector,
        city_names={slug: city.name for slug, city in cfg.cities.items()},
        usernames={slug: city.tg.username for slug, city in cfg.channels.cities.items()},
        city_tz=cfg.city_tz())


def build_client(cfg, db, daily_budget: float | None):
    """Клиент Haiku с записью расходов в базу (как в фазе 4)."""
    settings = cfg.llm
    if daily_budget:
        from dataclasses import replace

        settings = replace(settings, budget_usd=daily_budget)
    client = LlmClient(settings, api_key=cfg.anthropic_key)
    client.on_usage = lambda purpose, model, usage: drafts_store.log_llm_usage(
        db, purpose, model, None, None, usage.input_tokens, usage.output_tokens,
        usage.cache_read, usage.cache_write, usage.cost_usd)
    return client


def print_status(db, cfg) -> None:
    counts = clip_store.counts(db)
    print("клипы:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "пусто")
    for city in cfg.active_cities:
        made = clip_store.made_today(db, city, cfg.city_tz().get(city, "Europe/Moscow"))
        print(f"  {city:14} сегодня {made}/{cfg.clips.clips_per_day}")
    rows = db.conn.execute(
        """SELECT id, city, status, score, mode, hook, duration_sec, file_size, reject_reason
           FROM clips ORDER BY id DESC LIMIT 15""").fetchall()
    for r in rows:
        size = f"{(r['file_size'] or 0) / 1048576:.1f} МБ" if r["file_size"] else "—"
        print(f"  #{r['id']:<4} {r['city']:14} {r['status']:9} скор {r['score'] or '?':>2} "
              f"{r['mode']:7} {size:>8}  {(r['hook'] or r['reject_reason'] or '')[:60]}")


def print_candidates(db, cfg, factory: ClipFactory, limit: int,
                     any_status: bool = False) -> None:
    """Кто мог бы стать клипом и что мешает. Без единого платного запроса."""
    rows = factory.candidates(limit=limit, any_status=any_status)
    print(f"кандидатов: {len(rows)}")
    for row in rows:
        verdict = clipfilter.check(row, blocked=cfg.clips.blocked_categories)
        mark = "✔" if verdict.ok else "✘"
        print(f"  {mark} #{row['id']:<5} {row['city']:14} {row['category'] or '?':8} "
              f"{(row['title'] or '')[:52]}")
        if not verdict.ok:
            print(f"      {verdict.reason}")


def one_pass(factory: ClipFactory, args) -> None:
    if not args.render_only:
        factory.rescript_pending()
        factory.plan_clips(any_status=args.any_status)
    if not args.plan_only:
        factory.render_pending(limit=args.render_limit)


def main() -> None:
    p = argparse.ArgumentParser(description="Завод коротких видео (фаза V1)")
    p.add_argument("--once", action="store_true", help="один проход и выход")
    p.add_argument("--plan-only", action="store_true", help="только скоринг и сценарии")
    p.add_argument("--render-only", action="store_true", help="только сборка очереди")
    p.add_argument("--render-limit", type=int, default=3,
                   help="сколько клипов собрать за проход (по одному за раз)")
    p.add_argument("--status", action="store_true", help="показать очередь клипов")
    p.add_argument("--candidates", type=int, nargs="?", const=20, metavar="N",
                   help="показать кандидатов и причины отказа, без запросов к модели")
    p.add_argument("--draft", type=int, help="сделать клип по конкретному драфту")
    p.add_argument("--daily-budget", type=float, default=0.0,
                   help="предохранитель: остановиться, потратив столько $")
    p.add_argument("--any-status", action="store_true",
                   help="КАЛИБРОВКА: брать и неопубликованные драфты (тестовый режим "
                        "каналов; вердикт safety и клип-фильтр не ослабляются)")
    p.add_argument("--max-age-hours", type=int,
                   help="КАЛИБРОВКА: окно свежести новостей вместо clips.max_age_hours")
    p.add_argument("--limit", type=int, help="кандидатов за проход (вместо score_per_pass)")
    p.add_argument("--per-day", type=int,
                   help="КАЛИБРОВКА: дневной лимит клипов на город вместо clips_per_day")
    p.add_argument("--min-score", type=int,
                   help="КАЛИБРОВКА: порог виральности вместо clips.min_score "
                        "(посмотреть, что даёт лента при другом пороге)")
    p.add_argument("--forget-skipped", action="store_true",
                   help="КАЛИБРОВКА: снять записи «пропущено» — драфты снова станут "
                        "кандидатами (в рабочем проходе решение записывается один раз)")
    p.add_argument("--retention", action="store_true",
                   help="снять с диска клипы старше clips.retention_days")
    p.add_argument("--force", action="store_true",
                   help="разовый прогон, даже если clips.enabled: false")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    setup_logging(args.log_level)
    cfg = load()
    if args.max_age_hours or args.limit or args.per_day or args.min_score or args.force:
        from dataclasses import replace

        cfg = replace(cfg, clips=replace(
            cfg.clips, max_age_hours=args.max_age_hours or cfg.clips.max_age_hours,
            score_per_pass=args.limit or cfg.clips.score_per_pass,
            clips_per_day=args.per_day or cfg.clips.clips_per_day,
            min_score=args.min_score or cfg.clips.min_score,
            # --force снимает выключатель на ОДИН прогон: сам завод проверяет
            # `enabled` внутри, и без этой строки флаг только пропускал бы
            # выход из main, а проход всё равно возвращал пустоту.
            enabled=cfg.clips.enabled or args.force))
    db = Database(cfg.paths.db)
    try:
        if args.status:
            print_status(db, cfg)
            return
        if args.retention:
            print(f"снято с диска: {clip_store.purge_old(db, cfg.clips.retention_days)}")
            return
        if args.forget_skipped:
            dropped = db.conn.execute("DELETE FROM clips WHERE status='skipped'").rowcount
            log.info("снято записей «пропущено»: %s — драфты снова кандидаты", dropped)
        if not cfg.clips.enabled and not args.force:
            # Контейнер под `restart: unless-stopped`: выйти сразу — значит
            # уехать в рестарт-луп. Выключенный завод просто спит.
            log.info("завод клипов выключен (clips.enabled: false). Включается одной "
                     "строкой конфига после перевода каналов в mode: live "
                     "(VIDEO-SPEC, «Порядок включения»). Разовый прогон: --force")
            if args.once or args.draft or args.status or args.candidates:
                return
            while True:
                time.sleep(3600)

        client = None
        if cfg.llm.enabled:
            try:
                client = build_client(cfg, db, args.daily_budget)
            except LlmError as exc:
                log.error("нет доступа к модели (%s) — завод работать не может", exc)
                if not args.candidates:
                    raise SystemExit(2)
        factory = build_factory(cfg, db, client)

        if args.candidates:
            print_candidates(db, cfg, factory, args.candidates, any_status=args.any_status)
            return

        log.info("завод клипов: %s клипов/сутки на город, порог виральности %s, "
                 "рендер %s×%s, параллельность %s, файл ≤%s МБ",
                 cfg.clips.clips_per_day, cfg.clips.min_score, cfg.clips.width,
                 cfg.clips.height, cfg.clips.render_parallelism, cfg.clips.max_file_mb)
        log.info("фавориты: %s; площадки (V1 — руками): %s",
                 ", ".join(cfg.clips.favorite_categories), ", ".join(cfg.clips.platforms))

        if args.draft:
            row = drafts_store.get_draft(db, args.draft)
            if row is None:
                raise SystemExit(f"драфта #{args.draft} нет")
            existing = clip_store.clip_for_draft(db, args.draft)
            if existing is not None:
                log.info("у драфта #%s уже есть клип #%s (%s)", args.draft, existing["id"],
                         existing["status"])
            else:
                log.info("результат: %s", factory.make_clip(row))
            factory.render_pending(limit=1)
            print_status(db, cfg)
            return

        if args.once:
            one_pass(factory, args)
            print_status(db, cfg)
            return

        while True:
            try:
                one_pass(factory, args)
            except Exception as exc:              # noqa: BLE001 — цикл не роняем
                log.exception("проход завода клипов упал: %s", exc)
            time.sleep(cfg.clips.poll_seconds)
    except KeyboardInterrupt:
        log.info("остановлено вручную")
    finally:
        db.close()


if __name__ == "__main__":
    main()
