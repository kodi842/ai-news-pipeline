#!/usr/bin/env python
"""Перелогин юзербота в НОВЫЙ session-файл (решение ревью по QUESTIONS §9).

    .venv/bin/python scripts/relogin.py --check    # что будет сделано, без входа
    .venv/bin/python scripts/relogin.py            # выполнить перелогин

Зачем: у продавца бандла осталась копия нашего session-файла. Копия — это тот же
`auth_key`, для Telegram та же сессия, поэтому `resetAuthorizations` её не убивает.
Убивает только новая авторизация: код придёт сервисным сообщением в действующую
сессию (её мы и слушаем этим же скриптом), а 2FA-пароль есть только у нас — значит
продавец, даже увидев код, войти не сможет.

Порядок работы:
  1. подключаемся СТАРОЙ сессией (она же ловит код);
  2. новым клиентом на новом файле запрашиваем код;
  3. читаем код из служебного чата 777000 старой сессией;
  4. входим с кодом и 2FA-паролем из secrets/;
  5. сверяем, что вошли в тот же аккаунт;
  6. бэкапим оба файла, новый ставим на место старого, старый удаляем;
  7. отметку о времени новой сессии кладём в БД: от неё считаются сутки
     до `resetAuthorizations` и до первых вступлений по инвайтам.

Коллектор на время перелогина должен быть остановлен.
"""
import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telethon.errors import SessionPasswordNeededError  # noqa: E402
from telethon.tl.types.auth import SentCodeTypeApp, SentCodeTypeSms  # noqa: E402

from tgtraff.app import build_client, load, require_free_session, setup_logging  # noqa: E402
from tgtraff.collector.readonly import ReadOnlyTelegramClient  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.relogin import (  # noqa: E402
    LOGIN_SERVICE_CHAT, backup_session, extract_login_code, is_login_code_message,
)

PASSWORD_FILE = "tg_2fa_password.txt"
SESSION_CREATED_KEY = "session_created_at"
CODE_WAIT_SEC = 180


def new_session_path(cfg) -> Path:
    return cfg.paths.session.with_name(cfg.paths.session.stem + ".new" + cfg.paths.session.suffix)


def build_on(cfg, path: Path) -> ReadOnlyTelegramClient:
    """Клиент на произвольном session-файле, тот же отпечаток устройства.
    Read-only: вход (SendCode/SignIn/CheckPassword) обёрткой разрешён, запись — нет."""
    return ReadOnlyTelegramClient(
        str(path), cfg.telegram.api_id, cfg.telegram.api_hash, **cfg.device.as_kwargs(),
        auto_reconnect=True, connection_retries=5, request_retries=5,
    )


async def wait_for_code(old_client, sent_at: datetime) -> str:
    """Ждём код в служебном чате Telegram, читая его ДЕЙСТВУЮЩЕЙ сессией."""
    print(f"жду код в служебном чате {LOGIN_SERVICE_CHAT} (до {CODE_WAIT_SEC} с)…")
    deadline = datetime.now(timezone.utc) + timedelta(seconds=CODE_WAIT_SEC)
    while datetime.now(timezone.utc) < deadline:
        async for message in old_client.iter_messages(LOGIN_SERVICE_CHAT, limit=5):
            date = message.date
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if date < sent_at - timedelta(seconds=30):
                continue                       # старое сообщение, не наш код
            if is_login_code_message(message):
                code = extract_login_code(message.message)
                print(f"код получен ({date:%H:%M:%S} UTC)")
                return code
        await asyncio.sleep(3)
    raise SystemExit("код не пришёл за отведённое время — перелогин не выполнен, "
                     "старая сессия осталась рабочей")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Перелогин юзербота в новый session-файл")
    ap.add_argument("--check", action="store_true", help="показать план и выйти")
    args = ap.parse_args()

    setup_logging("WARNING")
    cfg = load()
    secrets_dir = cfg.paths.session.parent
    password_path = secrets_dir / PASSWORD_FILE
    new_path = new_session_path(cfg)

    if not password_path.exists():
        raise SystemExit(f"нет файла с 2FA-паролем: {password_path}\n"
                         f"Без него вход не завершить — сначала scripts/harden_account.py")
    password = password_path.read_text("utf-8").strip()

    print(f"старая сессия: {cfg.paths.session}")
    print(f"новая сессия:  {new_path}")
    if args.check:
        print("\nплан: запросить код → прочитать его старой сессией → войти с 2FA →\n"
              "      бэкап обоих файлов → новый на место старого → старый удалить.\n"
              "      Сброс чужих авторизаций — отдельно, когда новой сессии будет ≥ 24 ч.")
        return

    require_free_session(cfg)
    if new_path.exists():
        raise SystemExit(f"{new_path} уже существует — уберите его, чтобы не путать сессии")

    old = build_client(cfg)
    await old.connect()
    if not await old.is_user_authorized():
        raise SystemExit("действующая сессия недействительна — ловить код нечем")
    me = await old.get_me()
    phone = me.phone and f"+{me.phone}"
    print(f"аккаунт: id={me.id}, имя={me.first_name!r}, тел=···{(me.phone or '')[-4:]}")
    if not phone:
        raise SystemExit("у аккаунта не виден номер — вход невозможен")

    new = build_on(cfg, new_path)
    ok = False
    try:
        await new.connect()
        sent_at = datetime.now(timezone.utc)
        sent = await new.send_code_request(phone)

        kind = type(sent.type).__name__ if sent.type else "?"
        if isinstance(sent.type, SentCodeTypeSms):
            raise SystemExit("Telegram шлёт код по SMS на чужой номер — вход невозможен, "
                             "перелогин отменён (старая сессия цела)")
        if not isinstance(sent.type, SentCodeTypeApp):
            print(f"⚠️  необычный способ доставки кода: {kind} — пробуем поймать в приложении")

        code = await wait_for_code(old, sent_at)

        try:
            await new.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            await new.sign_in(password=password)   # ровно та ступень, ради которой ставили 2FA
            print("2FA-пароль принят")

        new_me = await new.get_me()
        if new_me.id != me.id:
            raise SystemExit(f"вошли не в тот аккаунт: {new_me.id} вместо {me.id}")
        print(f"✅ новая авторизация создана для id={new_me.id}")
        ok = True
    finally:
        await new.disconnect()
        await old.disconnect()

    if not ok:
        return

    # Файлы меняем только после успешного входа: до этого момента рабочей остаётся старая.
    old_backup = backup_session(cfg.paths.session)
    new_backup = backup_session(new_path)
    print(f"бэкапы: {old_backup.name}, {new_backup.name}")

    for extra in (cfg.paths.session.with_suffix(".session-journal"),):
        extra.unlink(missing_ok=True)
    cfg.paths.session.unlink()
    new_path.replace(cfg.paths.session)
    cfg.paths.session.chmod(0o600)
    print(f"новая сессия установлена как {cfg.paths.session}, старый файл удалён")

    db = Database(cfg.paths.db)
    created = datetime.now(timezone.utc)
    db.set_meta(SESSION_CREATED_KEY, created.isoformat())
    db.close()

    unlock = created + timedelta(hours=24)
    print(f"\nотметка о новой сессии записана в БД ({SESSION_CREATED_KEY}).")
    print(f"Через 24 ч, после {unlock:%Y-%m-%d %H:%M} UTC, выполнить:")
    print("  .venv/bin/python scripts/harden_account.py --skip-2fa   # сброс чужих авторизаций")
    print("Тогда же прогрев начнёт вступать по инвайтам (§15).")


if __name__ == "__main__":
    asyncio.run(main())
