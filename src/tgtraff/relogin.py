"""Перелогин юзербота в новый session-файл (решение ревью по QUESTIONS §9).

Проблема: аккаунт куплен вместе с session-файлом, и у продавца осталась его копия.
Копия — это тот же `auth_key`, то есть для Telegram та же самая сессия:
`auth.resetAuthorizations` сбрасывает только ДРУГИЕ авторизации и её не трогает.

Решение: завести новую авторизацию. Механика безопасна именно потому, что 2FA уже
стоит — код подтверждения Telegram присылает сервисным сообщением в действующую
сессию (её видит и продавец), но завершить вход без нашего пароля нельзя. После
успешного входа старая авторизация сбрасывается, и копия продавца умирает вместе
с ней. Пароль лежит в secrets/, всё остальное — ниже.

Здесь чистые функции; сам вход — scripts/relogin.py.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

# Служебный чат Telegram: единственный источник, которому верим на слово с кодом.
LOGIN_SERVICE_CHAT = 777000

# «Login code: 12345», «Код для входа: 12345», иногда с дефисами внутри.
_CODE = re.compile(
    r"(?:login\s+code|code|код\s+для\s+входа|код\s+подтверждения)\s*[:\-—]?\s*"
    r"(\d(?:[\s\-]?\d){4,6})",
    re.I,
)


def extract_login_code(text: str | None) -> str | None:
    """Код входа из сервисного сообщения. Случайное число в тексте кодом не считаем:
    берём только то, что явно помечено словом «код»."""
    if not text:
        return None
    match = _CODE.search(text)
    if not match:
        return None
    return re.sub(r"[\s\-]", "", match.group(1))


def is_login_code_message(message) -> bool:
    """Сообщение с кодом именно от Telegram.

    Проверка отправителя обязательна: «код из Telegram» в личке от постороннего —
    типовая схема угона аккаунта, и код в ней вводят как раз злоумышленнику."""
    if getattr(message, "sender_id", None) != LOGIN_SERVICE_CHAT:
        return False
    return extract_login_code(getattr(message, "message", None)) is not None


def backup_session(path: Path) -> Path:
    """Копия session-файла с датой в имени. Оригинал не трогаем: бэкап делается
    ДО замены, чтобы неудачный перелогин не оставил нас без доступа."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"session-файла нет: {path}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    copy = path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")
    n = 1
    while copy.exists():                       # два бэкапа в одну секунду — не проблема
        copy = path.with_name(f"{path.stem}.{stamp}-{n}.bak{path.suffix}")
        n += 1
    shutil.copy2(path, copy)
    copy.chmod(0o600)
    return copy
