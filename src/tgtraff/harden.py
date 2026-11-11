"""Харденинг аккаунта юзербота — одноразовая ручная операция оператора.

Зачем (QUESTIONS §1, условие приёмки фазы 1): аккаунт куплен готовым бандлом,
у продавца остаются копии session-файла и доступ к номеру. Пока на аккаунте нет
2FA и не сброшены чужие сессии, «наш» он только на словах.

Что делаем:
  1. ставим 2FA-пароль (сначала пишем его в secrets/, потом отправляем в Telegram);
  2. сбрасываем все прочие сессии — после этого продавец отрезан;
  3. приводим профиль в нейтральный вид и прячем номер в приватности;
  4. уносим файлы бандла из корня проекта в secrets/.

Модуль намеренно НЕ использует ReadOnlyTelegramClient: почти всё здесь — записи в
аккаунт, и они запрещены обёрткой по построению. Это единственное место проекта,
где юзербот что-то меняет, и запускает его человек руками.
"""
from __future__ import annotations

import logging
import os
import secrets as pysecrets
import shutil
import string
from pathlib import Path

from telethon.errors import FreshResetAuthorisationForbiddenError
from telethon.tl.functions.account import SetPrivacyRequest, UpdateProfileRequest
from telethon.tl.functions.auth import ResetAuthorizationsRequest
from telethon.tl.types import (
    InputPrivacyKeyAddedByPhone,
    InputPrivacyKeyPhoneNumber,
    InputPrivacyKeyStatusTimestamp,
    InputPrivacyValueAllowContacts,
    InputPrivacyValueDisallowAll,
)

log = logging.getLogger("tgtraff.harden")

# Файлы купленного бандла: выгрузка аккаунта, tdata и сессии разных библиотек.
BUNDLE_PATTERNS = ("[0-9]*.json", "*_tdata.zip", "*_telethon.session", "*_pyrogram.session")

_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_=+"


# ---------- пароль ----------

def generate_password(length: int = 28) -> str:
    """Пароль генерируется здесь, а не придумывается человеком: его всё равно
    не вводить руками — он лежит в secrets/ и нужен только при перелогине."""
    return "".join(pysecrets.choice(_ALPHABET) for _ in range(length))


def save_secret(path: Path, value: str) -> Path:
    """Пишет секрет в файл, доступный только владельцу. Существующий файл не трогает:
    затереть чужой пароль молча — потерять доступ к аккаунту."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    return path


async def set_two_factor(client, password_path: Path, hint: str = "tgtraff") -> str:
    """Ставит 2FA. Пароль сначала оказывается на диске и только потом в Telegram —
    порядок обратный означал бы риск поставить пароль, которого мы не знаем."""
    password = generate_password()
    save_secret(password_path, password)
    await client.edit_2fa(current_password=None, new_password=password, hint=hint, email=None)
    return password


# ---------- чужие сессии ----------

async def reset_other_sessions(client) -> bool:
    """Сбрасывает все сессии, кроме текущей. False — Telegram отказал, потому что
    наша сессия моложе суток: не сбой, а «повторить через 24 ч»."""
    try:
        await client(ResetAuthorizationsRequest())
        return True
    except FreshResetAuthorisationForbiddenError:
        log.warning("Telegram не даёт сбросить чужие сессии с сессии моложе 24 ч — повторить завтра")
        return False


# ---------- профиль и приватность ----------

async def neutralise_profile(client, first_name: str, about: str = "") -> None:
    """Убирает легенду продавца: имя бандла («Addison») в ленте городских каналов
    выглядит ровно тем, чем является."""
    await client(UpdateProfileRequest(first_name=first_name, last_name="", about=about))


async def hide_phone(client) -> None:
    """Номер — чужой, он не должен светиться. «Найти по номеру» Telegram запретить
    всем не даёт, минимум — только контакты (их у аккаунта нет)."""
    await client(SetPrivacyRequest(key=InputPrivacyKeyPhoneNumber(),
                                   rules=[InputPrivacyValueDisallowAll()]))
    await client(SetPrivacyRequest(key=InputPrivacyKeyAddedByPhone(),
                                   rules=[InputPrivacyValueAllowContacts()]))
    # Коллектор висит онлайн круглосуточно — это заметный признак, прячем.
    await client(SetPrivacyRequest(key=InputPrivacyKeyStatusTimestamp(),
                                   rules=[InputPrivacyValueDisallowAll()]))


# ---------- файлы бандла ----------

def move_bundle_files(root: Path, secrets_dir: Path) -> list[Path]:
    """Уносит файлы бандла из корня проекта в secrets/. В git они и так не попадают
    (.gitignore), но в корне их легко приложить к архиву или скопировать не туда."""
    root, secrets_dir = Path(root), Path(secrets_dir)
    moved: list[Path] = []
    for pattern in BUNDLE_PATTERNS:
        for src in sorted(root.glob(pattern)):
            if not src.is_file():
                continue
            dst = secrets_dir / src.name
            if dst.exists():                      # не затираем то, что уже лежит в secrets/
                dst = secrets_dir / f"{src.stem}.bundle{src.suffix}"
            secrets_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            dst.chmod(0o600)
            moved.append(dst)
    return moved
