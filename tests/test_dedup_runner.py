"""Очередь дедупа: что берётся в работу, в каком порядке и что не берётся дважды."""
import pytest

from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.dedup.cluster import Deduplicator
from tgtraff.dedup.runner import close_stale, pending_feed, run_once, watch
from tgtraff.dedup.settings import DedupSettings
from tgtraff.dedup.store import cluster_of

from tests.test_dedup_cluster import FakeEmbedder

CITY = "magnitogorsk"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def source(db, key, source_type="tg", city=CITY):
    return db.upsert_source(SourceSpec(city=city, source_type=source_type, key=key))


def post(db, source_id, text, at, external_id, grouped_id=None, filtered_out=0,
         source_type="tg"):
    return db.insert_raw_post(source_id=source_id, source_type=source_type,
                              external_id=str(external_id), published_at=at, text=text,
                              grouped_id=grouped_id, filtered_out=filtered_out).id


def dedup(db, **overrides):
    return Deduplicator(db, FakeEmbedder(), DedupSettings(**overrides))


def test_feed_is_chronological(db):
    a = source(db, "example_channel_5")
    post(db, a, "Вторая новость про происшествие в городе", "2026-09-01T12:00:00+00:00", 2)
    post(db, a, "Первая новость про происшествие в городе", "2026-09-01T10:00:00+00:00", 1)

    items = pending_feed(db, city=CITY)

    assert [i.external_id for i in items] == ["1", "2"]


def test_clustered_posts_leave_the_queue(db):
    a = source(db, "example_channel_5")
    post(db, a, "Новость про коммунальную аварию на улице Труда", "2026-09-01T10:00:00+00:00", 1)

    run_once(db, dedup(db), city=CITY)

    assert pending_feed(db, city=CITY) == []


def test_album_tail_does_not_return_to_the_queue(db):
    """Хвост альбома участником кластера не становится — но и в очереди
    висеть вечно не должен, иначе он вытеснит свежие посты."""
    a = source(db, "example_channel_5")
    post(db, a, "Пожар на складе: фоторепортаж", "2026-09-01T10:00:00+00:00", 100, grouped_id="7")
    post(db, a, "", "2026-09-01T10:00:01+00:00", 101, grouped_id="7")
    post(db, a, "", "2026-09-01T10:00:02+00:00", 102, grouped_id="7")

    stats = run_once(db, dedup(db), city=CITY)

    assert stats.processed == 1
    assert pending_feed(db, city=CITY) == []


def test_album_is_not_cut_in_half_by_the_limit(db):
    a = source(db, "example_channel_5")
    post(db, a, "Пожар на складе: фоторепортаж", "2026-09-01T10:00:00+00:00", 100, grouped_id="7")
    post(db, a, "", "2026-09-01T10:00:01+00:00", 101, grouped_id="7")
    post(db, a, "", "2026-09-01T10:00:02+00:00", 102, grouped_id="7")

    [item] = pending_feed(db, city=CITY, limit=1)

    assert len(item.raw_post_ids) == 3


def test_filtered_out_posts_never_enter_the_feed(db):
    """Областной пост без упоминания города новостью не является (QUESTIONS §3)."""
    a = source(db, "incident_kuzbass", "tg_regional")
    post(db, a, "Происшествие в Кемерове, к нашему городу отношения не имеет",
         "2026-09-01T10:00:00+00:00", 1, filtered_out=1, source_type="tg_regional")

    assert pending_feed(db, city=CITY) == []


def test_run_once_counts_new_and_merged(db):
    a, b = source(db, "example_channel_5"), source(db, "verstov", "vk")
    text = "В Магнитогорске сгорел склад на улице Ленина, пострадавших нет."
    post(db, a, text, "2026-09-01T10:00:00+00:00", 1)
    post(db, b, text, "2026-09-01T10:20:00+00:00", 2, source_type="vk")

    stats = run_once(db, dedup(db), city=CITY)

    assert (stats.processed, stats.new_clusters, stats.merged) == (2, 1, 1)
    assert stats.by_reason == {"new": 1, "exact_text": 1}
    assert "склеек 1" in stats.line()


def test_run_once_respects_the_city(db):
    a = source(db, "example_channel_5", city="magnitogorsk")
    b = source(db, "chp_nvkz", city="novokuznetsk")
    p1 = post(db, a, "Новость Магнитогорска про аварию на теплотрассе", "2026-09-01T10:00:00+00:00", 1)
    p2 = post(db, b, "Новость Новокузнецка про аварию на теплотрассе", "2026-09-01T10:00:00+00:00", 2)

    run_once(db, dedup(db), city="magnitogorsk")

    assert cluster_of(db.conn, p1) is not None
    assert cluster_of(db.conn, p2) is None


def test_stale_clusters_are_closed(db):
    a = source(db, "example_channel_5")
    post(db, a, "Старая новость про аварию на сетях водоканала", "2026-09-01T10:00:00+00:00", 1)
    d = Deduplicator(db, FakeEmbedder(), DedupSettings(), now="2026-09-05T10:00:00+00:00")
    run_once(db, d, city=CITY)

    assert close_stale(db, d) == 1
    assert db.conn.execute("SELECT status FROM clusters").fetchone()["status"] == "closed"


def test_fresh_clusters_stay_open(db):
    a = source(db, "example_channel_5")
    post(db, a, "Свежая новость про аварию на сетях водоканала", "2026-09-01T10:00:00+00:00", 1)
    d = Deduplicator(db, FakeEmbedder(), DedupSettings(), now="2026-09-01T12:00:00+00:00")
    run_once(db, d, city=CITY)

    assert close_stale(db, d) == 0
    assert db.conn.execute("SELECT status FROM clusters").fetchone()["status"] == "open"


def test_watch_once_processes_all_cities(db):
    a = source(db, "example_channel_5", city="magnitogorsk")
    b = source(db, "chp_nvkz", city="novokuznetsk")
    p1 = post(db, a, "Новость Магнитогорска про аварию на теплотрассе", "2026-09-01T10:00:00+00:00", 1)
    p2 = post(db, b, "Новость Новокузнецка про аварию на теплотрассе", "2026-09-01T10:00:00+00:00", 2)

    watch(db, dedup(db), cities=["magnitogorsk", "novokuznetsk"], once=True)

    assert cluster_of(db.conn, p1) is not None and cluster_of(db.conn, p2) is not None
