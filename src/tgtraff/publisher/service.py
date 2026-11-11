"""Сервисные автопосты (спека §8): утренняя погода и вечерний «топ-5 дня».

Это НАШ собственный контент: фактуры источников в нём нет, рерайта нет, LLM
не участвует — поэтому такие посты ложатся в очередь сразу одобренными.
Автопубликация новостей это правило не затрагивает: у драфта конвейера
по-прежнему нет пути в канал без кнопки модератора.

Идемпотентность — по локальным суткам города: одна погода и один топ в день,
рестарт процесса ничего не удваивает. Пропущенный час не догоняется: погода,
опубликованная в обед, — не утренняя погода.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from tgtraff import drafts as drafts_store
from tgtraff.publisher import store
from tgtraff.publisher.schedule import local_day, zone
from tgtraff.publisher.settings import PublisherSettings
from tgtraff.publisher.weather import describe_weather, temp

log = logging.getLogger("tgtraff.publisher.service")

WEATHER, TOP = "weather", "top"
# Насколько поздно ещё уместно выпустить пропущенный сервисный пост.
CATCH_UP_HOURS = 2

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")


class ServicePosts:
    def __init__(self, db, channels, cities: dict, settings: PublisherSettings | None = None,
                 weather=None):
        self.db = db
        self.channels = channels
        self.cities = cities                    # slug → {name, tz, lat, lon}
        self.settings = settings or PublisherSettings()
        self.weather = weather

    async def ensure(self, now: datetime | None = None) -> list[int]:
        """Заводит недостающие сервисные посты. Возвращает id созданных драфтов."""
        if not self.settings.service_enabled:
            return []
        now = now or datetime.now(timezone.utc)
        created: list[int] = []
        for slug, city in self.channels.cities.items():
            if not city.targets() or slug not in self.cities:
                continue
            local = now.astimezone(zone(self.cities[slug].get("tz", "Europe/Moscow")))
            if self._is_time(local, self.settings.weather_hour):
                draft_id = await self._weather_post(slug, city, now, local)
                created += [draft_id] if draft_id else []
            if self._is_time(local, self.settings.top_hour):
                draft_id = self._top_post(slug, city, now, local)
                created += [draft_id] if draft_id else []
        return created

    def _is_time(self, local: datetime, hour: int) -> bool:
        return hour <= local.hour < hour + CATCH_UP_HOURS

    # ---------- погода ----------

    async def _weather_post(self, slug: str, city, now: datetime, local: datetime) -> int | None:
        if self._done(WEATHER, slug, now):
            return None
        meta = self.cities[slug]
        if self.weather is None or meta.get("lat") is None:
            return None
        try:
            forecast = await self.weather.forecast_for(meta["lat"], meta["lon"],
                                                       meta.get("tz", "UTC"))
        except Exception as exc:                  # noqa: BLE001 — нет погоды, нет поста
            log.warning("%s: прогноз не получен (%s) — утреннего поста не будет", slug, exc)
            return None

        words, emoji = describe_weather(forecast.code)
        title = f"Погода в {meta.get('name_loc') or _prepositional(meta['name'])} на {_date_ru(local)}"
        body = (f"{emoji} Днём {temp(forecast.t_max)}, ночью {temp(forecast.t_min)}, {words}. "
                f"Ветер до {int(round(forecast.wind))} м/с.")
        if forecast.precipitation >= 1:
            body += f" Осадки до {forecast.precipitation:.0f} мм — зонт пригодится."
        return self._publish(slug, WEATHER, title, body, "weather", now)

    # ---------- топ-5 дня ----------

    def _top_post(self, slug: str, city, now: datetime, local: datetime) -> int | None:
        if self._done(TOP, slug, now):
            return None
        rows = self._top_rows(slug, local_day(now, self.cities[slug].get("tz", "UTC")))
        if len(rows) < self.settings.top_min_items:
            log.info("%s: за день опубликовано %s постов — вечернего топа не будет",
                     slug, len(rows))
            return None
        meta = self.cities[slug]
        lines = []
        for i, row in enumerate(rows, 1):
            # Заголовок пришёл от rewriter'а — экранируем; ссылка наша.
            title = html.escape(row["title"] or "Без заголовка")
            lines.append(f"{i}. <a href=\"{row['url']}\">{title}</a>" if row["url"]
                         else f"{i}. {title}")
        where = _prepositional(meta["name"])
        # Неполный список топом не называем: обещание в заголовке должно
        # совпадать с тем, что под ним.
        title = (f"Топ-{self.settings.top_size} дня в {where}"
                 if len(rows) >= self.settings.top_size else f"Главное за день в {where}")
        return self._publish(slug, TOP, title, "\n".join(lines), "city", now)

    def _top_rows(self, city: str, day: str, limit: int | None = None):
        """Посты дня по популярности.

        Просмотры собирает фаза 6 (`published.views`); пока их нет, порядок
        держит число источников кластера — «сколько городских лент об этом
        написало» — а затем время выхода."""
        limit = limit or self.settings.top_size
        return self.db.conn.execute(
            """SELECT p.url, p.views, d.title, d.sources
               FROM published p JOIN drafts d ON d.id = p.draft_id
               WHERE p.city=? AND p.platform='tg' AND p.deleted_at IS NULL
                 AND substr(p.published_at,1,10)=?
                 AND COALESCE(d.origin,'news') != 'service'
               ORDER BY COALESCE(p.views, 0) DESC,
                        LENGTH(COALESCE(d.sources,'')) DESC, p.published_at
               LIMIT ?""", (city, day, limit)).fetchall()

    # ---------- общее ----------

    def _publish(self, slug: str, kind: str, title: str, body: str, category: str,
                 now: datetime) -> int:
        draft_id = drafts_store.save_draft(
            self.db, cluster_id=None, city=slug, title=title, body=body,
            category=category, urgency="normal", origin="service", flags=["service"],
            safety_verdict="safe", notes=f"сервисный автопост ({kind})", status="approved")
        store.enqueue(self.db, draft_id, slug, now)
        self.db.set_meta(self._key(kind, slug, now), str(draft_id))
        log.info("%s: сервисный пост %s — драфт #%s", slug, kind, draft_id)
        return draft_id

    def _done(self, kind: str, slug: str, now: datetime) -> bool:
        return self.db.get_meta(self._key(kind, slug, now)) is not None

    def _key(self, kind: str, slug: str, now: datetime) -> str:
        day = local_day(now, self.cities[slug].get("tz", "UTC"))
        return f"service:{kind}:{slug}:{day}"


def _date_ru(moment: datetime) -> str:
    return f"{moment.day} {MONTHS[moment.month - 1]}"


def _prepositional(name: str) -> str:
    """«Череповец» → «Череповце». Грубо, но по трём городам пилота точно."""
    special = {"Череповец": "Череповце", "Новокузнецк": "Новокузнецке",
               "Магнитогорск": "Магнитогорске"}
    if name in special:
        return special[name]
    return name[:-1] + "е" if name.endswith(("а", "я")) else name + "е"
