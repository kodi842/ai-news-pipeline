"""Юзербот — ТОЛЬКО чтение (CLAUDE.md). Запрет должен быть механическим, а не «мы так договорились»."""
import pytest
from telethon.sessions import MemorySession
from telethon.tl.functions.channels import GetFullChannelRequest, InviteToChannelRequest, JoinChannelRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.functions.messages import (
    AddChatUserRequest,
    DeleteMessagesRequest,
    EditMessageRequest,
    ForwardMessagesRequest,
    GetHistoryRequest,
    ImportChatInviteRequest,
    SendMediaRequest,
    SendMessageRequest,
    SendReactionRequest,
    SetTypingRequest,
)
from telethon.tl.functions.upload import SaveFilePartRequest
from telethon.tl.types import InputPeerEmpty

from tgtraff.collector.readonly import ReadOnlyTelegramClient, ReadOnlyViolation, assert_read_only

PEER = InputPeerEmpty()


@pytest.mark.parametrize(
    "request_obj",
    [
        SendMessageRequest(peer=PEER, message="привет"),
        SendMediaRequest(peer=PEER, media=None, message=""),
        ForwardMessagesRequest(from_peer=PEER, id=[1], to_peer=PEER),
        SendReactionRequest(peer=PEER, msg_id=1),
        InviteToChannelRequest(channel=PEER, users=[]),
        AddChatUserRequest(chat_id=1, user_id=PEER, fwd_limit=0),
        EditMessageRequest(peer=PEER, id=1, message="правка"),
        DeleteMessagesRequest(id=[1]),
        SetTypingRequest(peer=PEER, action=None),
        SaveFilePartRequest(file_id=1, file_part=0, bytes=b""),
    ],
    ids=lambda r: type(r).__name__,
)
def test_write_requests_are_blocked(request_obj):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(request_obj)


@pytest.mark.parametrize(
    "request_obj",
    [
        GetHistoryRequest(peer=PEER, offset_id=0, offset_date=None, add_offset=0, limit=10, max_id=0, min_id=0, hash=0),
        GetFullChannelRequest(channel=PEER),
        ResolveUsernameRequest(username="example_channel_7"),
        JoinChannelRequest(channel=PEER),          # подписка на источник нужна для чтения
        ImportChatInviteRequest(hash="abc"),       # вступление в приватный источник по инвайту
    ],
    ids=lambda r: type(r).__name__,
)
def test_read_and_join_requests_are_allowed(request_obj):
    assert_read_only(request_obj)


def test_batch_of_requests_is_blocked_if_any_writes():
    with pytest.raises(ReadOnlyViolation):
        assert_read_only([ResolveUsernameRequest(username="x"), SendMessageRequest(peer=PEER, message="!")])


async def test_client_send_methods_raise():
    client = ReadOnlyTelegramClient(MemorySession(), 12345, "hash")
    for method, args in [
        ("send_message", ("me", "привет")),
        ("send_file", ("me", "/tmp/x.jpg")),
        ("forward_messages", ("me", [1], "me")),
        ("edit_message", ("me", 1, "правка")),
        ("delete_messages", ("me", [1])),
        ("send_read_acknowledge", ("me",)),
    ]:
        with pytest.raises(ReadOnlyViolation):
            await getattr(client, method)(*args)


async def test_client_call_blocks_write_request():
    client = ReadOnlyTelegramClient(MemorySession(), 12345, "hash")
    with pytest.raises(ReadOnlyViolation):
        await client(SendMessageRequest(peer=PEER, message="привет"))


# ---- логин: auth-запросы блокировать нельзя, иначе юзербот не войдёт ----

@pytest.mark.parametrize("request_obj", [
    __import__("telethon.tl.functions.auth", fromlist=["x"]).SendCodeRequest(
        phone_number="+70000000000", api_id=1, api_hash="h", settings=None),
    __import__("telethon.tl.functions.auth", fromlist=["x"]).ResendCodeRequest(
        phone_number="+70000000000", phone_code_hash="h"),
    __import__("telethon.tl.functions.auth", fromlist=["x"]).SignInRequest(
        phone_number="+70000000000", phone_code_hash="h", phone_code="12345"),
    __import__("telethon.tl.functions.auth", fromlist=["x"]).CheckPasswordRequest(password=None),
    __import__("telethon.tl.functions.account", fromlist=["x"]).GetPasswordRequest(),
], ids=lambda r: type(r).__name__)
def test_auth_requests_are_allowed(request_obj):
    assert_read_only(request_obj)


def test_logout_is_blocked():
    """Разлогин с юзербота — не наша операция: сессия нужна коллектору."""
    from telethon.tl.functions.auth import LogOutRequest
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(LogOutRequest())


# ---- ревью фазы 1: профиль и приватность аккаунта юзербот тоже не трогает ----

@pytest.mark.parametrize("request_obj", [
    __import__("telethon.tl.functions.account", fromlist=["x"]).UpdateUsernameRequest(username="chp_bot"),
    __import__("telethon.tl.functions.channels", fromlist=["x"]).UpdateUsernameRequest(
        channel=PEER, username="chp_city"),
    __import__("telethon.tl.functions.account", fromlist=["x"]).SetPrivacyRequest(key=None, rules=[]),
], ids=lambda r: f"{type(r).__module__.rsplit('.', 1)[-1]}.{type(r).__name__}")
def test_profile_and_privacy_changes_are_blocked(request_obj):
    """Настройки аккаунта меняет оператор руками (scripts/harden_account.py),
    а не код конвейера: случайная смена юзернейма/приватности ломает сессию и легенду."""
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(request_obj)
