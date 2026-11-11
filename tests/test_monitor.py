"""Алерты админ-бота (спека §7): источник молчит > 24 ч, ВК-токен протух,
очередь переполнена, ошибки публикации.

Алерт — это то, что будит человека ночью, поэтому проверяется и обратное:
без повода никто никого не будит.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.moderation.monitor import Monitor
from tgtraff.publisher import store
from tgtraff.publisher.settings import ModerationSettings

NOW = datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc)
CITY = "cherepovets"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


class Alerts:
    def __init__(self):
        self.sent = []

    async def alert(self, message):
        self.sent.append(message)
        return True


def monitor(db, alerts, **kw):
    return Monitor(db, alerts, ModerationSettings(**kw))


def source(db, key, source_type="tg", last_post_hours=1, error=None):
    sid = db.upsert_source(SourceSpec(city=CITY, source_type=source_type, key=key))
    db.conn.execute("UPDATE sources SET last_post_at=?, last_error=? WHERE id=?",
                    ((NOW - timedelta(hours=last_post_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     error, sid))
    return sid


async def test_quiet_pipeline_raises_nothing(db):
    source(db, "example_channel_7", last_post_hours=2)
    alerts = Alerts()
    assert await monitor(db, alerts).check(now=NOW) == []
    assert alerts.sent == []


async def test_silent_source_is_reported(db):
    source(db, "example_channel_7", last_post_hours=30)
    alerts = Alerts()
    messages = await monitor(db, alerts).check(now=NOW)
    assert any("молчит" in m for m in messages)
    assert "example_channel_7" in " ".join(messages)


async def test_silent_sources_are_reported_in_one_message(db):
    for i in range(5):
        source(db, f"dead{i}", last_post_hours=40)
    alerts = Alerts()
    await monitor(db, alerts).check(now=NOW)
    assert len(alerts.sent) == 1


async def test_dead_vk_token_looks_like_all_vk_sources_failing(db):
    for i in range(3):
        source(db, f"pub{i}", source_type="vk", error="VK error 5: auth failed")
    messages = await monitor(db, Alerts()).check(now=NOW)
    assert any("вк" in m.lower() and "токен" in m.lower() for m in messages)


# --- §57: алерт «очередь переполнена» считает только СРОЧНОЕ -----------------
#
# После §55 пул несрочного большой by design (модератор тянет его сам), и старый
# счётчик new+deferred слал новое число каждые 15 минут. Считаем то, что висит
# карточками без решения: молнии, UGC и manual_only.

def urgent(db, n, **kw):
    for i in range(n):
        drafts_store.save_draft(db, cluster_id=None, city=CITY, title=f"#{i}", body="т",
                                urgency="flash", **kw)


async def test_overflowing_urgent_queue_is_reported(db):
    urgent(db, 6)
    messages = await monitor(db, Alerts(), queue_alert_size=5).check(now=NOW)
    assert any("очеред" in m.lower() for m in messages)


async def test_big_pool_of_unhurried_drafts_raises_nothing(db):
    """Главный смысл §57: пул несрочного после §55 велик и алертом не является."""
    for i in range(200):
        drafts_store.save_draft(db, cluster_id=None, city=CITY, title=f"#{i}", body="т")
    alerts = Alerts()
    assert await monitor(db, alerts, queue_alert_size=20).check(now=NOW) == []
    assert alerts.sent == []


async def test_ugc_and_manual_only_count_as_urgent(db):
    for i in range(3):
        drafts_store.save_draft(db, cluster_id=None, city=CITY, title=f"u{i}", body="т",
                                origin="ugc")
    for i in range(3):
        drafts_store.save_draft(db, cluster_id=None, city=CITY, title=f"m{i}", body="т",
                                safety_verdict="manual_only")
    messages = await monitor(db, Alerts(), queue_alert_size=5).check(now=NOW)
    assert any("очеред" in m.lower() for m in messages)


async def test_decided_urgent_drafts_do_not_count(db):
    urgent(db, 6, status="approved")
    assert await monitor(db, Alerts(), queue_alert_size=5).check(now=NOW) == []


async def test_deferred_urgent_draft_still_waits_for_a_decision(db):
    urgent(db, 6, status="deferred")
    messages = await monitor(db, Alerts(), queue_alert_size=5).check(now=NOW)
    assert any("очеред" in m.lower() for m in messages)


async def test_queue_alert_does_not_repeat_while_the_backlog_stays_over(db):
    urgent(db, 6)
    m = monitor(db, Alerts(), queue_alert_size=5)
    assert await m.check(now=NOW)
    urgent(db, 3)                                  # число выросло — текст другой, повода нет
    assert await m.check(now=NOW + timedelta(minutes=15)) == []
    assert await m.check(now=NOW + timedelta(hours=5)) == []


async def test_queue_alert_repeats_after_six_hours(db):
    urgent(db, 6)
    m = monitor(db, Alerts(), queue_alert_size=5)
    assert await m.check(now=NOW)
    messages = await m.check(now=NOW + timedelta(hours=6, minutes=1))
    assert any("очеред" in m_.lower() for m_ in messages)


async def test_queue_alert_is_silent_until_the_threshold_is_crossed_again(db):
    """Дребезг вокруг порога (20-19-21) не будит человека каждым тиком."""
    urgent(db, 6)
    m = monitor(db, Alerts(), queue_alert_size=5)
    assert await m.check(now=NOW)
    db.conn.execute("UPDATE drafts SET status='approved'")          # очередь разобрали
    assert await m.check(now=NOW + timedelta(minutes=15)) == []
    urgent(db, 6)                                  # и она снова переполнилась
    assert await m.check(now=NOW + timedelta(minutes=30)) == []      # но шести часов не прошло


async def test_failed_publication_is_reported(db):
    did = drafts_store.save_draft(db, cluster_id=None, city=CITY, title="Х", body="т",
                                  status="approved")
    qid = store.enqueue(db, did, CITY, NOW)
    db.conn.execute("UPDATE publish_queue SET status='failed', last_error='chat not found' "
                    "WHERE id=?", (qid,))
    messages = await monitor(db, Alerts()).check(now=NOW)
    assert any("публикац" in m.lower() for m in messages)


async def test_stale_queue_item_is_reported(db):
    did = drafts_store.save_draft(db, cluster_id=None, city=CITY, title="Х", body="т",
                                  status="approved")
    store.enqueue(db, did, CITY, NOW - timedelta(hours=20))
    messages = await monitor(db, Alerts()).check(now=NOW)
    assert any("протух" in m.lower() or "ждёт" in m.lower() for m in messages)


async def test_the_same_alert_is_not_repeated_every_tick(db):
    source(db, "example_channel_7", last_post_hours=30)
    alerts = Alerts()
    m = monitor(db, alerts)
    await m.check(now=NOW)
    await m.check(now=NOW + timedelta(minutes=5))
    assert len(alerts.sent) == 1
