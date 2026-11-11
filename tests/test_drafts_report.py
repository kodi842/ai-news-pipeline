"""Отчёт для разметки драфтов оператором (итерации приёмки фазы 4).

Формат ровно один: кластер → драфт → источники, чтобы можно было проверить
каждый факт глазами и поставить оценку в ту же строку.
"""
import pytest

from tgtraff import drafts as store
from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.rewriter.report import collect_report, render_markdown

CITY = "magnitogorsk"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def make_draft(db, **over):
    sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key="example_channel_13"))
    pid = db.insert_raw_post(source_id=sid, source_type="tg", external_id="1",
                             published_at="2026-09-02T09:00:00+00:00",
                             text="На Ленина столкнулись такси и грузовик, пострадал водитель").id
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at)"
        " VALUES (?, 'open', ?, 1, '2026-09-02T09:00:00+00:00','2026-09-02T09:00:00+00:00')",
        (CITY, pid))
    cid = cur.lastrowid
    db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id, reason)"
                    " VALUES (?,?,'new')", (cid, pid))
    kw = dict(cluster_id=cid, city=CITY, title="На Ленина столкнулись такси и грузовик",
              body="Авария произошла утром. Пострадал водитель такси.",
              category="dtp", urgency="flash", safety_verdict="safe",
              relevance="news", media_kind="template", media_reason="no_media",
              sources=["example_channel_13"], model="claude-haiku-4-5", cost_usd=0.0011)
    kw.update(over)
    return store.save_draft(db, **kw)


def test_report_pairs_a_draft_with_its_sources(db):
    make_draft(db)
    data = collect_report(db, city=CITY)
    assert len(data["drafts"]) == 1
    card = data["drafts"][0]
    assert card["title"].startswith("На Ленина")
    assert card["sources"][0]["source"] == "example_channel_13"
    assert "столкнулись такси" in card["sources"][0]["text"]


def test_markdown_shows_draft_facts_and_markup_slot(db):
    make_draft(db)
    md = render_markdown(collect_report(db, city=CITY))
    assert "На Ленина столкнулись такси и грузовик" in md
    assert "Источники кластера" in md
    assert "Оценка" in md                      # место для разметки оператором
    assert "ДТП" in md                          # категория по-русски


def test_flags_and_safety_are_visible_in_the_report(db):
    make_draft(db, safety_verdict="manual_only", safety_reasons=["военное"],
               flags=["low_facts"])
    md = render_markdown(collect_report(db, city=CITY))
    assert "manual_only" in md
    assert "военное" in md
    assert "low_facts" in md


def test_report_counts_money_and_verdicts(db):
    make_draft(db)
    store.log_llm_usage(db, purpose="rewrite", model="m", city=CITY, cluster_id=None,
                        input_tokens=10, output_tokens=5, cache_read=0, cache_write=0,
                        cost_usd=0.002)
    data = collect_report(db, city=CITY)
    assert data["total_cost_usd"] == pytest.approx(0.002)
    assert data["by_safety"]["safe"] == 1


def test_report_can_be_limited(db):
    for i in range(3):
        make_draft(db, title=f"Драфт {i}")
    assert len(collect_report(db, city=CITY, limit=2)["drafts"]) == 2
