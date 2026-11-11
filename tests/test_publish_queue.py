"""Очередь публикаций (спека §8): драфт → слот → доставка → message_id.

Очередь лежит в БД, а не в памяти: рестарт publisher'а не должен ни терять
одобренные посты, ни публиковать их дважды.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.publisher import store

CITY, TZ = "cherepovets", "Europe/Moscow"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def utc(hh, mm=0, day=4):
    return datetime(2026, 9, day, hh, mm, tzinfo=timezone.utc)


def draft(db, city=CITY, **kw):
    return drafts_store.save_draft(db, cluster_id=None, city=city,
                                   title=kw.pop("title", "Хук"), body="Тело",
                                   category="dtp", urgency=kw.pop("urgency", "normal"),
                                   status="approved", **kw)


def test_approved_draft_lands_in_the_queue_with_its_slot(db):
    qid = store.enqueue(db, draft(db), CITY, scheduled_at=utc(15), moderator_id=7)
    row = store.get_queued(db, qid)
    assert row["status"] == "queued"
    assert row["scheduled_at"].startswith("2026-09-04T15:00")
    assert row["moderator_id"] == 7


def test_due_returns_only_items_whose_time_has_come(db):
    store.enqueue(db, draft(db), CITY, scheduled_at=utc(15))
    later = store.enqueue(db, draft(db), CITY, scheduled_at=utc(20))
    due = store.due(db, now=utc(16))
    assert [r["id"] for r in due] != []
    assert later not in [r["id"] for r in due]


def test_claim_is_taken_only_once(db):
    """Два тика publisher'а не должны опубликовать один пост дважды."""
    qid = store.enqueue(db, draft(db), CITY, scheduled_at=utc(15))
    assert store.claim(db, qid) is True
    assert store.claim(db, qid) is False


def test_failed_item_returns_to_the_queue_until_attempts_run_out(db):
    qid = store.enqueue(db, draft(db), CITY, scheduled_at=utc(15))
    store.claim(db, qid)
    store.mark_failed(db, qid, "сеть моргнула", max_attempts=3)
    assert store.get_queued(db, qid)["status"] == "queued"
    for _ in range(2):
        store.claim(db, qid)
        store.mark_failed(db, qid, "сеть моргнула", max_attempts=3)
    row = store.get_queued(db, qid)
    assert row["status"] == "failed" and row["attempts"] == 3


def test_same_draft_cannot_be_queued_twice(db):
    did = draft(db)
    store.enqueue(db, did, CITY, scheduled_at=utc(15))
    with pytest.raises(store.AlreadyQueued):
        store.enqueue(db, did, CITY, scheduled_at=utc(16))


def test_cancelled_draft_can_be_queued_again(db):
    did = draft(db)
    qid = store.enqueue(db, did, CITY, scheduled_at=utc(15))
    store.cancel(db, qid)
    assert store.enqueue(db, did, CITY, scheduled_at=utc(16)) != qid


def test_publication_maps_draft_to_message_id(db):
    did = draft(db)
    store.record_publication(db, draft_id=did, city=CITY, platform="tg",
                             channel="-1001", message_id="42",
                             url="https://t.me/c/1/42", extra_message_ids=["43"])
    rows = store.publications(db, did)
    assert [r["message_id"] for r in rows] == ["42"]
    assert store.decode_extra(rows[0]) == ["43"]


def test_publication_is_marked_deleted_not_erased(db):
    did = draft(db)
    pid = store.record_publication(db, draft_id=did, city=CITY, platform="tg",
                                   channel="-1001", message_id="42")
    store.mark_deleted(db, pid)
    assert store.publications(db, did, include_deleted=True)[0]["deleted_at"]
    assert store.publications(db, did) == []


def test_day_load_counts_scheduled_and_published_of_the_local_day(db):
    store.enqueue(db, draft(db), CITY, scheduled_at=utc(15))        # 18:00 локального
    store.enqueue(db, draft(db), CITY, scheduled_at=utc(19))        # 22:00 локального
    store.enqueue(db, draft(db), CITY, scheduled_at=utc(4, day=5))  # завтра 07:00
    assert store.day_load(db, CITY, TZ, utc(15)) == 2


def test_day_load_ignores_another_city(db):
    store.enqueue(db, draft(db, city="magnitogorsk"), "magnitogorsk", scheduled_at=utc(15))
    assert store.day_load(db, CITY, TZ, utc(15)) == 0


def test_last_planned_at_is_the_latest_scheduled_moment(db):
    store.enqueue(db, draft(db), CITY, scheduled_at=utc(15))
    store.enqueue(db, draft(db), CITY, scheduled_at=utc(19))
    assert store.last_planned_at(db, CITY) == utc(19)


def test_stale_queue_item_is_reported(db):
    """Новость города живёт часы: пост, просидевший в очереди сутки, — протухший."""
    store.enqueue(db, draft(db), CITY, scheduled_at=utc(15, day=3))
    stale = store.stale(db, now=utc(15), older_than_hours=12)
    assert len(stale) == 1
