"""Закрепы канала ботом: правила + UGC-оффер с кнопкой (CHANNEL-SETUP, §60.5).

Зачем командой, а не руками. Инлайн-кнопка в Telegram бывает только у поста,
отправленного ботом через Bot API, — закреп, поставленный владельцем со своего
аккаунта, кнопки иметь не может в принципе. На время моратория оффер жил
текстовой ссылкой (решение §60.5), и эта команда — обещанный перевыпуск:
оператор говорит «выпустить закрепы», и в каждом боевом канале появляется
оффер с живой кнопкой «📸 Прислать новость» и своим deep-link'ом города.

Порядок фиксирован решением §60.3: сначала правила, потом оффер. В стопке
закреплённых Telegram показывает верхним ПОСЛЕДНИЙ — верхним должен быть
оффер, он и продаёт UGC.

Тексты — `assets/brand/PINS.md` (оффер — вариант A) и docs/CHANNEL-SETUP.md
(правила без sales-строки, решение §60.4). Суммы и обещание анонимности здесь
дублируют закреп сознательно: это оферта читателю, и меняться она должна
в одном месте — вместе с текстом бота (`moderation/ugc_bot.py`).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from tgtraff.publisher.format import ugc_link

log = logging.getLogger("tgtraff.publisher.pins")

BUTTON_TEXT = "📸 Прислать новость"
META_PREFIX = "pins"


def rules_text(city, ugc_bot: str) -> str:
    """Правила одной строкой (PINS.md §2A) без sales-контакта — §60.4:
    менеджера продаж ещё нет, и строка «реклама — @…» вернётся вместе с ним."""
    return (
        "📌 <b>Правила канала</b>\n\n"
        "Только про наш город, без мата, политики, рекламы и травли — "
        "за это баним без предупреждения.\n"
        f'Новости и жалобы — кнопкой «{BUTTON_TEXT}» под соседним закрепом '
        f'или сюда: <a href="{ugc_link(ugc_bot, city.deep_link)}">наш бот</a>.\n'
        "Ошибку в посте нашли — напишите туда же, поправим."
    )


def offer_text(city, ugc_bot: str) -> str:
    """UGC-оффер, вариант A из PINS.md — развёрнутый: на старте аудитория
    механики «продать новость» не знает, и её надо объяснить."""
    return (
        "📣 <b>ПРИСЛАТЬ НОВОСТЬ — ПЛАТИМ ЗА ЭКСКЛЮЗИВ</b>\n\n"
        "Видели ДТП, пожар, коммунальную аварию, драку, перекрытую улицу или просто "
        "что-то странное? Снимите и пришлите нам — выйдет быстрее, чем где-либо ещё "
        "в городе.\n\n"
        "💰 <b>Сколько платим</b>\n"
        "• 300 ₽ — эксклюзивное фото или видео, которого больше нигде нет\n"
        "• до 1000 ₽ — видео крупного ЧП с места: пожар, серьёзное ДТП, спасработы\n"
        "• бесплатно — обычные новости и жалобы: публикуем и так, автора указываем "
        "по желанию\n\n"
        "🔒 <b>Анонимно</b>\n"
        "Имя и контакты не публикуем никогда, если вы сами не попросите. "
        "Ваш номер видим только мы.\n\n"
        "⚡ <b>Как прислать</b>\n"
        "Кнопка ниже → отправьте фото или видео и пару слов: что и где "
        "(улица, район). Всё остальное сделаем сами.\n\n"
        "💳 Выплата на карту или по номеру телефона в течение суток после публикации."
    )


@dataclass
class PinsResult:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        lines = []
        if self.done:
            lines.append("📌 закрепы выпущены: " + ", ".join(self.done))
        if self.skipped:
            lines.append("⏭ пропущены (канал не настроен): " + ", ".join(self.skipped))
        for city, error in self.failed.items():
            lines.append(f"⚠️ {city}: {error}")
        return "\n".join(lines) or "Каналов для закрепов нет."


class Pins:
    """Выпуск и перевыпуск пары закрепов во всех настроенных каналах."""

    def __init__(self, db, bot, channels, parse_mode: str = "HTML"):
        self.db = db
        self.bot = bot
        self.channels = channels
        self.parse_mode = parse_mode

    def plan(self) -> tuple[list[str], list[str]]:
        """(куда выпустим, что пропустим) — для подтверждения оператором
        ДО того, как в боевые каналы уйдут посты."""
        ready, skipped = [], []
        for slug, city in self.channels.cities.items():
            (ready if city.tg.is_ready else skipped).append(slug)
        return ready, skipped

    async def issue(self, only: str | None = None) -> PinsResult:
        """Перевыпуск во всех каналах (или в одном городе).

        Один упавший канал не должен уносить остальные: бота могли не сделать
        админом в одном из трёх, и это не повод оставить два других без оффера."""
        result = PinsResult()
        for slug, city in self.channels.cities.items():
            if only and slug != only:
                continue
            if not city.tg.is_ready:
                result.skipped.append(slug)
                continue
            try:
                await self._issue_city(slug, city)
                result.done.append(slug)
            except Exception as exc:              # noqa: BLE001 — не админ, нет прав, сеть
                log.warning("закрепы для %s не выпустились: %s", slug, exc)
                result.failed[slug] = str(exc)
        return result

    async def _issue_city(self, slug: str, city) -> None:
        await self._drop_previous(slug, city)
        ids = []
        for text, markup in ((rules_text(city, self.channels.ugc_bot), None),
                             (offer_text(city, self.channels.ugc_bot), self._button(city))):
            message = await self.bot.send_message(
                chat_id=city.tg.chat_id, text=text, parse_mode=self.parse_mode,
                reply_markup=markup, disable_web_page_preview=True)
            # Закреп не будит канал: подписчику важен сам пост, а не служебный.
            await self.bot.pin_chat_message(chat_id=city.tg.chat_id,
                                            message_id=message.message_id,
                                            disable_notification=True)
            ids.append(message.message_id)
        self.db.set_meta(f"{META_PREFIX}:{slug}", json.dumps(ids))
        log.info("закрепы выпущены в %s: %s", slug, ids)

    async def _drop_previous(self, slug: str, city) -> None:
        """Перевыпуск заменяет предыдущую пару, а не добавляется к ней: иначе
        в стопке закреплённых копятся мёртвые офферы прошлых версий.

        Снятое руками или удалённое в канале нас не смущает — ошибку на
        каждом id глотаем: цель шага в том, чтобы старого закрепа не осталось."""
        for message_id in self._previous(slug):
            for call, kw in ((self.bot.unpin_chat_message, {}),
                             (self.bot.delete_message, {})):
                try:
                    await call(chat_id=city.tg.chat_id, message_id=message_id, **kw)
                except Exception as exc:          # noqa: BLE001
                    log.debug("старый закреп %s в %s: %s", message_id, slug, exc)

    def _previous(self, slug: str) -> list[int]:
        raw = self.db.get_meta(f"{META_PREFIX}:{slug}")
        if not raw:
            return []
        try:
            return [int(x) for x in json.loads(raw)]
        except (TypeError, ValueError):
            return []

    def _button(self, city):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=BUTTON_TEXT,
                                 url=ugc_link(self.channels.ugc_bot, city.deep_link))]])
