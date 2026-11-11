"""Ретеншн фазы 6: `filtered_out` старше 30 дней и медиа по политике.

Живой замер перед фазой 6: 2,8 ГБ медиа за три недели сбора (~130 МБ в сутки).
На VPS это главный потребитель диска, поэтому уборка — не украшение, а условие
трёхсуточного прогона.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.metrics.retention import Retention
from tgtraff.metrics.settings import RetentionSettings

NOW = datetime(2026, 9, 5, 4, 30, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def source(db):
    return db.upsert_source(SourceSpec(city="cherepovets", source_type="tg", key="src"))


def post(db, source_id, days_ago=0, filtered=0, media: str | None = None, tmp_path=None):
    published_at = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = db.insert_raw_post(source_id, "tg", f"m{days_ago}-{filtered}-{media}", published_at,
                             text="текст", filtered_out=filtered)
    if media:
        path = tmp_path / media
        path.write_bytes(b"x" * 1024)
        media_id = db.add_media(res.id, 0, "photo")
        db.mark_media_downloaded(media_id, str(path), 1024, "sha", "image/jpeg")
        return res.id, media_id, path
    return res.id, None, None


def retention(db, **kw):
    return Retention(db, RetentionSettings(**kw))


# ---- filtered_out: 30 дней по спеке ----

def test_old_filtered_out_posts_are_deleted(db, source, tmp_path):
    old, _, _ = post(db, source, days_ago=40, filtered=1, tmp_path=tmp_path)
    fresh, _, _ = post(db, source, days_ago=5, filtered=1, tmp_path=tmp_path)

    result = retention(db).run(now=NOW)

    assert result.filtered_out == 1
    left = {r["id"] for r in db.conn.execute("SELECT id FROM raw_posts")}
    assert left == {fresh}


def test_normal_posts_are_never_touched_by_that_rule(db, source, tmp_path):
    """Правило спеки — только про посты, не прошедшие фильтр города. Обычная
    лента остаётся: на ней калибруются дедуп и relevance."""
    kept, _, _ = post(db, source, days_ago=200, filtered=0, tmp_path=tmp_path)

    retention(db).run(now=NOW)

    assert db.conn.execute("SELECT COUNT(*) c FROM raw_posts").fetchone()["c"] == 1
    assert kept


def test_files_of_deleted_posts_leave_the_disk(db, source, tmp_path):
    _, _, path = post(db, source, days_ago=40, filtered=1, media="old.jpg", tmp_path=tmp_path)

    retention(db).run(now=NOW)

    assert not path.exists(), "запись удалили, а файл остался бы висеть навсегда"


# ---- медиа-политика ----

def test_media_older_than_the_window_leaves_the_disk(db, source, tmp_path):
    _, media_id, path = post(db, source, days_ago=30, media="a.jpg", tmp_path=tmp_path)

    result = retention(db, media_days=14).run(now=NOW)

    assert not path.exists()
    assert result.media_files == 1
    assert result.media_bytes == 1024
    row = db.conn.execute("SELECT * FROM raw_media WHERE id=?", (media_id,)).fetchone()
    assert row["file_path"] is None
    assert row["sha256"] == "sha", "хэши остаются: по ним работает дедуп"


def test_fresh_media_stays(db, source, tmp_path):
    _, _, path = post(db, source, days_ago=3, media="b.jpg", tmp_path=tmp_path)
    retention(db, media_days=14).run(now=NOW)
    assert path.exists()


def test_media_of_a_published_post_is_never_deleted(db, source, tmp_path):
    """Файл, который ушёл в канал, нужен для правки и перевыпуска поста —
    политика возраста его не касается."""
    post_id, media_id, path = post(db, source, days_ago=60, media="c.jpg", tmp_path=tmp_path)
    draft_id = drafts_store.save_draft(db, cluster_id=None, city="cherepovets", title="т",
                                       body="т", media_path=str(path), status="published")
    db.conn.execute("UPDATE drafts SET media_raw_media_id=? WHERE id=?", (media_id, draft_id))

    retention(db, media_days=14).run(now=NOW)

    assert path.exists()


def test_a_missing_file_is_not_an_error(db, source, tmp_path):
    """Файл могли убрать руками — уборка должна пережить это молча."""
    _, _, path = post(db, source, days_ago=30, media="d.jpg", tmp_path=tmp_path)
    path.unlink()

    result = retention(db, media_days=14).run(now=NOW)
    assert result.media_files == 1


# ---- расписание ----

def test_retention_runs_once_a_day(db, source, tmp_path):
    post(db, source, days_ago=40, filtered=1, tmp_path=tmp_path)
    r = retention(db)

    assert r.run(now=NOW).filtered_out == 1
    post(db, source, days_ago=41, filtered=1, tmp_path=tmp_path)
    assert r.run(now=NOW + timedelta(hours=2)) is None, "второй раз за сутки — не работаем"
    assert r.run(now=NOW + timedelta(days=1)) is not None


def test_disabled_retention_does_nothing(db, source, tmp_path):
    post(db, source, days_ago=40, filtered=1, tmp_path=tmp_path)
    assert retention(db, enabled=False).run(now=NOW) is None
    assert db.conn.execute("SELECT COUNT(*) c FROM raw_posts").fetchone()["c"] == 1


def test_forced_run_ignores_the_daily_lock(db, source, tmp_path):
    post(db, source, days_ago=40, filtered=1, tmp_path=tmp_path)
    r = retention(db)
    r.run(now=NOW)
    post(db, source, days_ago=41, filtered=1, tmp_path=tmp_path)
    assert r.run(now=NOW, force=True).filtered_out == 1


# ---- долгие ряды ----

def test_snapshots_and_bills_survive_the_whole_pilot(db):
    """Снимки и счета — данные гейта месяца 2; годовой ретеншн их не трогает."""
    db.conn.execute(
        """INSERT INTO subs_metrics (city, platform, channel, captured_at, subscribers)
           VALUES ('cherepovets','tg','-1001','2026-07-01T00:00:00Z', 100)""")
    retention(db, subs_metrics_days=400).run(now=NOW)
    assert db.conn.execute("SELECT COUNT(*) c FROM subs_metrics").fetchone()["c"] == 1
