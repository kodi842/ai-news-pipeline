#!/usr/bin/env python
"""Харденинг купленного аккаунта юзербота. ОДНОРАЗОВАЯ РУЧНАЯ ОПЕРАЦИЯ ОПЕРАТОРА.

    .venv/bin/python scripts/harden_account.py --check     # только показать состояние
    .venv/bin/python scripts/harden_account.py --name Иван # выполнить харденинг

Что делает (условие приёмки фазы 1, QUESTIONS §1):
  1. ставит 2FA-пароль и кладёт его в secrets/tg_2fa_password.txt;
  2. сбрасывает все прочие авторизации (копию НАШЕГО session-файла это не убивает —
     см. предупреждение в выводе скрипта);
  3. приводит профиль к нейтральному имени и прячет номер телефона в приватности;
  4. уносит файлы бандла из корня проекта в secrets/.

Важно:
  * Коллектор в это время должен быть остановлен: один аккаунт = один процесс.
  * Скрипт работает на ОБЫЧНОМ TelegramClient — это единственное место проекта,
    где юзербот пишет в свой аккаунт. Конвейеру такие права не выдаются никогда
    (см. src/tgtraff/collector/readonly.py).
  * Если Telegram откажет в сбросе сессий («сессия моложе 24 ч») — повторить
    запуск через сутки, остальные шаги при этом уже будут сделаны.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telethon import TelegramClient  # noqa: E402

from tgtraff import harden  # noqa: E402
from tgtraff.app import load, require_free_session, setup_logging  # noqa: E402
from tgtraff.config import PROJECT_ROOT  # noqa: E402

PASSWORD_FILE = "tg_2fa_password.txt"


def build_plain_client(cfg) -> TelegramClient:
    """Тот же session-файл и тот же отпечаток устройства, что у коллектора,
    но без read-only-обёртки: харденинг по определению пишет в аккаунт."""
    if not cfg.telegram.is_complete:
        raise SystemExit("Нет TG_API_ID / TG_API_HASH в .env")
    return TelegramClient(str(cfg.paths.session), cfg.telegram.api_id, cfg.telegram.api_hash,
                          **cfg.device.as_kwargs())


async def show_state(client) -> bool:
    """Печатает текущее состояние аккаунта и возвращает, стоит ли уже 2FA.
    Номер показываем хвостом: в логи и переписку он попадать не должен."""
    from telethon.tl.functions.account import GetAuthorizationsRequest, GetPasswordRequest

    me = await client.get_me()
    phone = f"···{me.phone[-4:]}" if me.phone else "—"
    print(f"\nаккаунт: id={me.id} имя={me.first_name or ''!r} {me.last_name or ''!r} "
          f"@{me.username or '—'} тел={phone}")

    pwd = await client(GetPasswordRequest())
    print(f"2FA: {'установлена' if pwd.has_password else 'НЕТ — аккаунт открыт для продавца'}")

    auths = await client(GetAuthorizationsRequest())
    print(f"активных сессий: {len(auths.authorizations)}")
    for a in auths.authorizations:
        mark = "  ← текущая (наша)" if a.current else ""
        print(f"  · {a.device_model} / {a.platform} / {a.app_name} {a.app_version} "
              f"— {a.country}, создана {a.date_created:%Y-%m-%d %H:%M}{mark}")
    return bool(pwd.has_password)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Харденинг аккаунта юзербота (ручная операция)")
    ap.add_argument("--check", action="store_true", help="только показать состояние, ничего не менять")
    ap.add_argument("--name", default="Сергей",
                    help="нейтральное имя профиля вместо легенды продавца")
    ap.add_argument("--skip-2fa", action="store_true", help="не трогать 2FA (если уже стоит)")
    args = ap.parse_args()

    setup_logging()
    cfg = load()
    secrets_dir = cfg.paths.session.parent

    # Файлы бандла переносим всегда: это локальная операция, Telegram для неё не нужен.
    moved = harden.move_bundle_files(PROJECT_ROOT, secrets_dir)
    if moved:
        print("файлы бандла перенесены в secrets/:")
        for p in moved:
            print(f"  · {p.name}")
    else:
        print("файлы бандла: в корне проекта не осталось (или уже перенесены)")

    require_free_session(cfg)
    client = build_plain_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Сессия недействительна — сначала scripts/login.py")

    try:
        has_2fa = await show_state(client)
        if args.check:
            return

        print("\n--- харденинг ---")

        if args.skip_2fa:
            print("2FA: пропущено по флагу")
        elif has_2fa:
            print("2FA: на аккаунте уже стоит пароль — не трогаю "
                  "(смена требует текущего пароля, это отдельная ручная операция)")
        else:
            password_path = secrets_dir / PASSWORD_FILE
            if password_path.exists():
                print(f"2FA: файл {password_path.name} уже есть — пропускаю, "
                      f"чтобы не перезаписать рабочий пароль")
            else:
                await harden.set_two_factor(client, password_path)
                print(f"2FA: пароль установлен и сохранён в {password_path}")

        if await harden.reset_other_sessions(client):
            print("чужие сессии: сброшены")
            print("  ВНИМАНИЕ: сброс убивает только ОТДЕЛЬНЫЕ авторизации. Если продавец\n"
                  "  держит копию нашего же session-файла, у него тот же auth_key — для\n"
                  "  Telegram это одна и та же сессия, и сброс её не трогает. Отрезает\n"
                  "  такую копию только перелогин в новый session-файл (после 2FA он\n"
                  "  безопасен: код придёт в приложение, а пароль есть только у нас) —\n"
                  "  решение по перелогину принимает ревью, см. QUESTIONS.")
        else:
            print("чужие сессии: Telegram отказал — сессия моложе 24 ч. "
                  "ПОВТОРИТЬ ЗАПУСК ЧЕРЕЗ СУТКИ")

        await harden.neutralise_profile(client, first_name=args.name)
        print(f"профиль: имя → {args.name!r}, фамилия и «о себе» очищены")

        await harden.hide_phone(client)
        print("приватность: номер скрыт от всех, поиск по номеру — только контакты, "
              "последняя активность скрыта")

        await show_state(client)
        print("\nГотово. Дальше — резервный аккаунт по плейбуку (QUESTIONS §1, п.3).")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
