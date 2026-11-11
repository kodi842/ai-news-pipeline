"""Хранилище драфтов фазы 4: что кладётся в базу и что из неё берёт очередь.

Драфт — результат всей фазы: текст, категория, медиа, вердикт safety и
причина relevance. Фаза 5 (модерация) читает ровно эти строки.
"""
import pytest

from tgtraff import drafts as store
from tgtraff.config import SourceSpec
from tgtraff.db import Database

CITY = "cherepovets"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def source(db, key="chereptoday", official=False, source_type="tg"):
    return db.upsert_source(SourceSpec(city=CITY, source_type=source_type, key=key,
                                       official=official))


def post(db, source_id, text="Новость", at="2026-09-02T08:00:00+00:00"):
    return db.insert_raw_post(source_id=source_id, source_type="tg",
                              external_id=str(abs(hash((source_id, text, at))) % 10**7),
                              published_at=at, text=text).id


def cluster(db, *post_ids):
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at)"
        " VALUES (?, 'open', ?, ?, '2026-09-02T08:00:00+00:00', '2026-09-02T08:00:00+00:00')",
        (CITY, post_ids[0], len(post_ids)))
    cid = cur.lastrowid
    for pid in post_ids:
        db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id, reason)"
                        " VALUES (?,?,'new')", (cid, pid))
    return cid


# --- источники: официальность нужна медиа-каскаду --------------------------

def test_official_flag_of_a_source_is_stored(db):
    sid = source(db, "vologdaobl_mchs", official=True)
    assert db.get_source(sid)["official"] == 1


def test_ordinary_source_is_not_official(db):
    assert db.get_source(source(db, "chereptoday"))["official"] == 0


# --- драфты ----------------------------------------------------------------

def test_draft_is_saved_with_verdicts_and_media(db):
    cid = cluster(db, post(db, source(db)))
    draft_id = store.save_draft(
        db, cluster_id=cid, city=CITY, title="Хук", body="Текст поста",
        category="dtp", safety_verdict="manual_only", safety_reasons=["военное"],
        flags=["low_facts"], relevance="news", media_kind="photo",
        media_reason="официальный источник", media_path="/tmp/a.jpg",
        model="claude-haiku-4-5", cost_usd=0.0012, notes="")

    row = store.get_draft(db, draft_id)
    assert row["title"] == "Хук"
    assert row["safety_verdict"] == "manual_only"
    assert store.decode_list(row["safety_reasons"]) == ["военное"]
    assert store.decode_list(row["flags"]) == ["low_facts"]
    assert row["status"] == "new"
    assert row["cost_usd"] == pytest.approx(0.0012)


def test_second_draft_for_the_same_cluster_is_refused(db):
    """Спека §3: пост в кластер с драфтом второй публикации не порождает."""
    cid = cluster(db, post(db, source(db)))
    store.save_draft(db, cluster_id=cid, city=CITY, title="Раз", body="т")
    assert store.draft_for_cluster(db, cid) is not None
    with pytest.raises(store.DraftExists):
        store.save_draft(db, cluster_id=cid, city=CITY, title="Два", body="т")


# --- relevance-вердикт кластера --------------------------------------------

def test_relevance_verdict_is_stored_on_the_cluster(db):
    cid = cluster(db, post(db, source(db)))
    store.set_cluster_relevance(db, cid, "ad", "erid")
    row = db.conn.execute("SELECT relevance, relevance_reason FROM clusters WHERE id=?",
                          (cid,)).fetchone()
    assert (row["relevance"], row["relevance_reason"]) == ("ad", "erid")


# --- очередь на обработку --------------------------------------------------

def test_pending_clusters_skips_the_ones_already_processed(db):
    s = source(db)
    fresh = cluster(db, post(db, s, "Свежий"))
    done = cluster(db, post(db, s, "Уже разобранный"))
    dropped = cluster(db, post(db, s, "Реклама"))
    store.save_draft(db, cluster_id=done, city=CITY, title="t", body="b")
    store.set_cluster_relevance(db, dropped, "ad", "erid")

    assert store.pending_clusters(db, city=CITY) == [fresh]


def test_pending_clusters_are_ordered_oldest_first(db):
    s = source(db)
    late = cluster(db, post(db, s, "Позже", at="2026-09-02T12:00:00+00:00"))
    early = cluster(db, post(db, s, "Раньше", at="2026-09-02T06:00:00+00:00"))
    db.conn.execute("UPDATE clusters SET first_seen_at=? WHERE id=?",
                    ("2026-09-02T12:00:00+00:00", late))
    db.conn.execute("UPDATE clusters SET first_seen_at=? WHERE id=?",
                    ("2026-09-02T06:00:00+00:00", early))
    assert store.pending_clusters(db, city=CITY) == [early, late]


# --- учёт денег ------------------------------------------------------------

def test_llm_usage_is_written_and_summed(db):
    store.log_llm_usage(db, purpose="rewrite", model="claude-haiku-4-5", city=CITY,
                        cluster_id=None, input_tokens=100, output_tokens=50,
                        cache_read=10, cache_write=5, cost_usd=0.0005)
    store.log_llm_usage(db, purpose="safety", model="claude-haiku-4-5", city=CITY,
                        cluster_id=None, input_tokens=80, output_tokens=20,
                        cache_read=0, cache_write=0, cost_usd=0.0002)
    total = store.usage_total(db)
    assert total["calls"] == 2
    assert total["cost_usd"] == pytest.approx(0.0007)


# --- вотермарки медиа ------------------------------------------------------

def test_watermark_verdict_is_cached_on_the_media_row(db):
    pid = post(db, source(db))
    media_id = db.add_media(pid, 0, "photo")
    store.set_media_watermark(db, media_id, "foreign", "Магнитогорск 24/7")
    row = db.conn.execute("SELECT watermark, watermark_owner, watermark_checked_at"
                          " FROM raw_media WHERE id=?", (media_id,)).fetchone()
    assert row["watermark"] == "foreign"
    assert row["watermark_owner"] == "Магнитогорск 24/7"
    assert row["watermark_checked_at"]
