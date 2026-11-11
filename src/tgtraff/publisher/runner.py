"""Процесс публикации (спека §8).

Тик: вернуть застрявшее → взять из очереди то, чему пришло время → доставить
во все настроенные платформы города → записать message_id.

Гарантии:
  * пост не выходит раньше своего слота (за время отвечает планировщик);
  * пост не выходит дважды (`claim`: queued→sending одним UPDATE);
  * упавшая доставка возвращает запись в очередь, а исчерпание попыток —
    алерт в админ-бот (спека §7: «ошибки публикации»);
  * падение ВК не отменяет Telegram: доставки независимы.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tgtraff import drafts as drafts_store
from tgtraff.moderation import actions
from tgtraff.publisher import store
from tgtraff.publisher.delivery import DeliveryError, DeliveryResult
from tgtraff.publisher.format import PostContent, render_plain, render_post
from tgtraff.publisher.settings import PublisherSettings

log = logging.getLogger("tgtraff.publisher")


@dataclass
class PublishOutcome:
    queue_id: int
    draft_id: int
    city: str
    status: str                      # published | retry | failed | skipped
    results: list[DeliveryResult] = field(default_factory=list)
    error: str | None = None


def content_of(row) -> PostContent:
    """Строка драфта → содержимое поста. Драфт конвейера, UGC и сервисный
    автопост попадают сюда одинаково — публикация про их происхождение не знает."""
    return PostContent(
        city=row["city"], title=row["title"] or "", body=row["body"] or "",
        category=row["category"] or "other", urgency=row["urgency"] or "normal",
        district=row["district"], media_path=row["media_path"],
        flags=tuple(drafts_store.decode_list(row["flags"])),
        body_html=(row["origin"] == "service"))


class Publisher:
    def __init__(self, db, channels, deliveries: dict, settings: PublisherSettings | None = None,
                 alerter=None):
        self.db = db
        self.channels = channels
        self.deliveries = deliveries
        self.settings = settings or PublisherSettings()
        self.alerter = alerter
        # Крючок для админ-бота: карточка драфта после публикации меняет вид.
        self.on_published = None

    # ---------- цикл ----------

    async def run_forever(self) -> None:
        log.info("publisher: режим %s, города %s", self.channels.mode,
                 ", ".join(self.channels.ready_cities()) or "нет настроенных")
        while True:
            try:
                await self.tick()
            except Exception as exc:              # noqa: BLE001 — тик не имеет права убить процесс
                log.exception("тик publisher'а упал: %s", exc)
            await asyncio.sleep(self.settings.poll_seconds)

    async def tick(self, now: datetime | None = None) -> list[PublishOutcome]:
        now = now or datetime.now(timezone.utc)
        store.release_stuck(self.db)
        outcomes = []
        for row in store.due(self.db, now):
            if not store.claim(self.db, row["id"]):
                continue                          # запись уже взял другой тик
            outcomes.append(await self._publish(row))
        return outcomes

    # ---------- одна запись очереди ----------

    async def _publish(self, row) -> PublishOutcome:
        queue_id, draft_id, city_slug = row["id"], row["draft_id"], row["city"]
        draft = drafts_store.get_draft(self.db, draft_id)
        city = self.channels.city(city_slug)

        if draft is None or city is None or not city.targets():
            store.cancel(self.db, queue_id)
            reason = (f"город {city_slug} без настроенного канала"
                      if draft is not None else f"драфт #{draft_id} исчез")
            await self._alert(f"публикация пропущена: {reason}")
            return PublishOutcome(queue_id, draft_id, city_slug, "skipped", error=reason)

        post = content_of(draft)
        results, errors = [], []
        for platform in city.targets():
            delivery = self.deliveries.get(platform)
            if delivery is None:
                continue
            try:
                results.append(await self._deliver(delivery, platform, city, post))
            except DeliveryError as exc:
                errors.append(f"{platform}: {exc}")
                log.warning("драфт #%s: доставка в %s не прошла (%s)", draft_id, platform, exc)

        if not results:
            error = "; ".join(errors) or "нет доставок"
            status = store.mark_failed(self.db, queue_id, error,
                                       self.settings.max_attempts)
            if status == "failed":
                await self._alert(f"публикация драфта #{draft_id} провалена: {error}")
            return PublishOutcome(queue_id, draft_id, city_slug,
                                  "failed" if status == "failed" else "retry", error=error)

        for result in results:
            store.record_publication(
                self.db, draft_id=draft_id, city=city_slug, platform=result.platform,
                channel=result.channel, message_id=result.message_id, url=result.url,
                extra_message_ids=result.extra_message_ids)
        store.mark_published(self.db, queue_id)
        actions.mark_published(self.db, draft_id)
        if errors:                                # часть площадок отвалилась — это не отказ
            await self._alert(f"драфт #{draft_id} вышел не везде: {'; '.join(errors)}")
        log.info("драфт #%s опубликован: %s", draft_id,
                 ", ".join(f"{r.platform}#{r.message_id}" for r in results))

        outcome = PublishOutcome(queue_id, draft_id, city_slug, "published", results)
        if self.on_published:
            await _maybe_await(self.on_published(outcome))
        return outcome

    async def _deliver(self, delivery, platform: str, city, post: PostContent):
        if platform == "tg":
            rendered = render_post(post, city, self.channels.ugc_bot)
            return await delivery.publish(city, rendered, post.media_path)
        return await delivery.publish(city, render_plain(post, city, self.channels.ugc_bot),
                                      post.media_path)

    async def _alert(self, message: str) -> None:
        log.error("%s", message)
        if self.alerter is not None:
            await _maybe_await(self.alerter.alert(message))


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value
