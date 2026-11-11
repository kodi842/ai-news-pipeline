"""Критические алерты до появления админ-бота (решение ревью, QUESTIONS §6).

Полноценная система алертов — фаза 5 (карточки, кнопки, aiogram). Здесь минимум,
который окупается уже сейчас: если в `.env` лежат токен бота и id чата, критическое
событие уходит голым HTTP-вызовом Bot API. Нет токена — работаем как в фазе 1,
громкой ошибкой в консоль.

Два правила:
  * алерт никогда не роняет конвейер — любая ошибка отправки проглатывается;
  * повторы гасятся: разлогин повторяется каждым тиком, чат не должен стать логом.

Публикацией это не является: бот пишет служебное сообщение в наш собственный чат.
Требование CLAUDE.md «публикация только через Bot API» здесь как раз соблюдено —
юзербот к отправке не прикасается.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("tgtraff.alerts")

TELEGRAM_LIMIT = 4096
PREFIX = "tgtraff"


async def _httpx_sender(token: str, chat_id: str, text: str) -> bool:
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": text,
                                    "disable_web_page_preview": True})
        return r.status_code == 200


class Alerter:
    def __init__(self, token: str | None, chat_id: str | None, sender=None,
                 repeat_after_sec: int = 1800):
        self.token = token
        self.chat_id = chat_id
        self._sender = sender or _httpx_sender
        self.repeat_after_sec = repeat_after_sec
        self._last_sent: dict[str, float] = {}

    @classmethod
    def from_env(cls, **kw) -> "Alerter":
        """ADMIN_BOT_TOKEN/BOT_TOKEN_ADMIN + адрес чата. Адрес — либо отдельный
        ADMIN_ALERT_CHAT_ID, либо (с фазы 5) первый модератор из ADMIN_CHAT_IDS:
        держать два списка одних и тех же людей ни к чему. Нет ни того, ни
        другого — алерты остаются консольными."""
        chat_id = os.getenv("ADMIN_ALERT_CHAT_ID") or _first_moderator()
        return cls(
            token=os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN_ADMIN") or None,
            chat_id=chat_id,
            **kw,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _suppressed(self, message: str) -> bool:
        now = time.monotonic()
        last = self._last_sent.get(message)
        if last is not None and now - last < self.repeat_after_sec:
            return True
        self._last_sent[message] = now
        return False

    async def alert(self, message: str) -> bool:
        """Отправляет критический алерт. True — ушло в Telegram."""
        log.error("АЛЕРТ: %s", message)
        if not self.enabled or self._suppressed(message):
            return False
        text = f"{PREFIX} ⚠️ {message}"[:TELEGRAM_LIMIT]
        try:
            return bool(await self._sender(self.token, self.chat_id, text))
        except Exception as e:                    # алерт не имеет права ломать конвейер
            log.warning("алерт не доставлен (%s: %s) — сообщение осталось в логе",
                        type(e).__name__, e)
            return False


def _first_moderator() -> str | None:
    raw = os.getenv("ADMIN_CHAT_IDS") or ""
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            return part
    return None
