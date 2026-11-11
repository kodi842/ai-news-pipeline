"""Перелогин юзербота в новый session-файл (решение ревью по §9).

Зачем: у продавца бандла лежит копия НАШЕГО session-файла — тот же `auth_key`,
для Telegram та же сессия, `resetAuthorizations` её не трогает. Отрезает только
новая авторизация: код подтверждения придёт сервисным сообщением в действующую
сессию (её мы и слушаем), а 2FA-пароль есть только у нас.

Здесь — разбор кода и работа с файлами. Сам вход делает scripts/relogin.py.
"""
import pytest

from tgtraff.relogin import (
    LOGIN_SERVICE_CHAT,
    backup_session,
    extract_login_code,
    is_login_code_message,
)


class Msg:
    def __init__(self, text, sender_id=LOGIN_SERVICE_CHAT):
        self.message = text
        self.sender_id = sender_id


# ---- код из сервисного сообщения ----

@pytest.mark.parametrize("text,code", [
    ("Login code: 12345. Do not give this code to anyone, even if they say they are from Telegram!",
     "12345"),
    ("Your login code: 54321", "54321"),
    ("Код для входа: 24680. Никому не сообщайте этот код.", "24680"),
    ("Code: 13579", "13579"),
])
def test_extracts_code_from_service_message(text, code):
    assert extract_login_code(text) == code


def test_ignores_message_without_code():
    assert extract_login_code("Добро пожаловать в Telegram!") is None


def test_does_not_take_a_random_number_for_a_code():
    """В сервисном чате бывают и другие числа — берём только явно помеченный код."""
    assert extract_login_code("Вы вошли с нового устройства 2 сентября в 19:40") is None


def test_code_may_be_split_by_dashes():
    """Telegram иногда разбивает код дефисами, чтобы его не читали автоматом."""
    assert extract_login_code("Login code: 1-2-3-4-5") == "12345"


def test_only_service_chat_messages_are_trusted():
    """Код «из Telegram» в личке от постороннего — классическая разводка.
    Доверяем ровно чату 777000."""
    assert is_login_code_message(Msg("Login code: 12345")) is True
    assert is_login_code_message(Msg("Login code: 99999", sender_id=123456)) is False


def test_message_from_service_chat_without_code_is_not_a_code():
    assert is_login_code_message(Msg("Ваш аккаунт в безопасности")) is False


# ---- файлы сессии ----

def test_backup_keeps_the_original_and_makes_a_dated_copy(tmp_path):
    session = tmp_path / "userbot.session"
    session.write_bytes(b"session-bytes")

    copy = backup_session(session)

    assert copy.exists() and copy != session
    assert copy.read_bytes() == b"session-bytes"
    assert session.exists()                       # оригинал на месте
    assert copy.stat().st_mode & 0o777 == 0o600


def test_two_backups_do_not_overwrite_each_other(tmp_path):
    session = tmp_path / "userbot.session"
    session.write_bytes(b"a")
    first = backup_session(session)
    session.write_bytes(b"b")
    second = backup_session(session)
    assert first != second
    assert first.read_bytes() == b"a" and second.read_bytes() == b"b"


def test_backup_of_missing_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_session(tmp_path / "нет-такого.session")
