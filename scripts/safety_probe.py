#!/usr/bin/env python
"""Приёмка safety-фильтра на наборе провокаций (спека, фаза 4).

    .venv/bin/python scripts/safety_probe.py                # словарь + классификатор
    .venv/bin/python scripts/safety_probe.py --rules-only   # без ключа и без денег
    .venv/bin/python scripts/safety_probe.py --out docs/reports/safety-probe.md

Набор — `config/safety_probe.yaml` (20 постов: прямые нарушения, пограничные
случаи и обычные ЧП, которые фильтр обязан пропустить). Провал — это когда
ожидалось `reject`/`manual_only`, а фильтр сказал `safe`: в обратную сторону
строгость допустима, лишний драфт модератор закроет кликом.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load, setup_logging  # noqa: E402
from tgtraff.llm.client import LlmClient  # noqa: E402
from tgtraff.safety import rules  # noqa: E402
from tgtraff.safety.checker import SafetyChecker  # noqa: E402
from tgtraff.safety.probe import load_probes  # noqa: E402

ORDER = {"safe": 0, "manual_only": 1, "reject": 2}


def verdict_of(probe, checker):
    if checker is None:
        v = rules.check_text(probe.text)
        return v.verdict, v.categories, "словарь", 0.0
    result = checker.check(probe.text)
    return result.verdict, result.categories, result.by, result.cost_usd


def main() -> None:
    ap = argparse.ArgumentParser(description="Прогон провокаций через safety-фильтр")
    ap.add_argument("--rules-only", action="store_true", help="только стоп-словарь")
    ap.add_argument("--out", help="записать отчёт в md-файл")
    ap.add_argument("--path", help="свой файл набора (по умолчанию config/safety_probe.yaml)")
    args = ap.parse_args()

    setup_logging("WARNING")
    cfg = load()
    checker = None
    if not args.rules_only:
        client = LlmClient(cfg.llm, api_key=cfg.anthropic_key)
        checker = SafetyChecker(client, cfg.safety)

    probes = load_probes(Path(args.path) if args.path else None)
    rows, failures, cost = [], [], 0.0
    for probe in probes:
        verdict, categories, by, spent = verdict_of(probe, checker)
        cost += spent
        # Провал — только смягчение: строже ожидаемого допустимо.
        ok = ORDER[verdict] >= ORDER[probe.expected]
        exact = verdict == probe.expected
        if not ok:
            failures.append((probe, verdict))
        rows.append({"probe": probe, "verdict": verdict, "categories": categories,
                     "by": by, "ok": ok, "exact": exact})

    print(f"{'кейс':<26} {'ожидали':<12} {'получили':<12} итог")
    for r in rows:
        mark = "OK " if r["exact"] else ("строже" if r["ok"] else "ПРОВАЛ")
        print(f"{r['probe'].id:<26} {r['probe'].expected:<12} {r['verdict']:<12} {mark}"
              f"  {', '.join(r['categories'])[:40]}")
    exact = sum(1 for r in rows if r["exact"])
    print(f"\nсовпало точно: {exact}/{len(rows)}; "
          f"не пропущено ни одной провокации: {'да' if not failures else 'НЕТ'}")
    if cost:
        print(f"потрачено: ${cost:.4f}")
    if failures:
        print("\nпровалы (фильтр мягче ожидаемого):")
        for probe, verdict in failures:
            print(f"  {probe.id}: ждали {probe.expected}, получили {verdict}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(render(rows, cost, failures), "utf-8")
        print(f"\nотчёт: {args.out}")
    sys.exit(1 if failures else 0)


def render(rows, cost, failures) -> str:
    out = ["# Приёмка safety-фильтра на наборе провокаций", "",
           f"Кейсов: {len(rows)}. Точных совпадений: "
           f"{sum(1 for r in rows if r['exact'])}. "
           f"Пропущенных провокаций: {len(failures)}.",
           f"Потрачено на модель: ${cost:.4f}." if cost else "", "",
           "| кейс | ожидали | получили | кто решил | категории | что это |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        mark = "" if r["exact"] else (" ⚠️" if r["ok"] else " ❌")
        out.append(f"| `{r['probe'].id}` | {r['probe'].expected} | {r['verdict']}{mark} | "
                   f"{r['by']} | {', '.join(r['categories']) or '—'} | {r['probe'].note} |")
    out += ["", "Легенда: ⚠️ — фильтр строже ожидаемого (допустимо), "
            "❌ — фильтр мягче ожидаемого (провал приёмки).", ""]
    for r in rows:
        out += [f"### {r['probe'].id} — {r['probe'].expected} → {r['verdict']}", "",
                "> " + r["probe"].text, ""]
    return "\n".join(out)


if __name__ == "__main__":
    main()
