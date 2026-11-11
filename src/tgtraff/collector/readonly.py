"""Механический запрет записи для юзербота.

CLAUDE.md: «Юзербот — ТОЛЬКО чтение. Никаких рассылок, инвайтинга, накруток»,
«Публикация — только через официальный Bot API».

Запрет реализован на самом низком уровне — в `TelegramClient.__call__`, через который
проходит любой RPC Telethon. Даже если в конвейере когда-нибудь появится код,
пытающийся что-то отправить, он упадёт с ReadOnlyViolation, а не отправит.
"""
from __future__ import annotations

import re

from telethon import TelegramClient

# Запрещаем по имени TL-запроса. Денилист (а не алоулист) — чтобы не сломать служебные
# запросы Telethon (auth, updates, файловые загрузки на скачивание).
_FORBIDDEN = re.compile(
    r"""^(
        Send            # SendMessage/SendMedia/SendMultiMedia/SendReaction/SendVote/SendInlineBotResult...
      | Forward
      | Edit(Message|ChatAdmin|ChatDefaultBannedRights|Creator|Title|Photo|AdminRequest)
      | Delete
      | Invite
      | AddChat
      | AddContact
      | Report
      | SetTyping
      | ToggleNoForwards
      | StartBot
      | Save(FilePart|BigFilePart|Draft|Gif|RecentSticker)
      | Upload
      | Create(Chat|Channel)
      | Leave                 # не уходим из источников без явного намерения оператора
      | Update(Profile|Username)   # профиль/юзернейм аккаунта — ручная операция оператора
      | SetPrivacy                 # настройки приватности — тоже (scripts/harden_account.py)
      | Unblock | Block
    )""",
    re.VERBOSE,
)

# Явные исключения:
#  * вступление в канал/по инвайту — условие чтения источника, а не «инвайтинг»
#    (мы приглашаем не людей, а себя подписываем на паблик);
#  * auth.* — иначе юзербот не сможет войти (SendCode попадает под запрет по префиксу).
_ALLOWED_EXACT = {
    "JoinChannelRequest",
    "ImportChatInviteRequest",
    "CheckChatInviteRequest",
    "SendCodeRequest",
    "ResendCodeRequest",
    "SignInRequest",
    "CheckPasswordRequest",
    "GetPasswordRequest",
}

# LogOut под запретом отдельно: сессия юзербота нужна коллектору, разлогин — ручная
# операция оператора, а не что-то, что может сделать код конвейера.
_FORBIDDEN_EXACT = {"LogOutRequest", "ResetAuthorizationsRequest", "DeleteAccountRequest"}


class ReadOnlyViolation(RuntimeError):
    """Попытка что-либо отправить/изменить с юзербота."""


def assert_read_only(request) -> None:
    """Бросает ReadOnlyViolation, если запрос (или любой из пачки) пишет в Telegram."""
    if isinstance(request, (list, tuple)):
        for item in request:
            assert_read_only(item)
        return

    name = type(request).__name__
    if name in _FORBIDDEN_EXACT:
        raise ReadOnlyViolation(f"запрос {name} запрещён: сессия юзербота управляется вручную")
    if name in _ALLOWED_EXACT:
        return
    if _FORBIDDEN.match(name):
        raise ReadOnlyViolation(
            f"юзербот работает только на чтение, запрос {name} запрещён "
            f"(публикация — только через Bot API, см. CLAUDE.md)"
        )


class ReadOnlyTelegramClient(TelegramClient):
    """TelegramClient, физически неспособный писать в Telegram."""

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        assert_read_only(request)
        return await super().__call__(request, ordered=ordered)

    # Высокоуровневые методы Telethon местами ходят мимо __call__ (или собирают запрос
    # лениво) — закрываем их отдельно, чтобы ошибка была понятной и ранней.
    def _deny(self, method: str):
        raise ReadOnlyViolation(
            f"юзербот работает только на чтение, {method}() запрещён "
            f"(публикация — только через Bot API, см. CLAUDE.md)"
        )

    async def send_message(self, *a, **kw):            self._deny("send_message")
    async def send_file(self, *a, **kw):               self._deny("send_file")
    async def forward_messages(self, *a, **kw):        self._deny("forward_messages")
    async def edit_message(self, *a, **kw):            self._deny("edit_message")
    async def delete_messages(self, *a, **kw):         self._deny("delete_messages")
    async def send_read_acknowledge(self, *a, **kw):   self._deny("send_read_acknowledge")
    async def pin_message(self, *a, **kw):             self._deny("pin_message")
    async def unpin_message(self, *a, **kw):           self._deny("unpin_message")
    async def edit_permissions(self, *a, **kw):        self._deny("edit_permissions")
    async def edit_admin(self, *a, **kw):              self._deny("edit_admin")
    async def kick_participant(self, *a, **kw):        self._deny("kick_participant")
    async def upload_file(self, *a, **kw):             self._deny("upload_file")
