#!/usr/bin/env python
"""Проверка машины перед запуском конвейера (фаза 6).

    .venv/bin/python scripts/preflight.py         # список проверок
    .venv/bin/python scripts/preflight.py --env   # какие переменные видит процесс

Код выхода 1, если не пройдена хотя бы одна ОБЯЗАТЕЛЬНАЯ проверка: systemd-юниты
запускают этот скрипт перед стартом (ExecStartPre), чтобы сервис не поднимался
в заведомо нерабочем окружении.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load, setup_logging  # noqa: E402
from tgtraff.preflight import env_summary, failed, render, run_checks  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Готовность машины к запуску (фаза 6)")
    ap.add_argument("--env", action="store_true", help="показать имена переменных окружения")
    ap.add_argument("--quiet", action="store_true", help="печатать только итог")
    args = ap.parse_args()

    setup_logging("WARNING")
    cfg = load()
    checks = run_checks(cfg)
    if args.env:
        print("переменные конвейера:", env_summary(), "\n")
    print(render(checks) if not args.quiet else
          f"проваленных обязательных проверок: {len(failed(checks))}")
    return 1 if failed(checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
