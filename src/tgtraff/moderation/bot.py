"""Админ-бот модерации (спека §7) и правка опубликованного (спека §8).

Логика решений живёт в `actions.py`, вид карточки — в `card.py`, здесь —
поведение бота: кому показывать, что показывать, что делать по нажатию.

Правка — ответным сообщением на карточку (спека §7): бот помнит, какая
карточка какому драфту принадлежит, поэтому «ответить и написать новый текст»
работает без режимов и состояний. Если пост уже опубликован, та же правка
уходит в канал — по карте draft → message_id.

Автопубликация (§59) — единственное исключение из «в канал идёт только то,
по чему нажата кнопка»: нейтральная новость, прошедшая все шесть условий,
уходит в очередь мимо карточки. Список категорий пуст — режима нет вовсе.

Очередь (решение ревью §55): сами приходят только карточки срочного — молния,
UGC, safety=manual_only. Остальное копится в пуле и выдаётся по /queue N
свежими вперёд; /queue без числа — сводка. Драфт без решения дольше срока
протухает: карточка переписывается в «⏳ протух» и теряет кнопки.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from pathlib import Path

from tgtraff import drafts as drafts_store
from tgtraff.clips import card as clip_view, store as clip_store
from tgtraff.clips.settings import ClipSettings
from tgtraff.moderation import actions, card as card_view
from tgtraff.moderation.card import (CANCEL, DEFER, EDIT, NOW, PHOTO_NO, PHOTO_OK, PINS,
                                     PUBLISH, PULL, REASON_PREFIX, REJECT, card_keyboard,
                                     card_text, photo_keyboard, pins_keyboard,
                                     published_keyboard, reject_keyboard, summary_keyboard,
                                     summary_text)
from tgtraff.publisher import store
from tgtraff.publisher.format import PostContent, render_post
from tgtraff.publisher.settings import ModerationSettings

log = logging.getLogger("tgtraff.moderation.bot")

CAPTION_LIMIT = 1024
# Больше карточек за один /queue не выдаём: чат — не лента.
QUEUE_MAX = 50
# Пауза между правками карточек подряд: лимит Telegram на сообщения в чат.
EDIT_PAUSE_SEC = 0.3
# Куда бот кладёт присланный модератором кадр до проверки (§61.1).
INBOX_DIR = Path("data/media/moderator_inbox")

HELP = (
    "Админ-бот модерации.\n\n"
    "Сами приходят только карточки срочного: молния, UGC, safety=manual_only. "
    "Остальное копится в очереди — тяните её, когда готовы.\n"
    "Кнопки:\n"
    "✅ Опубликовать — поставить в очередь на ближайший слот\n"
    "⚡️ Вне очереди — выпустить немедленно\n"
    "✏️ Править — <b>ответьте</b> на карточку новым текстом "
    "(первая строка — хук, остальное — тело)\n"
    "📷 Своё фото — <b>ответьте</b> на карточку ФОТОГРАФИЕЙ: заменит картинку "
    "(подпись к фото = правка текста). Чужой знак на кадре — бот спросит; "
    "чистый кадр получит наш. Пост уже вышел — картинка заменится и в канале\n"
    "🚫 Отклонить — спросит причину: «не новость» или «мало фактуры»\n"
    "⏸ Отложить — вернётся, когда потянете очередь\n"
    "Драфт без решения сутки (молния — 6 часов) протухает: ⏳, в канал не идёт.\n"
    "Нейтральные безопасные новости разрешённых категорий уходят в канал сами "
    "(§59) — их счётчик в сводке /queue и в дайджесте.\n\n"
    "Команды: /queue — сводка очереди по городам и категориям, "
    "/queue N — выдать N свежих карточек,\n"
    "/status — что в очередях, /digest — сводка дня, /cpf — прирост и CPF по площадкам,\n"
    "/pins — выпустить (перевыпустить) закрепы ботом: правила и UGC-оффер "
    "с кнопкой «📸 Прислать новость»."
)


class AdminBot:
    def __init__(self, db, bot, channels, planner, settings: ModerationSettings | None = None,
                 city_names: dict[str, str] | None = None, deliveries: dict | None = None,
                 alerter=None, autopublish_categories=(), photos=None,
                 inbox_dir: Path | str = INBOX_DIR, clip_settings=None):
        self.db = db
        self.bot = bot
        self.channels = channels
        self.planner = planner
        self.settings = settings or ModerationSettings()
        self.city_names = city_names or {}
        self.deliveries = deliveries or {}
        self.alerter = alerter
        # §59: категории, которым разрешён выход без человека. Пустой кортеж —
        # режима нет (kill-switch живёт в конфиге, а не в коде).
        self.autopublish_categories = tuple(autopublish_categories or ())
        # Закрепы ботом (CHANNEL-SETUP §60.5). None — команда недоступна.
        self.pins = None
        # Завод клипов (VIDEO-SPEC V1): бот раздаёт готовые mp4 карточками и
        # принимает решения по ним. Сборка идёт в своём контейнере.
        self.clip_settings = clip_settings or ClipSettings()
        # §61.1: приём фото от модератора. None — приём выключен (нет детектора).
        self.photos = photos
        self.inbox_dir = Path(inbox_dir)
        # message_id карточки → draft_id. Живёт в базе (drafts.card_message_id),
        # здесь — только кэш процесса.
        self._cards: dict[int, int] = {}
        # draft_id → (кадр, подпись), ждущие подтверждения (§61.1). Память
        # процесса: рестарт её теряет, и бот честно попросит прислать заново.
        self._pending_photos: dict[int, tuple[Path, str | None]] = {}
        # Пауза между правками карточек; тесты подменяют, чтобы не ждать.
        self._sleep = asyncio.sleep

    # ---------- доступ ----------

    def is_moderator(self, user_id: int) -> bool:
        return user_id in (self.settings.moderators or ())

    @property
    def moderators(self) -> tuple[int, ...]:
        return tuple(self.settings.moderators or ())

    # ---------- команды ----------

    async def handle_start(self, chat_id: int, user_id: int, username: str | None = None) -> None:
        if not self.moderators:
            await self._say(chat_id,
                            "Список модераторов пуст. Ваш id: <code>%s</code> — положите его "
                            "в <code>ADMIN_CHAT_IDS</code> в .env и перезапустите бота." % user_id)
            return
        if not self.is_moderator(user_id):
            await self._say(chat_id, f"Вас нет в списке модераторов (ваш id: {user_id}).")
            return
        await self._say(chat_id, HELP)

    async def handle_queue(self, chat_id: int, user_id: int, args: str | None = None) -> int:
        """/queue — сводка очереди; /queue N — выдать N свежих карточек из пула (§55).

        Пул — то, что не пушится само: обычные драфты и отложенные. Модератор
        тянет его, когда готов, а не тонет в пуше."""
        if not self.is_moderator(user_id):
            return 0
        arg = (args or "").strip()
        if not arg:
            summary = actions.queue_summary(self.db)
            summary["autopublish"] = bool(self.autopublish_categories)
            markup = summary_keyboard(self.settings.queue_default) if summary["pool"] else None
            await self._say(chat_id, summary_text(summary, self.city_names), markup)
            return 0
        if not arg.isdigit():
            await self._say(chat_id, "Так: /queue — сводка очереди, /queue 10 — выдать десять свежих.")
            return 0
        return await self.pull_cards(chat_id, int(arg))

    async def pull_cards(self, chat_id: int, limit: int) -> int:
        """Выдаёт из пула `limit` карточек, свежие вперёд. Отложенный драфт
        возвращается в стопку и показывается заново."""
        limit = max(1, min(limit, QUEUE_MAX))
        sent = 0
        for row in actions.pool(self.db, limit=limit):
            if row["status"] == "deferred":
                actions.revive(self.db, row["id"])
                row = drafts_store.get_draft(self.db, row["id"])
            if await self._deliver(row):
                sent += 1
        left = actions.queue_summary(self.db)["pool"]
        await self._say(chat_id, (f"Выдано {sent}, в очереди осталось {left}." if sent
                                  else "Очередь пуста — решений никто не ждёт."))
        return sent

    async def handle_status(self, chat_id: int, user_id: int) -> None:
        if not self.is_moderator(user_id):
            return
        drafts = actions.counts(self.db)
        queue = store.queue_counts(self.db)
        mode = "ТЕСТОВЫЙ" if not self.channels.is_live else "БОЕВОЙ"
        ready = ", ".join(self.channels.ready_cities()) or "нет настроенных каналов"
        lines = [f"Режим публикации: <b>{mode}</b> ({ready})", "",
                 "Драфты: " + (", ".join(f"{k} {v}" for k, v in sorted(drafts.items())) or "пусто"),
                 "Очередь: " + (", ".join(f"{k} {v}" for k, v in sorted(queue.items())) or "пусто")]
        await self._say(chat_id, "\n".join(lines))

    async def handle_pins(self, chat_id: int, user_id: int, args: str | None = None) -> None:
        """/pins — выпустить (перевыпустить) закрепы ботом, CHANNEL-SETUP §60.5.

        Сразу не выпускаем: закреп — это пост в боевом канале, и оператор
        сначала видит, в какие каналы он уйдёт."""
        if not self.is_moderator(user_id) or self.pins is None:
            return
        target = (args or "").strip().lower() or "all"
        if target != "all" and self.channels.city(target) is None:
            await self._say(chat_id, f"Не знаю такого города: {html.escape(target)}. "
                                     "Города: " + ", ".join(self.channels.cities))
            return
        ready, skipped = self.pins.plan()
        if target != "all":
            ready, skipped = [c for c in ready if c == target], [c for c in skipped if c == target]
        if not ready:
            await self._say(chat_id, "Каналы не настроены — закрепы выпускать некуда.")
            return
        # Режим — первой строкой: закреп в тестовый канал и закреп в боевой,
        # который увидят подписчики, — разные события с одной командой.
        mode = "БОЕВЫЕ" if self.channels.is_live else "ТЕСТОВЫЕ"
        lines = [f"📌 Выпустить два закрепа (правила, следом UGC-оффер с кнопкой) "
                 f"в <b>{mode}</b> каналы:", ", ".join(ready)]
        if skipped:
            lines.append("Пропустим (канал не настроен): " + ", ".join(skipped))
        lines.append("Предыдущая пара закрепов, если её ставил бот, будет снята.")
        await self._say(chat_id, "\n".join(lines), pins_keyboard(target))

    async def broadcast(self, text: str) -> int:
        """Сообщение всем модераторам: дневной дайджест и отчёты фазы 6."""
        sent = 0
        for chat_id in self.moderators:
            await self._say(chat_id, text)
            sent += 1
        return sent

    async def handle_digest(self, chat_id: int, user_id: int, digest) -> None:
        """Сводку дня можно спросить в любой момент, не дожидаясь её часа."""
        if not self.is_moderator(user_id):
            return
        await self._say(chat_id, digest.build())

    async def handle_cpf(self, chat_id: int, user_id: int, report_text: str) -> None:
        if not self.is_moderator(user_id):
            return
        await self._say(chat_id, f"<pre>{html.escape(report_text)}</pre>")

    # ---------- карточки ----------

    async def push_cards(self, limit: int | None = None, now: datetime | None = None) -> int:
        """Пуш §55: карточкой сразу уходит только срочное — молния, UGC,
        safety=manual_only. Остальное ждёт в пуле, пока модератор не потянет /queue."""
        if not self.moderators:
            return 0
        limit = limit or self.settings.cards_per_tick
        sent = 0
        for row in actions.unseen(self.db, limit=limit, urgent_only=True):
            if await self._deliver(row):
                sent += 1
        return sent

    async def autopublish(self, now: datetime | None = None) -> int:
        """§59: нейтральная новость уходит в очередь без карточки.

        Шесть условий проверяет `actions.may_autopublish`, слот — планировщик
        (обычная сетка с паузами). Модератору не приходит ничего: пуш — только
        для срочного, а счётчик автопостов виден в /queue и в дайджесте."""
        if not self.autopublish_categories:
            return 0
        sent = 0
        for row in actions.autopublish_candidates(self.db, self.autopublish_categories):
            try:
                result = actions.autopublish(self.db, row["id"], self.planner, now=now)
            except store.AlreadyQueued:           # драфт успели одобрить руками
                continue
            if result is not None:
                sent += 1
        if sent:
            log.info("автопубликация §59: %s постов в очередь", sent)
        return sent

    async def expire(self, now: datetime | None = None) -> int:
        """§55: драфты без решения дольше срока протухают; их карточки
        переписываются в «⏳ протух» и теряют кнопки."""
        rows = actions.expire(self.db, now=now, hours=self.settings.stale_hours,
                              flash_hours=self.settings.stale_flash_hours,
                              limit=self.settings.stale_per_tick)
        edited = 0
        for row in rows:
            if not (row["card_chat_id"] and row["card_message_id"]):
                continue
            if edited:
                await self._sleep(EDIT_PAUSE_SEC)
            await self._finish_card(row["card_chat_id"], row["card_message_id"], row,
                                    self._stale_verdict(row))
            edited += 1
        return len(rows)

    def _stale_verdict(self, row) -> str:
        hours = (self.settings.stale_flash_hours if (row["urgency"] or "") == "flash"
                 else self.settings.stale_hours)
        return (f"⏳ протух — без решения {hours} ч, в канал не идёт "
                "(вернётся, если придёт заметная фактура — §28)")

    async def _deliver(self, row) -> bool:
        """Карточка всем модераторам; в базе остаётся первая — по ней бот
        правит сообщение после решения."""
        primary: tuple[int, int] | None = None
        for chat_id in self.moderators:
            message_id = await self.send_card(chat_id, row)
            if message_id is None:
                continue
            # Карточка у каждого модератора своя: ответом на любую из них
            # можно править драфт, поэтому помним все.
            self._cards[message_id] = row["id"]
            primary = primary or (chat_id, message_id)
        if primary is None:
            return False
        actions.mark_card(self.db, row["id"], primary[0], primary[1])
        return True

    async def send_card(self, chat_id: int, row) -> int | None:
        """Карточка модератору. Возвращает message_id сообщения С КНОПКАМИ —
        именно на него отвечают правкой.

        Подпись к медиа в Telegram — 1024 знака. Длинная карточка не имеет права
        стоить модератору картинки, поэтому уходит двумя сообщениями: фото,
        следом текст с кнопками."""
        text = card_text(row, city_name=self.city_names.get(row["city"]),
                         links=self._links(row))
        markup = card_keyboard(row)
        media = row["media_path"]
        has_photo = bool(media) and Path(media).exists() \
            and row["media_kind"] in ("photo", "template")
        try:
            if has_photo:
                from aiogram.types import FSInputFile

                photo = FSInputFile(media)
                if len(text) <= CAPTION_LIMIT:
                    message = await self.bot.send_photo(
                        chat_id=chat_id, photo=photo, caption=text,
                        parse_mode="HTML", reply_markup=markup)
                    return message.message_id
                await self.bot.send_photo(chat_id=chat_id, photo=photo)
            message = await self.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup,
                disable_web_page_preview=True)
        except Exception as exc:                  # noqa: BLE001 — карточка не должна ронять тик
            log.warning("карточка драфта #%s не ушла: %s", row["id"], exc)
            return None
        return message.message_id

    # ---------- кнопки ----------

    async def handle_callback(self, user_id: int, chat_id: int, message_id: int,
                              data: str, now: datetime | None = None) -> str:
        if not self.is_moderator(user_id):
            return "Нет доступа."
        action, _, rest = data.partition(":")
        if action == PULL:                        # кнопка под сводкой /queue
            n = int(rest) if rest.isdigit() else self.settings.queue_default
            return f"Выдано: {await self.pull_cards(chat_id, n)}"
        if action == PINS:                        # кнопка под планом /pins
            return await self._issue_pins(chat_id, rest)
        if action in (clip_view.CLIP_PUBLISH, clip_view.CLIP_REJECT, clip_view.CLIP_REHOOK):
            return await self._clip_decision(action, rest, user_id, chat_id, message_id)
        draft_id_raw, _, extra = rest.partition(":")
        if not draft_id_raw.isdigit():
            return "Не понял кнопку."
        draft_id = int(draft_id_raw)
        row = drafts_store.get_draft(self.db, draft_id)
        if row is None:
            return "Драфта уже нет."
        self._cards[message_id] = draft_id

        if row["status"] == "stale" and action != "del":
            # Кнопка на протухшей карточке: правка не дошла или это карточка-дубль
            # (Telegram принял сообщение, а ответ не пришёл). Решения нет — карточка
            # приводится к виду «⏳ протух».
            await self._finish_card(chat_id, message_id, row, self._stale_verdict(row))
            return "⏳ Драфт протух — без решения дольше срока, в канал не идёт."

        if action == PUBLISH:
            return await self._approve(row, user_id, chat_id, message_id, now=now)
        if action == NOW:
            return await self._approve(row, user_id, chat_id, message_id, now=now, force=True)
        if action == REJECT:
            await self._swap_markup(chat_id, message_id, reject_keyboard(row))
            return "Почему отклоняем?"
        if action == CANCEL:
            await self._swap_markup(chat_id, message_id, card_keyboard(row))
            return "Вернулись"
        if action == REASON_PREFIX:
            return await self._reject(row, user_id, chat_id, message_id, extra)
        if action == DEFER:
            actions.defer(self.db, draft_id, user_id)
            await self._finish_card(chat_id, message_id, row, "⏸ отложено — вернётся по /queue")
            return "Отложено"
        if action == EDIT:
            # §63: подсказка молчала про фото-ответ (§61.1), и оператор счёл
            # функцию отсутствующей. Теперь она называет оба способа правки.
            await self._say(chat_id,
                            f"Ответьте на карточку #{draft_id} текстом (первая строка — хук, "
                            "остальное — тело) или фотографией — она заменит картинку; "
                            "подпись к фото работает как правка текста.")
            return "Жду ответное сообщение"
        if action == PHOTO_OK:
            return await self._confirm_photo(row, user_id, chat_id)
        if action == PHOTO_NO:
            self._drop_pending_photo(draft_id)
            return "Фото не поставили."
        if action == "del":
            return await self._delete_published(row, chat_id, message_id)
        return "Неизвестная кнопка"

    async def _issue_pins(self, chat_id: int, target: str) -> str:
        if self.pins is None:
            return "Закрепы недоступны."
        result = await self.pins.issue(None if target in ("", "all") else target)
        await self._say(chat_id, result.render())
        return f"Готово: {len(result.done)}" if result.done else "Не вышло"

    async def _approve(self, row, user_id: int, chat_id: int, message_id: int,
                       now=None, force: bool = False) -> str:
        draft_id = row["id"]
        try:
            result = actions.approve(self.db, draft_id, user_id, self.planner, now=now,
                                     immediate=force)
        except store.AlreadyQueued:
            return "Этот драфт уже в очереди."
        except ValueError as exc:
            return f"Не могу поставить в очередь: {exc}"

        when = result.scheduled_at.astimezone(
            _zone(self.planner.tz(row["city"])))
        verdict = ("выйдет сейчас" if result.immediate
                   else f"в очереди на {when:%H:%M} по местному времени")
        await self._finish_card(chat_id, message_id, row, f"✅ одобрено, {verdict}")
        return f"Готово: {verdict}"

    async def _reject(self, row, user_id: int, chat_id: int, message_id: int,
                      reason: str) -> str:
        try:
            actions.reject(self.db, row["id"], user_id, reason)
        except ValueError:
            return "Причина не распознана."
        tail = ("больше не предложим" if reason == actions.NOT_NEWS
                else "вернём, если придёт заметная фактура (§28)")
        await self._finish_card(chat_id, message_id, row,
                                f"🚫 отклонено ({actions.REJECT_REASONS[reason]}) — {tail}")
        return "Отклонено"

    # ---------- клипы (VIDEO-SPEC V1) ----------

    async def push_clip_cards(self, limit: int | None = None) -> int:
        """Готовые клипы — карточками модераторам: файл, сценарий, кнопки.

        Автопаба у клипов нет: площадки строже своего канала, и это решение
        ревью (VIDEO-SPEC). Поэтому карточка — единственный путь наружу."""
        if not self.moderators or not self.clip_settings.enabled:
            return 0
        sent = 0
        for row in clip_store.ready_for_card(self.db, limit=limit or 3):
            if await self._deliver_clip(row):
                sent += 1
        return sent

    async def _deliver_clip(self, row) -> bool:
        path = Path(row["file_path"] or "")
        if not path.exists():
            # Файл убрал ретеншн или он не дописался: карточка без видео
            # бессмысленна — модератор не может посмотреть то, чего нет.
            log.warning("клип #%s: файла %s нет — карточка не уходит", row["id"], path)
            clip_store.mark_failed(self.db, row["id"], "файл клипа пропал", max_attempts=0)
            return False
        text = clip_view.card_text(row, city_name=self.city_names.get(row["city"]),
                                  draft=drafts_store.get_draft(self.db, row["draft_id"]),
                                  settings=self.clip_settings)
        markup = clip_view.card_keyboard(row)
        primary = None
        for chat_id in self.moderators:
            message_id = await self._send_clip(chat_id, path, text, markup)
            if message_id is None:
                continue
            primary = primary or (chat_id, message_id)
        if primary is None:
            return False
        clip_store.mark_card(self.db, row["id"], primary[0], primary[1])
        return True

    async def _send_clip(self, chat_id: int, path: Path, text: str, markup) -> int | None:
        from aiogram.types import FSInputFile

        try:
            message = await self.bot.send_video(
                chat_id=chat_id, video=FSInputFile(str(path)), caption=text,
                parse_mode="HTML", reply_markup=markup, supports_streaming=True)
        except Exception as exc:                  # noqa: BLE001 — карточка не роняет тик
            log.warning("карточка клипа не ушла: %s", exc)
            return None
        return message.message_id

    async def _clip_decision(self, action: str, rest: str, user_id: int, chat_id: int,
                             message_id: int) -> str:
        if not rest.split(":")[0].isdigit():
            return "Не понял кнопку."
        clip_id = int(rest.split(":")[0])
        row = clip_store.get_clip(self.db, clip_id)
        if row is None:
            return "Клипа уже нет."

        if action == clip_view.CLIP_PUBLISH:
            clip_store.mark_published(self.db, clip_id, user_id)
            await self._close_clip_card(chat_id, message_id, clip_id,
                                        f"✅ выкладывает {user_id} — "
                                        + ", ".join(self.clip_settings.platforms))
            # Описание отдельным сообщением: из подписи под видео его копировать
            # неудобно, а именно этим модератор сейчас и занят.
            await self._say(chat_id, "<b>Описание для площадки</b> (копируйте целиком):\n\n"
                            + html.escape(clip_view.description(
                                row, city_name=self.city_names.get(row["city"]),
                                username=self._username(row["city"]))))
            return "Отмечено: выкладываете руками"
        if action == clip_view.CLIP_REJECT:
            clip_store.mark_rejected(self.db, clip_id, user_id, reason="решение модератора")
            await self._close_clip_card(chat_id, message_id, clip_id, "🚫 отклонён")
            return "Отклонён"
        clip_store.request_rehook(self.db, clip_id, user_id)
        await self._close_clip_card(chat_id, message_id, clip_id,
                                    "🔁 хук перегенерируется — придёт новая карточка")
        return "Перегенерим хук: клип вернётся новой карточкой"

    async def _close_clip_card(self, chat_id: int, message_id: int, clip_id: int,
                               verdict: str) -> None:
        """Карточка клипа после решения остаётся в чате, но без кнопок."""
        row = clip_store.get_clip(self.db, clip_id)
        text = clip_view.card_text(row, city_name=self.city_names.get(row["city"]),
                                   draft=drafts_store.get_draft(self.db, row["draft_id"]),
                                   settings=self.clip_settings)
        try:
            await self.bot.edit_message_caption(chat_id=chat_id, message_id=message_id,
                                                caption=f"{text}\n\n<b>{verdict}</b>"[:1024],
                                                parse_mode="HTML")
        except Exception as exc:                  # noqa: BLE001
            log.debug("карточку клипа #%s не удалось переписать: %s", clip_id, exc)

    def _username(self, city: str) -> str | None:
        channel = self.channels.city(city)
        return getattr(getattr(channel, "tg", None), "username", None) if channel else None

    # ---------- правка ответным сообщением ----------

    async def handle_reply(self, user_id: int, chat_id: int, reply_to_message_id: int,
                           text: str) -> bool:
        """Ответ на карточку — новый текст драфта (спека §7)."""
        if not self.is_moderator(user_id) or not text or not text.strip():
            return False
        draft_id = self._draft_of_card(reply_to_message_id)
        if draft_id is None:
            return False
        actions.edit(self.db, draft_id, text, user_id)
        row = drafts_store.get_draft(self.db, draft_id)
        note = "Готово, текст заменён."
        if row["status"] == "published":
            note = await self._edit_published(row)
        await self._say(chat_id, f"#{draft_id}: {note}")
        return True

    # ---------- §61.1: фото ответным сообщением ----------

    async def handle_photo_reply(self, user_id: int, chat_id: int, reply_to_message_id: int,
                                 file_id: str, caption: str | None = None) -> bool:
        """Ответ на карточку ФОТОГРАФИЕЙ — новое медиа драфта (§61.1).

        Тот же путь, что у правки текста: бот знает, какой карточке какой драфт
        принадлежит, поэтому режимов и состояний не нужно. Подпись к фото, если
        она есть, работает ровно как текстовая правка — хук и тело.

        Присланное сначала проверяется на ЧУЖОЙ знак (спека §5): чужой или
        непроверенный кадр в драфт не попадает, пока человек не подтвердит
        кнопкой; чистый получает НАШ знак и встаёт сразу."""
        if not self.is_moderator(user_id) or self.photos is None or not file_id:
            return False
        draft_id = self._draft_of_card(reply_to_message_id)
        if draft_id is None:
            return False
        row = drafts_store.get_draft(self.db, draft_id)
        if row is None:
            return False

        path = await self._download_photo(draft_id, file_id)
        if path is None:
            await self._say(chat_id, f"#{draft_id}: фото не скачалось — пришлите ещё раз.")
            return True

        check = self.photos.check(path)
        if check.needs_confirmation:
            # Подпись едет вместе с кадром: подтверждение не должно съесть
            # правку текста, которую модератор написал под фотографией.
            self._pending_photos[draft_id] = (path, caption)
            whose = f" — похоже, это знак «{html.escape(check.owner)}»" if check.owner else ""
            what = ("На фото ЧУЖОЙ вотермарк" + whose if check.is_foreign
                    else "Проверить фото на чужой знак не удалось")
            await self._say(chat_id,
                            f"#{draft_id}: {what}.\nЧужие знаки мы не убираем (спека §5). "
                            "Ставим как есть — без нашей плашки?", photo_keyboard(row, check.owner))
            return True

        await self._say(chat_id, f"#{draft_id}: {await self._put_photo(row, path, user_id, caption)}")
        return True

    async def _confirm_photo(self, row, user_id: int, chat_id: int) -> str:
        """Кнопка «всё равно поставить» под предупреждением о чужом знаке.

        НАШ знак на такой кадр не ставится: плашка — заявка на эксклюзив,
        а на чужом фото это была бы неправда (спека §5)."""
        pending = self._pending_photos.pop(row["id"], None)
        if pending is None or not pending[0].exists():
            return "Фото уже не ждёт — пришлите его ответом на карточку заново."
        path, caption = pending
        await self._say(chat_id, f"#{row['id']}: "
                        + await self._put_photo(row, path, user_id, caption, stamp=False))
        return "Поставили"

    def _drop_pending_photo(self, draft_id: int) -> None:
        pending = self._pending_photos.pop(draft_id, None)
        if pending:
            _drop_file(pending[0])

    async def _put_photo(self, row, path: Path, user_id: int, caption: str | None,
                         stamp: bool = True) -> str:
        """Кадр → медиа драфта, а если пост уже вышел — и в канал."""
        draft_id = row["id"]
        # Вышел ли пост С КАРТИНКОЙ — видно только по драфту ДО подмены:
        # после неё media_path всегда заполнен.
        had_media = bool(row["media_path"])
        try:
            self.photos.attach(draft_id, path, user_id, stamp=stamp)
        except Exception as exc:                  # noqa: BLE001 — диск, права, битый файл
            log.warning("фото к драфту #%s не подставилось: %s", draft_id, exc)
            return f"фото не удалось подставить: {exc}"
        # Скачанный кадр — перевалка: в медиа-каталоге он уже лежит своей копией,
        # а inbox иначе копит гигабайты, которые ретеншн не разбирает.
        _drop_file(path)
        if caption and caption.strip():
            actions.edit(self.db, draft_id, caption, user_id)
        row = drafts_store.get_draft(self.db, draft_id)
        tail = " и текст заменён" if caption and caption.strip() else ""
        if row["status"] != "published":
            return f"картинка заменена{tail} — выйдет с ней."
        return f"картинка заменена{tail}; " + await self._edit_published_media(row, had_media)

    async def _edit_published_media(self, row, had_media: bool = True) -> str:
        """Замена картинки в уже вышедшем посте (§61.1, карта draft → message_id).

        Пост, вышедший ТЕКСТОМ, картинкой стать не может — это ограничение
        Bot API (§43). Молча промолчать здесь нельзя: модератор должен узнать,
        что в канале ничего не поменялось."""
        if not had_media:
            return ("пост выходил ТЕКСТОМ, а текстовое сообщение картинкой не станет "
                    "(ограничение Bot API, §43) — в канале ничего не изменилось. "
                    "Чтобы пост вышел с фото: «🗑 Удалить пост» и выпустить заново.")
        text = render_post(_content(row), self.channels.city(row["city"]),
                           self.channels.ugc_bot).text
        edited = 0
        for pub in store.publications(self.db, row["id"]):
            delivery = self.deliveries.get(pub["platform"])
            if delivery is None or not hasattr(delivery, "edit_media"):
                continue
            extra = store.decode_extra(pub)
            # Длинный пост уехал двумя сообщениями: картинка — в первом,
            # его id и лежит в extra. Короткий держит картинку сам.
            target = extra[0] if extra else pub["message_id"]
            caption = None if extra else text[:CAPTION_LIMIT]
            try:
                await delivery.edit_media(self.channels.city(row["city"]), target,
                                          row["media_path"], caption)
                store.mark_edited(self.db, pub["id"])
                edited += 1
            except Exception as exc:              # noqa: BLE001
                log.warning("картинка поста #%s не заменилась: %s", pub["id"], exc)
                return (f"в канале заменить не вышло ({exc}). Пост без картинки "
                        "картинкой не станет — это ограничение Bot API (§43): "
                        "снимите пост кнопкой «Удалить» и перевыпустите.")
        return ("в канале картинка обновлена" if edited
                else "опубликованных сообщений не нашлось")

    async def _download_photo(self, draft_id: int, file_id: str) -> Path | None:
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        path = self.inbox_dir / f"{draft_id}-{file_id[:24]}.jpg"
        try:
            await self.bot.download(file_id, destination=str(path))
        except Exception as exc:                  # noqa: BLE001
            log.warning("фото модератора не скачалось (%s)", exc)
            return None
        return path

    def _draft_of_card(self, message_id: int) -> int | None:
        if message_id in self._cards:
            return self._cards[message_id]
        row = self.db.conn.execute(
            "SELECT id FROM drafts WHERE card_message_id=? ORDER BY id DESC LIMIT 1",
            (message_id,)).fetchone()
        return row["id"] if row else None

    async def _edit_published(self, row) -> str:
        """Правка уже вышедшего поста — по карте draft → message_id (спека §8)."""
        city = self.channels.city(row["city"])
        text = render_post(_content(row), city, self.channels.ugc_bot).text
        edited = 0
        for pub in store.publications(self.db, row["id"]):
            delivery = self.deliveries.get(pub["platform"])
            if delivery is None or not hasattr(delivery, "edit"):
                continue
            has_media = bool(row["media_path"]) and not store.decode_extra(pub)
            try:
                await delivery.edit(city, pub["message_id"], text, has_media)
                store.mark_edited(self.db, pub["id"])
                edited += 1
            except Exception as exc:              # noqa: BLE001
                log.warning("правка поста #%s не прошла: %s", pub["id"], exc)
                return f"текст в базе поправлен, но канал не принял правку: {exc}"
        return ("текст заменён и пост в канале обновлён" if edited
                else "текст заменён (опубликованных сообщений не нашлось)")

    async def _delete_published(self, row, chat_id: int, message_id: int) -> str:
        city = self.channels.city(row["city"])
        removed = 0
        for pub in store.publications(self.db, row["id"]):
            delivery = self.deliveries.get(pub["platform"])
            if delivery is None:
                continue
            ids = [pub["message_id"], *store.decode_extra(pub)]
            try:
                await delivery.delete(city, ids)
            except Exception as exc:              # noqa: BLE001 — могли удалить руками
                log.warning("удаление поста #%s: %s", pub["id"], exc)
            store.mark_deleted(self.db, pub["id"])
            removed += 1
        await self._say(chat_id, f"#{row['id']}: пост удалён из канала ({removed}).")
        return "Удалено"

    # ---------- бота добавили в канал ----------

    async def handle_my_chat_member(self, chat_id: int, title: str | None,
                                    chat_type: str, status: str) -> str | None:
        """Бота сделали админом канала — сообщаем id и куда его положить.

        Иначе id приватного канала приходится добывать вручную; а ошибиться
        в нём — значит опубликовать не туда."""
        if chat_type not in ("channel", "supergroup"):
            return None
        name = title or str(chat_id)
        if status in ("administrator", "creator"):
            message = (f"Бот теперь админ в «{name}».\n"
                       f"chat_id: <code>{chat_id}</code>\n"
                       "Положите его в .env в переменную "
                       "<code>TG_CHANNEL_&lt;ГОРОД&gt;_TEST</code> "
                       "(боевую — только после переключения channels.yaml в live).")
        elif status in ("left", "kicked", "restricted"):
            message = f"Бот больше не админ в «{name}» (chat_id {chat_id}) — публикации туда не будет."
        else:
            return None
        # Модераторов может ещё не быть (первый запуск) — тогда id остаётся
        # в логе процесса, в сам канал бот служебных сообщений не пишет.
        log.info("%s", message.replace("\n", " "))
        for moderator in self.moderators:
            await self._say(moderator, message)
        return message

    # ---------- крючок publisher'а ----------

    async def announce_published(self, outcome) -> None:
        row = drafts_store.get_draft(self.db, outcome.draft_id)
        if row is None:
            return
        url = next((r.url for r in outcome.results if r.platform == "tg"), None)
        where = ", ".join(f"{r.platform}" for r in outcome.results)
        # §59: пост, вышедший сам, помечен прямо здесь. Разбор инцидента
        # начинается с вопроса «кто это выпустил», и ответ должен быть в
        # сообщении, а не в базе; кнопки правки и удаления — те же.
        mark = "🤖 " if row["autopublished_at"] else ""
        tail = " — без модератора (§59)" if row["autopublished_at"] else ""
        text = (f"{mark}📢 Драфт #{outcome.draft_id} опубликован ({where}){tail}.\n"
                f"<b>{row['title']}</b>")
        for chat_id in self.moderators:
            try:
                await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                            reply_markup=published_keyboard(row, url),
                                            disable_web_page_preview=True)
            except Exception as exc:              # noqa: BLE001
                log.warning("уведомление о публикации не ушло: %s", exc)

    # ---------- служебное ----------

    async def _finish_card(self, chat_id: int, message_id: int, row, verdict: str) -> None:
        """Карточка после решения остаётся в чате, но без кнопок и с итогом."""
        await rewrite_card(self.bot, chat_id, message_id, row,
                           self.city_names.get(row["city"]), verdict, sleep=self._sleep,
                           links=self._links(row))

    def _links(self, row):
        """Ссылки на исходные публикации кластера (§61.2). База может ответить
        чем угодно — карточка важнее ссылок, поэтому ошибка не роняет пуш."""
        try:
            return actions.source_links(self.db, row["cluster_id"])
        except Exception as exc:                  # noqa: BLE001
            log.debug("ссылки на источники драфта #%s не собрались: %s", row["id"], exc)
            return []

    async def _swap_markup(self, chat_id: int, message_id: int, markup) -> None:
        try:
            await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id,
                                                     reply_markup=markup)
        except Exception as exc:                  # noqa: BLE001
            log.debug("клавиатуру не удалось заменить: %s", exc)

    async def _say(self, chat_id: int, text: str, reply_markup=None) -> None:
        kw = {"reply_markup": reply_markup} if reply_markup is not None else {}
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                        disable_web_page_preview=True, **kw)
        except Exception as exc:                  # noqa: BLE001
            log.warning("сообщение модератору не ушло: %s", exc)


async def connect_bot(bot, sleep=asyncio.sleep, delay: float = 15, max_delay: float = 120):
    """Первый запрос к Telegram при старте — с повторами, а не с падением.

    С РФ-сервера связь с api.telegram.org рвётся (по IPv4 — до полной
    недоступности), и падение на первом же запросе роняло процесс в рестарт-луп
    Docker'а: 123 рестарта за ночь. Сеть ждём столько, сколько нужно; неверный
    токен (401) — не сеть, падаем сразу и внятно. Polling у aiogram и так с
    повторами — дыра была только здесь. `bot.me()` кэширует ответ, так что
    свой запрос при старте polling'а aiogram в сеть уже не делает."""
    from aiogram.exceptions import TelegramNetworkError

    attempt = 0
    while True:
        try:
            return await bot.me()
        except TelegramNetworkError as exc:
            attempt += 1
            wait = min(delay * attempt, max_delay)
            log.warning("Telegram не отвечает при старте (%s) — попытка %s, повтор через %.0f с",
                        exc, attempt, wait)
            await sleep(wait)


async def rewrite_card(bot, chat_id: int, message_id: int, row, city_name: str | None,
                       verdict: str, sleep=asyncio.sleep, links=None) -> bool:
    """Переписывает карточку в чате: тот же текст, итог жирным, без кнопок.

    Карточка бывает и подписью к фото, и обычным сообщением, а Telegram
    отвечает ошибкой, если править не тот вид, — пробуем оба. На 429 (много
    правок подряд — протухание после ночи) ждём, сколько просит Telegram,
    и повторяем: решённая карточка не должна остаться с живыми кнопками."""
    text = card_text(row, city_name=city_name, links=links) + f"\n\n<b>{verdict}</b>"
    as_caption = bool(row["media_path"]) and row["media_kind"] in ("photo", "template") \
        and len(text) <= CAPTION_LIMIT

    async def caption():
        await bot.edit_message_caption(chat_id=chat_id, message_id=message_id,
                                       caption=text, parse_mode="HTML")

    async def plain():
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                    text=text[:card_view.TEXT_LIMIT], parse_mode="HTML",
                                    disable_web_page_preview=True)

    for attempt in ((caption, plain) if as_caption else (plain, caption)):
        for _ in range(2):                        # одна повторная попытка после 429
            try:
                await attempt()
                return True
            except Exception as exc:              # noqa: BLE001
                wait = getattr(exc, "retry_after", None)   # aiogram TelegramRetryAfter
                if wait:
                    log.warning("карточка #%s: Telegram просит подождать %s с", row["id"], wait)
                    await sleep(wait + 1)
                    continue
                log.debug("карточку не удалось переписать (%s): %s", attempt.__name__, exc)
                break
    return False


def _drop_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("временный файл %s не убрался: %s", path, exc)


def _content(row) -> PostContent:
    return PostContent(city=row["city"], title=row["title"] or "", body=row["body"] or "",
                       category=row["category"] or "other", urgency=row["urgency"] or "normal",
                       district=row["district"], media_path=row["media_path"])


def _zone(tz: str):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(tz)
    except Exception:                             # noqa: BLE001
        return timezone.utc
