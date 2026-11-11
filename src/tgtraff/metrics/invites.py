"""Инвайт-ссылки под источники трафика (спека §9, чек-лист прогрева, п.5).

«Каждому посеву — своя инвайт-ссылка». Ссылки заводятся ЗАРАНЕЕ, до первого
рубля закупа: ссылка, созданная после запуска объявления, теряет тех, кто
пришёл первыми, и CPF площадки уже не восстановить.

Считать вступления помогает сам Telegram: бот — админ канала и получает
`chat_member` с той ссылкой, по которой человек пришёл. Отдельный опрос
статистики не нужен, а значит, и юзербот к этому не привлекается.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from tgtraff.metrics import store
from tgtraff.metrics.settings import MetricsSettings
from tgtraff.publisher.schedule import local_day

log = logging.getLogger("tgtraff.metrics.invites")

# Прирост без ссылки: поиск Telegram, репост, «привёл знакомый». Это тоже
# прирост, но не купленный — в одну корзину с посевом он идти не должен.
ORGANIC = "organic"
# Служебная ссылка: по ней в канал заходит наш же юзербот, чтобы читать
# просмотры (Bot API их не отдаёт). Это не источник трафика — в CPF-отчёте
# такие строки не участвуют, иначе один наш вход испортил бы статистику
# площадки.
SERVICE = "service"
DEFAULT_TZ = "Europe/Moscow"


class InviteLinks:
    def __init__(self, db, bot, channels, settings: MetricsSettings | None = None,
                 city_tz: dict[str, str] | None = None, alerter=None):
        self.db = db
        self.bot = bot
        self.channels = channels
        self.settings = settings or MetricsSettings()
        self.city_tz = city_tz or {}
        self.alerter = alerter
        self._alerted: set[str] = set()

    # ---------- заведение ----------

    async def ensure(self, now: datetime | None = None) -> list:
        """Заводит недостающие ссылки. Возвращает только созданные в этот раз."""
        if not self.settings.invite_links_enabled:
            return []
        now = now or datetime.now(timezone.utc)
        created = []
        for slug, city in self.channels.cities.items():
            chat_id = city.tg.chat_id
            if not chat_id:
                continue
            for source in self.settings.source_specs():
                row = store.get_link(self.db, slug, str(chat_id), source.name)
                if row is not None and row["url"]:
                    continue
                url = await self._create(slug, chat_id, source)
                if url is None:
                    continue
                created.append(store.ensure_link(
                    self.db, slug, str(chat_id), source.name, url=url,
                    chat_id=str(chat_id), kind=source.kind, note=source.title))
                log.info("%s: заведена инвайт-ссылка «%s» → %s", slug, source.label, url)
        return created

    async def _create(self, slug: str, chat_id, source) -> str | None:
        try:
            link = await self.bot.create_chat_invite_link(chat_id=chat_id, name=source.label)
        except Exception as exc:                  # noqa: BLE001 — нет права приглашать и т.п.
            await self._complain(slug, exc)
            return None
        self._alerted.discard(slug)
        return getattr(link, "invite_link", None)

    async def service_link(self, city: str) -> str | None:
        """Ссылка для юзербота: своя, отдельная от площадок."""
        channel = self.channels.cities.get(city)
        if channel is None or not channel.tg.chat_id:
            return None
        chat_id = channel.tg.chat_id
        row = store.get_link(self.db, city, str(chat_id), SERVICE)
        if row is not None and row["url"]:
            return row["url"]
        url = await self._create(city, chat_id, _ServiceSource())
        if url is None:
            return None
        store.ensure_link(self.db, city, str(chat_id), SERVICE, url=url, chat_id=str(chat_id),
                          kind=SERVICE, note="служебная: подписка юзербота для съёма просмотров")
        return url

    # ---------- счёт вступлений ----------

    def joined(self, chat_id, invite_url: str | None, now: datetime | None = None,
               city: str | None = None) -> None:
        """Пришёл подписчик. Ссылку даёт сам Telegram в `chat_member`."""
        now = now or datetime.now(timezone.utc)
        city = city or self._city_of(chat_id)
        day = local_day(now, self.city_tz.get(city, DEFAULT_TZ))

        if invite_url:
            row = store.link_by_url(self.db, invite_url)
            if row is None:
                # Ссылку могли создать руками в клиенте — вступление по ней
                # всё равно наше, теряться оно не должно.
                row = store.ensure_link(self.db, city, str(chat_id), _name_of(invite_url),
                                        url=invite_url, chat_id=str(chat_id), kind="other",
                                        note="ссылка заведена вне конвейера")
        else:
            row = store.ensure_link(self.db, city, str(chat_id), ORGANIC, chat_id=str(chat_id),
                                    kind="organic", note="прирост без ссылки")
        store.record_join(self.db, row["id"], day)

    def _city_of(self, chat_id) -> str:
        for slug, city in self.channels.cities.items():
            if str(city.tg.chat_id) == str(chat_id):
                return slug
        return str(chat_id)

    async def _complain(self, slug: str, exc: Exception) -> None:
        log.warning("%s: инвайт-ссылка не создана (%s)", slug, exc)
        if slug in self._alerted or self.alerter is None:
            return
        self._alerted.add(slug)
        await self.alerter.alert(
            f"{slug}: не удаётся завести инвайт-ссылки ({exc}). Бот должен быть админом "
            f"канала с правом приглашать — без ссылок CPF закупа не посчитать")


class _ServiceSource:
    """Мини-описание служебной ссылки для `_create` (у неё нет строки конфига)."""

    name = SERVICE
    label = "служебная (юзербот)"
    kind = SERVICE


def _name_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1] or url
