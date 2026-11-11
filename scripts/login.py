#!/usr/bin/env python
"""Первый вход юзербота. Запускать ВРУЧНУЮ В СВОЁМ ТЕРМИНАЛЕ:

    .venv/bin/python scripts/login.py

Спросит телефон, код из Telegram и (если включена) 2FA-пароль. Ничего из этого
никуда не отправляется и не логируется — Telethon кладёт только session-файл в
secrets/ (в git не попадает).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import build_client, load, setup_logging  # noqa: E402


async def main() -> None:
    setup_logging()
    cfg = load()
    client = build_client(cfg)
    print(f"session-файл: {cfg.paths.session}")
    await client.start()                      # интерактивно спросит телефон/код/2FA
    me = await client.get_me()
    print(f"\n✅ вошли как {me.first_name or ''} @{me.username or '—'} (id={me.id})")
    print("Юзербот в режиме только чтение. Дальше: .venv/bin/python scripts/warmup.py")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
