"""Доставки: одна запись очереди → пост в каждой настроенной платформе (спека §8).

Архитектура платформо-независимая: адаптер знает только «положи этот текст и
этот файл в этот канал» — MAX/OK добавляются новым классом с тем же методом
`publish`, очередь и модерация об этом не узнают.

Telegram: постинг ТОЛЬКО через официальный Bot API (CLAUDE.md), медиа уходит
своим файлом — перезаливом, без форвардов и копипаста.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from tgtraff.publisher.format import RenderedPost, plan_delivery

log = logging.getLogger("tgtraff.publisher.delivery")


class DeliveryError(RuntimeError):
    """Платформа не приняла пост. Очередь вернёт запись и попробует ещё раз."""


@dataclass
class DeliveryResult:
    platform: str
    channel: str
    message_id: str
    url: str | None = None
    # Длинный пост уходит двумя сообщениями: медиа отдельно, текст отдельно.
    # Главным считается сообщение с текстом — его правит модератор.
    extra_message_ids: list[str] = field(default_factory=list)

    @property
    def all_message_ids(self) -> list[str]:
        return [self.message_id, *self.extra_message_ids]


def _markup(post: RenderedPost):
    """Кнопка «Прислать новость» под постом (спека §8, PINS.md §1)."""
    if not post.button:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=post.button.text, url=post.button.url)]])


def _file(path: str):
    from aiogram.types import FSInputFile

    return FSInputFile(path)


def post_url(channel, username: str | None, message_id: str) -> str | None:
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat = str(channel)
    if chat.startswith("-100"):
        return f"https://t.me/c/{chat[4:]}/{message_id}"
    return None


class TelegramDelivery:
    """Канал Telegram. `bot` — aiogram.Bot (в тестах подставляется двойник)."""

    platform = "tg"

    def __init__(self, bot, parse_mode: str = "HTML"):
        self.bot = bot
        self.parse_mode = parse_mode

    async def publish(self, city, post: RenderedPost, media_path: str | None) -> DeliveryResult:
        chat_id = city.tg.chat_id
        plan = plan_delivery(post.text, media_path if media_path and
                             Path(media_path).exists() else None)
        markup = _markup(post)
        try:
            if plan.mode == "text":
                message = await self.bot.send_message(
                    chat_id=chat_id, text=post.text, parse_mode=self.parse_mode,
                    reply_markup=markup, disable_web_page_preview=True)
                extra: list[str] = []
            elif plan.mode == "photo_caption":
                message = await self._send_media(chat_id, media_path, plan.kind,
                                                 caption=post.text, markup=markup)
                extra = []
            else:
                # Подпись к медиа ограничена 1024 знаками — длинный пост не режем,
                # а выпускаем двумя сообщениями подряд.
                media_message = await self._send_media(chat_id, media_path, plan.kind,
                                                       caption=None, markup=None)
                message = await self.bot.send_message(
                    chat_id=chat_id, text=post.text, parse_mode=self.parse_mode,
                    reply_markup=markup, disable_web_page_preview=True)
                extra = [str(media_message.message_id)]
        except DeliveryError:
            raise
        except Exception as exc:                  # noqa: BLE001 — платформа могла ответить чем угодно
            raise DeliveryError(f"Telegram не принял пост: {exc}") from exc

        message_id = str(message.message_id)
        return DeliveryResult(self.platform, str(chat_id), message_id,
                              post_url(chat_id, city.tg.username, message_id), extra)

    async def _send_media(self, chat_id, path: str, kind: str, caption, markup):
        kw = {"chat_id": chat_id, "caption": caption, "parse_mode": self.parse_mode,
              "reply_markup": markup}
        if kind == "video":
            return await self.bot.send_video(video=_file(path), **kw)
        return await self.bot.send_photo(photo=_file(path), **kw)

    async def edit(self, city, message_id: str, text: str, has_media: bool) -> None:
        """Правка опубликованного из админ-бота (спека §8, карта draft→message_id)."""
        try:
            if has_media:
                await self.bot.edit_message_caption(
                    chat_id=city.tg.chat_id, message_id=int(message_id),
                    caption=text, parse_mode=self.parse_mode)
            else:
                await self.bot.edit_message_text(
                    chat_id=city.tg.chat_id, message_id=int(message_id),
                    text=text, parse_mode=self.parse_mode)
        except Exception as exc:                  # noqa: BLE001
            raise DeliveryError(f"правка не прошла: {exc}") from exc

    async def edit_media(self, city, message_id: str, media_path: str,
                         caption: str | None = None) -> None:
        """Замена самой картинки вышедшего поста (§61.1).

        `editMessageCaption` меняет только текст (это и есть ограничение §43) —
        файл заменяет `editMessageMedia`. Работает лишь там, где медиа уже
        было: превратить текстовое сообщение в фото Telegram не позволяет,
        и вызывающий об этом узнаёт ошибкой, а не тишиной."""
        if not media_path or not Path(media_path).exists():
            raise DeliveryError(f"файла для замены нет на диске: {media_path}")
        from aiogram.types import InputMediaPhoto

        media = InputMediaPhoto(media=_file(media_path), caption=caption,
                                parse_mode=self.parse_mode if caption else None)
        try:
            await self.bot.edit_message_media(chat_id=city.tg.chat_id,
                                              message_id=int(message_id), media=media)
        except Exception as exc:                  # noqa: BLE001
            raise DeliveryError(f"замена картинки не прошла: {exc}") from exc

    async def delete(self, city, message_ids) -> None:
        errors = []
        for message_id in message_ids:
            try:
                await self.bot.delete_message(chat_id=city.tg.chat_id,
                                              message_id=int(message_id))
            except Exception as exc:              # noqa: BLE001 — одно из двух могли удалить руками
                errors.append(f"{message_id}: {exc}")
        if errors:
            raise DeliveryError("удаление не прошло — " + "; ".join(errors))
