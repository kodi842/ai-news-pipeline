"""Выборка кластеров для ручной проверки (приёмка фазы 3).

Спека требует «ручную проверку выборки 50 кластеров», поэтому выборка обязана
быть честной (случайной), воспроизводимой (по seed) и полной по фактуре:
кластер → источники → тексты, иначе проверять нечего.
"""
import pytest

from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.dedup.cluster import Deduplicator
from tgtraff.dedup.runner import run_once
from tgtraff.dedup.sample import collect, day_bounds, render_markdown
from tgtraff.dedup.settings import DedupSettings

from tests.test_dedup_cluster import FakeEmbedder

CITY = "magnitogorsk"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def source(db, key, source_type="tg", city=CITY):
    return db.upsert_source(SourceSpec(city=city, source_type=source_type, key=key))


def post(db, source_id, text, at, external_id, source_type="tg"):
    return db.insert_raw_post(source_id=source_id, source_type=source_type,
                              external_id=str(external_id), published_at=at, text=text).id


def build(db, count=6, day="2026-09-02"):
    """Лента дня: `count` разных новостей плюс одна, перепечатанная вторым источником."""
    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    for i in range(count):
        post(db, a, f"Новость номер {i} о происшествии в Магнитогорске на улице Ленина",
             f"{day}T10:{i:02d}:00+00:00", 100 + i)
    post(db, b, "Новость номер 0 о происшествии в Магнитогорске на улице Ленина",
         f"{day}T10:20:00+00:00", 200, source_type="vk")
    run_once(db, Deduplicator(db, FakeEmbedder(), DedupSettings()), city=CITY, limit=100)


def test_day_bounds_cover_exactly_one_day():
    since, until = day_bounds("2026-09-02")
    assert since == "2026-09-02T00:00:00+00:00"
    assert until == "2026-09-03T00:00:00+00:00"


def test_sample_counts_day_and_merged_clusters(db):
    build(db)
    data = collect(db, day="2026-09-02", limit=50, seed=1, only_merged=False)

    assert data["total_clusters_in_day"] == 6      # шесть новостей
    assert data["merged_clusters_in_day"] == 1     # одна собрана из двух источников
    assert data["sample_size"] == 6
    assert data["sample_merged"] == 1


def test_sample_is_capped_by_limit_and_reproducible(db):
    build(db, count=12)
    first = collect(db, day="2026-09-02", limit=5, seed=7, only_merged=False)
    again = collect(db, day="2026-09-02", limit=5, seed=7, only_merged=False)
    other = collect(db, day="2026-09-02", limit=5, seed=8, only_merged=False)

    ids = [c["id"] for c in first["clusters"]]
    assert len(ids) == 5
    assert ids == [c["id"] for c in again["clusters"]]        # тот же seed — та же выборка
    assert ids != [c["id"] for c in other["clusters"]]        # другой seed — другая


def test_only_merged_mode_returns_clusters_with_two_posts(db):
    build(db)
    data = collect(db, day="2026-09-02", limit=50, seed=1, only_merged=True)

    assert data["sample_size"] == 1
    assert all(c["size"] > 1 for c in data["clusters"])


def test_cluster_card_carries_sources_platforms_and_texts(db):
    build(db)
    data = collect(db, day="2026-09-02", limit=50, seed=1, only_merged=True)
    card = data["clusters"][0]

    assert card["sources"] == ["example_channel_5", "verstov_info"]
    assert card["platforms"] == ["tg", "vk"]
    assert len(card["posts"]) == 2
    assert "Новость номер 0" in card["posts"][0]["text"]
    assert card["posts"][0]["reason"] == "new"
    assert card["posts"][1]["reason_label"] == "точный хэш текста"
    assert card["posts"][1]["similarity"] == 1.0


def test_posts_inside_a_cluster_are_chronological(db):
    build(db)
    card = collect(db, day="2026-09-02", limit=50, seed=1, only_merged=True)["clusters"][0]

    times = [p["published_at"] for p in card["posts"]]
    assert times == sorted(times)


def test_other_days_are_not_included(db):
    build(db)
    assert collect(db, day="2026-09-01", limit=50, seed=1, only_merged=False)["clusters"] == []


def test_markdown_report_shows_sources_and_texts(db):
    build(db)
    text = render_markdown(collect(db, day="2026-09-02", limit=50, seed=1, only_merged=True))

    assert "Проверочная выборка кластеров — 2026-09-02" in text
    assert "example_channel_5" in text and "verstov_info" in text
    assert "Новость номер 0" in text
    assert "точный хэш текста" in text
