"""Харденинг купленного аккаунта юзербота (условие приёмки фазы 1, QUESTIONS §1).

Операция ручная и одноразовая, но её шаги должны быть проверяемыми: пароль сначала
сохраняется на диск и только потом ставится в Telegram, чужие сессии сбрасываются,
профиль и приватность приводятся в нейтральный вид, файлы бандла уезжают в secrets/.
"""
import stat

import pytest
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

from tgtraff import harden


class FakeClient:
    """Заглушка Telegram: помнит запросы, умеет падать заданной ошибкой."""

    def __init__(self, raise_on=None):
        self.requests = []
        self.two_fa = []
        self.raise_on = raise_on or {}

    async def __call__(self, request):
        name = type(request).__name__
        if name in self.raise_on:
            raise self.raise_on[name]
        self.requests.append(request)
        return True

    async def edit_2fa(self, current_password=None, new_password=None, hint="", email=None):
        if "edit_2fa" in self.raise_on:
            raise self.raise_on["edit_2fa"]
        self.two_fa.append((current_password, new_password, hint, email))
        return True


def sent(client, request_type):
    return [r for r in client.requests if isinstance(r, request_type)]


# ---- пароль 2FA ----

def test_generated_password_is_long_and_random():
    a, b = harden.generate_password(), harden.generate_password()
    assert len(a) >= 20 and a != b
    assert a.isascii() and " " not in a


def test_password_is_saved_before_it_is_set(tmp_path):
    """Порядок критичен: пароль, поставленный в Telegram и не сохранённый, —
    потерянный аккаунт (перелогин на чужом номере невозможен)."""
    path = tmp_path / "tg_2fa_password.txt"
    saved = harden.save_secret(path, "s3cret")
    assert saved.read_text("utf-8").strip() == "s3cret"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_save_secret_refuses_to_overwrite(tmp_path):
    path = tmp_path / "tg_2fa_password.txt"
    harden.save_secret(path, "первый")
    with pytest.raises(FileExistsError):
        harden.save_secret(path, "второй")


async def test_set_two_factor_persists_then_calls_telegram(tmp_path):
    client = FakeClient()
    path = tmp_path / "pw.txt"
    password = await harden.set_two_factor(client, path, hint="tgtraff")
    assert path.read_text("utf-8").strip() == password
    assert client.two_fa == [(None, password, "tgtraff", None)]


async def test_set_two_factor_keeps_password_file_when_telegram_fails(tmp_path):
    """Если Telegram отказал уже после смены — файл должен остаться: иначе не поймём,
    какой пароль в итоге стоит на аккаунте."""
    client = FakeClient(raise_on={"edit_2fa": RuntimeError("сеть")})
    path = tmp_path / "pw.txt"
    with pytest.raises(RuntimeError):
        await harden.set_two_factor(client, path)
    assert path.exists()


# ---- сброс чужих сессий ----

async def test_reset_other_sessions_sends_reset_request():
    client = FakeClient()
    assert await harden.reset_other_sessions(client) is True
    assert len(sent(client, ResetAuthorizationsRequest)) == 1


async def test_fresh_session_refusal_is_reported_not_raised():
    """Telegram запрещает сброс с сессии моложе суток — это не сбой скрипта,
    а «повторить через 24 ч» (QUESTIONS §1)."""
    client = FakeClient(raise_on={"ResetAuthorizationsRequest": FreshResetAuthorisationForbiddenError(None)})
    assert await harden.reset_other_sessions(client) is False


# ---- профиль и приватность ----

async def test_profile_is_neutralised():
    client = FakeClient()
    await harden.neutralise_profile(client, first_name="Максим")
    req = sent(client, UpdateProfileRequest)[0]
    assert req.first_name == "Максим"
    assert req.last_name == "" and req.about == ""


async def test_phone_number_is_hidden_from_everyone():
    client = FakeClient()
    await harden.hide_phone(client)
    keys = {type(r.key) for r in sent(client, SetPrivacyRequest)}
    assert InputPrivacyKeyPhoneNumber in keys
    phone = [r for r in sent(client, SetPrivacyRequest) if isinstance(r.key, InputPrivacyKeyPhoneNumber)][0]
    assert [type(v) for v in phone.rules] == [InputPrivacyValueDisallowAll]


async def test_search_by_phone_is_narrowed_and_last_seen_hidden():
    """«Найти по номеру» нельзя запретить всем (Telegram примет только contacts),
    а вечный online юзербота светить незачем."""
    client = FakeClient()
    await harden.hide_phone(client)
    by_phone = [r for r in sent(client, SetPrivacyRequest) if isinstance(r.key, InputPrivacyKeyAddedByPhone)][0]
    assert [type(v) for v in by_phone.rules] == [InputPrivacyValueAllowContacts]
    last_seen = [r for r in sent(client, SetPrivacyRequest) if isinstance(r.key, InputPrivacyKeyStatusTimestamp)][0]
    assert [type(v) for v in last_seen.rules] == [InputPrivacyValueDisallowAll]


# ---- файлы бандла ----

def test_bundle_files_move_to_secrets(tmp_path):
    root, secrets = tmp_path, tmp_path / "secrets"
    for name in ("255820219.json", "255820219_tdata.zip",
                 "255820219_telethon.session", "255820219_pyrogram.session"):
        (root / name).write_bytes(b"x")
    (root / "README.md").write_text("не трогать", encoding="utf-8")

    moved = harden.move_bundle_files(root, secrets)

    assert len(moved) == 4
    assert {p.name for p in secrets.iterdir()} == {
        "255820219.json", "255820219_tdata.zip",
        "255820219_telethon.session", "255820219_pyrogram.session"}
    assert not list(root.glob("255820219*"))
    assert (root / "README.md").exists()


def test_moving_bundle_twice_is_a_no_op(tmp_path):
    root, secrets = tmp_path, tmp_path / "secrets"
    (root / "255820219.json").write_bytes(b"x")
    harden.move_bundle_files(root, secrets)
    assert harden.move_bundle_files(root, secrets) == []


def test_bundle_move_keeps_both_copies_when_target_exists(tmp_path):
    """Затирать уже лежащий в secrets/ файл нельзя — это может быть рабочая сессия."""
    root, secrets = tmp_path, tmp_path / "secrets"
    secrets.mkdir()
    (root / "255820219.json").write_text("новый", encoding="utf-8")
    (secrets / "255820219.json").write_text("старый", encoding="utf-8")

    moved = harden.move_bundle_files(root, secrets)

    assert (secrets / "255820219.json").read_text("utf-8") == "старый"
    assert len(moved) == 1 and moved[0].name != "255820219.json"
    assert not list(root.glob("255820219*"))


def test_moved_bundle_files_are_not_world_readable(tmp_path):
    root, secrets = tmp_path, tmp_path / "secrets"
    (root / "255820219.json").write_bytes(b"x")
    (root / "255820219.json").chmod(0o644)
    moved = harden.move_bundle_files(root, secrets)
    assert stat.S_IMODE(moved[0].stat().st_mode) == 0o600
