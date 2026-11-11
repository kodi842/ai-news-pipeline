"""Дневной дайджест в админ-бот (спека §9).

Это единственная сводка, которую владелец видит каждый день, и она же —
черновик отчёта к гейту месяца 2. Отсюда три правила:

  * цифры гейта первыми: подписчики, суточный прирост, охват, CPF по площадкам;
  * там, где замера нет, стоит прочерк, а не ноль: «никто не читает» и «мы не
    смотрели» — разные утверждения, и в решении о победителе они стоят по-разному;
  * дайджест уходит и в пустой день. Молчание конвейера, вставшего ночью,
    выглядит ровно как молчание конвейера, которому нечего сказать.

Идемпотентность — по локальным суткам (та же меточная схема, что у сервисных
автопостов): рестарт процесса не пришлёт вторую сводку.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from tgtraff.metrics import store
from tgtraff.metrics.report import cpf_report
from tgtraff.metrics.settings import MetricsSettings
from tgtraff.publisher.schedule import local_day, zone

log = logging.getLogger("tgtraff.metrics.digest")

META_KEY = "digest:last_day"
# Насколько поздно ещё уместно прислать сводку за день, если процесс лежал.
CATCH_UP_HOURS = 2
DASH = "—"


class DailyDigest:
    def __init__(self, db, notify, channels, settings: MetricsSettings | None = None,
                 city_names: dict[str, str] | None = None,
                 city_tz: dict[str, str] | None = None):
        self.db = db
        self.notify = notify                      # async callable(text)
        self.channels = channels
        self.settings = settings or MetricsSettings()
        self.city_names = city_names or {}
        self.city_tz = city_tz or {}

    # ---------- расписание ----------

    async def ensure(self, now: datetime | None = None) -> str | None:
        if not (self.settings.enabled and self.settings.digest_enabled):
            return None
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(zone(self.settings.digest_tz))
        day = local.strftime("%Y-%m-%d")
        hour = self.settings.digest_hour
        if not (hour <= local.hour < hour + CATCH_UP_HOURS):
            return None
        if self.db.get_meta(META_KEY) == day:
            return None

        text = self.build(now)
        await self.notify(text)
        self.db.set_meta(META_KEY, day)
        log.info("дайджест за %s отправлен", day)
        return text

    # ---------- содержание ----------

    def build(self, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(hours=24)
        local = now.astimezone(zone(self.settings.digest_tz))
        lines = [f"📊 <b>Дайджест за {local:%d.%m}</b>"]

        for slug in self.channels.cities:
            lines += self._city_block(slug, now, since)

        lines += self._traffic_block(now)
        lines += self._pipeline_block(now, since)
        return "\n".join(lines)

    def _city_block(self, slug: str, now: datetime, since: datetime) -> list[str]:
        name = self.city_names.get(slug, slug)
        snapshot = store.last_snapshot(self.db, slug, before=now)
        reach = store.reach(self.db, slug, since, now)
        growth = store.growth(self.db, slug, now)

        subs = _num(snapshot["subscribers"]) if snapshot else DASH
        delta = f" ({growth:+d})" if growth is not None else ""
        er = f"{snapshot['er'] * 100:.1f}%" if snapshot and snapshot["er"] else DASH
        avg = _num(reach["avg"]) if reach["avg"] is not None else DASH

        out = [f"\n<b>{html.escape(name)}</b>",
               f"подписчиков: {subs}{delta} · постов за сутки: {reach['posts']}",
               f"средний охват: {avg} · ER: {er}"]

        top = self._top_posts(slug, since, now)
        if top:
            out.append("лучшее за день:")
            out += [f"  {i}. {html.escape(t['title'] or 'без заголовка')} — {_num(t['views'])}"
                    for i, t in enumerate(top, 1)]
        return out

    def _top_posts(self, city: str, since: datetime, now: datetime):
        return self.db.conn.execute(
            """SELECT d.title, p.views FROM published p
               JOIN drafts d ON d.id = p.draft_id
               WHERE p.city=? AND p.platform='tg' AND p.deleted_at IS NULL
                 AND p.published_at >= ? AND p.views IS NOT NULL
                 AND COALESCE(d.origin,'news') != 'service'
               ORDER BY p.views DESC LIMIT ?""",
            (city, store.iso(since), self.settings.digest_top)).fetchall()

    def _traffic_block(self, now: datetime) -> list[str]:
        report = cpf_report(self.db, now=now, city_tz=self.city_tz, days=1)
        rows = [r for r in report.rows if r.joins_window or r.cost]
        if not rows:
            return []
        out = ["\n<b>Трафик</b>"]
        for row in sorted(rows, key=lambda r: -r.joins_window):
            cpf = f", CPF {row.cpf:.0f} ₽" if row.cpf is not None else ""
            out.append(f"  {row.name}: +{row.joins_window} за сутки (всего {row.joins}){cpf}")
        if report.cpf is not None:
            out.append(f"  средний CPF закупа: {report.cpf:.2f} ₽")
        return out

    def _pipeline_block(self, now: datetime, since: datetime) -> list[str]:
        c = self.db.conn
        waiting = c.execute(
            "SELECT COUNT(*) n FROM drafts WHERE status IN ('new','deferred')").fetchone()["n"]
        ugc = c.execute(
            "SELECT COUNT(*) n FROM drafts WHERE COALESCE(origin,'news')='ugc' AND created_at >= ?",
            (store.iso(since),)).fetchone()["n"]
        rejected = c.execute(
            "SELECT COUNT(*) n FROM drafts WHERE status='rejected' AND decided_at >= ?",
            (store.iso(since),)).fetchone()["n"]
        # §55: протухшие без решения — отдельный счётчик. Он же мера того,
        # сколько конвейер пишет впустую: много протухших = много лишнего.
        stale = c.execute(
            "SELECT COUNT(*) n FROM drafts WHERE status='stale' AND decided_at >= ?",
            (store.iso(since),)).fetchone()["n"]
        # §59: сколько канал выпустил сам, без человека. Отдельной строкой —
        # с неё начинается разбор, если в ленте окажется что-то не то.
        auto = c.execute(
            "SELECT COUNT(*) n FROM drafts WHERE autopublished_at >= ?",
            (store.iso(since),)).fetchone()["n"]
        spend = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) s FROM llm_usage WHERE created_at >= ?",
            (store.iso(since),)).fetchone()["s"]
        queued = c.execute(
            "SELECT COUNT(*) n FROM publish_queue WHERE status='queued'").fetchone()["n"]

        out = ["\n<b>Конвейер</b>",
               f"ждут решения: {waiting} · в очереди на выход: {queued} · "
               f"отклонено за сутки: {rejected} · протухло за сутки: {stale}",
               f"🤖 автопостом за сутки: {auto} (§59, без модератора)",
               f"UGC за сутки: {ugc} · счёт модели: ${spend:.2f}"]

        total, measured = store.views_coverage(self.db, now)
        if total and measured < total:
            out.append(f"⚠️ просмотры сняты у {measured} из {total} постов суток — "
                       f"проверьте, что юзербот подписан на наши каналы")
        return out


def _num(value) -> str:
    if value is None:
        return DASH
    if isinstance(value, float):
        return f"{value:,.0f}".replace(",", " ")
    return f"{int(value):,}".replace(",", " ")
