"""Бот «Прислать новость» (спека, фаза 5): приём UGC от читателей.

Один бот на всю сеть, город приходит deep-link'ом из кнопки под постом
(`?start=mgn|nvk|che`, DECISIONS.md). Пришедшему без ссылки бот предлагает
выбрать город кнопками — иначе заявка повисает без адресата.

Механика «платим за новости» — текст в закрепе и в приветствии; выплаты
ручные (спека, фаза 5), бот только запоминает автора.
"""
from __future__ import annotations

import logging
from pathlib import Path

from tgtraff.moderation.ugc import UgcIntake, UnknownCity

log = logging.getLogger("tgtraff.ugc.bot")

CITY_PREFIX = "city"

WELCOME = (
    "📸 <b>Прислать новость — платим за эксклюзив</b>\n\n"
    "Видели ДТП, пожар, коммунальную аварию, перекрытую улицу? Снимите и пришлите — "
    "выйдет быстрее, чем где-либо ещё в городе.\n\n"
    "💰 300 ₽ за эксклюзивное фото или видео, до 1000 ₽ за видео крупного ЧП. "
    "Обычные новости и жалобы публикуем бесплатно.\n"
    "🔒 Имя и контакты не публикуем, если вы сами не попросите.\n\n"
    "Пришлите фото или видео и пару слов: <b>что и где</b> (улица, район)."
)

THANKS = ("Спасибо! Передали редактору — если возьмём в канал, напишем сюда же. "
          "Если это эксклюзив, свяжемся по выплате.")

ASK_CITY = "Из какого вы города?"
EMPTY = "Напишите пару слов: что случилось и где. Можно приложить фото или видео."


class UgcBot:
    def __init__(self, db, bot, intake: UgcIntake, city_names: dict[str, str],
                 download_dir: Path | str = "data/media/ugc_inbox"):
        self.db = db
        self.bot = bot
        self.intake = intake
        self.city_names = city_names
        self.download_dir = Path(download_dir)

    # ---------- /start ----------

    async def handle_start(self, chat_id: int, user_id: int, username: str | None,
                           payload: str | None) -> None:
        city = self.intake.register(user_id, username, payload)
        if city:
            name = self.city_names.get(city, city)
            await self._say(chat_id, f"{WELCOME}\n\nВаш город: <b>{name}</b>.")
            return
        await self._say(chat_id, f"{WELCOME}\n\n{ASK_CITY}", markup=self._city_keyboard())

    def _city_keyboard(self):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"{CITY_PREFIX}:{slug}")]
            for slug, name in self.city_names.items()])

    async def handle_city_choice(self, user_id: int, chat_id: int, data: str) -> str | None:
        prefix, _, slug = data.partition(":")
        if prefix != CITY_PREFIX or slug not in self.city_names:
            return None
        self.intake.register(user_id, None, None)
        self.intake.set_city(user_id, slug)
        await self._say(chat_id, f"Записали: <b>{self.city_names[slug]}</b>. "
                                 "Теперь пришлите фото или видео и пару слов, что случилось.")
        return slug

    # ---------- заявка ----------

    async def handle_submission(self, chat_id: int, user_id: int, username: str | None,
                                text: str | None, file_id: str | None = None,
                                media_kind: str | None = None) -> int | None:
        if not (text or "").strip() and not file_id:
            await self._say(chat_id, EMPTY)
            return None
        if not self.intake.city_of(user_id):
            await self._say(chat_id, ASK_CITY, markup=self._city_keyboard())
            return None

        media_path = await self._download(user_id, file_id, media_kind) if file_id else None
        try:
            draft_id = self.intake.submit(user_id, username, text or "",
                                          media_path=media_path, media_kind=media_kind)
        except UnknownCity:
            await self._say(chat_id, ASK_CITY, markup=self._city_keyboard())
            return None
        except ValueError:
            await self._say(chat_id, EMPTY)
            return None
        await self._say(chat_id, THANKS)
        return draft_id

    async def _download(self, user_id: int, file_id: str, media_kind: str | None) -> Path | None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".mp4" if media_kind == "video" else ".jpg"
        path = self.download_dir / f"{user_id}-{file_id[:24]}{suffix}"
        try:
            await self.bot.download(file_id, destination=str(path))
        except Exception as exc:                  # noqa: BLE001 — текст важнее картинки
            log.warning("UGC-медиа не скачалось (%s) — заявка уйдёт текстом", exc)
            return None
        return path

    async def _say(self, chat_id: int, text: str, markup=None) -> None:
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                        reply_markup=markup, disable_web_page_preview=True)
        except Exception as exc:                  # noqa: BLE001
            log.warning("ответ читателю не ушёл: %s", exc)
