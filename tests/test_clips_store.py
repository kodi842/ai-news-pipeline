"""Хранилище клипов (VIDEO-SPEC, фаза V1).

Клип — это строка `clips`: у него есть драфт-родитель, режим сборки, сценарий,
файл и решение модератора. Очередь рендера лежит в БД и рендерится
ПАРАЛЛЕЛЬНОСТЬЮ 1 (спека: «рендер очередью, параллельность 1») — за это
отвечает claim: planned → rendering одним UPDATE.
"""
import pytest

from tgtraff.clips import store
from tgtraff.db import Database

CITY = "magnitogorsk"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def draft(db, city=CITY, status="published", category="dtp"):
    return db.conn.execute(
        "INSERT INTO drafts (city, title, body, category, safety_verdict, status)"
        " VALUES (?,?,?,?,'safe',?)",
        (city, "Хук", "Тело новости", category, status)).lastrowid


def test_save_clip_lands_with_status_planned(db):
    clip_id = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video",
                              score=8, hook="Автобус смял столб",
                              lines=["Первая строка", "Вторая строка"], cta="Подробности в ТГ")
    row = store.get_clip(db, clip_id)
    assert row["status"] == "planned"
    assert row["mode"] == "video"
    assert store.decode_lines(row) == ["Первая строка", "Вторая строка"]


def test_one_clip_per_draft(db):
    draft_id = draft(db)
    store.save_clip(db, draft_id=draft_id, city=CITY, mode="video", score=8, hook="х")
    with pytest.raises(store.ClipExists):
        store.save_clip(db, draft_id=draft_id, city=CITY, mode="photos", score=9, hook="х")


def test_rejected_clip_does_not_block_second_attempt(db):
    """Отклонённый клип драфта не мешает собрать клип заново позже."""
    draft_id = draft(db)
    first = store.save_clip(db, draft_id=draft_id, city=CITY, mode="video", score=8, hook="х")
    store.mark_rejected(db, first, moderator_id=1, reason="плохой хук")
    second = store.save_clip(db, draft_id=draft_id, city=CITY, mode="video", score=8, hook="ы")
    assert second != first


def test_claim_render_is_taken_once(db):
    clip_id = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="х")
    assert store.claim_render(db, clip_id) is True
    assert store.claim_render(db, clip_id) is False
    assert store.get_clip(db, clip_id)["status"] == "rendering"


def test_pending_render_skips_claimed(db):
    a = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="a")
    b = store.save_clip(db, draft_id=draft(db), city=CITY, mode="photos", score=7, hook="b")
    store.claim_render(db, a)
    assert [r["id"] for r in store.pending_render(db)] == [b]


def test_pending_render_takes_best_score_first(db):
    low = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=6, hook="low")
    high = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=9, hook="high")
    assert [r["id"] for r in store.pending_render(db)] == [high, low]


def test_mark_rendered_records_file(db, tmp_path):
    clip_id = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="х")
    store.claim_render(db, clip_id)
    store.mark_rendered(db, clip_id, path="/tmp/a.mp4", size=1024, duration=22.5,
                        subtitles="whisper")
    row = store.get_clip(db, clip_id)
    assert row["status"] == "ready"
    assert row["file_path"] == "/tmp/a.mp4"
    assert row["duration_sec"] == pytest.approx(22.5)
    assert row["subtitles"] == "whisper"
    assert row["rendered_at"]


def test_failed_render_returns_to_queue_until_attempts_run_out(db):
    clip_id = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="х")
    store.claim_render(db, clip_id)
    assert store.mark_failed(db, clip_id, "ffmpeg упал", max_attempts=2) == "planned"
    store.claim_render(db, clip_id)
    assert store.mark_failed(db, clip_id, "ffmpeg упал", max_attempts=2) == "failed"
    assert "ffmpeg" in store.get_clip(db, clip_id)["last_error"]


def test_release_stuck_returns_rendering_to_queue(db):
    """Процесс убили посреди рендера — клип не имеет права пропасть."""
    clip_id = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="х")
    store.claim_render(db, clip_id)
    db.conn.execute("UPDATE clips SET render_started_at='2020-01-01T00:00:00Z' WHERE id=?",
                    (clip_id,))
    assert store.release_stuck(db, older_than_minutes=15) == 1
    assert store.get_clip(db, clip_id)["status"] == "planned"


def test_ready_for_card_only_undelivered(db):
    clip_id = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="х")
    store.claim_render(db, clip_id)
    store.mark_rendered(db, clip_id, path="/tmp/a.mp4", size=10, duration=20, subtitles="none")
    assert [r["id"] for r in store.ready_for_card(db)] == [clip_id]
    store.mark_card(db, clip_id, chat_id=5, message_id=77)
    assert store.ready_for_card(db) == []
    assert store.clip_of_card(db, 77) == clip_id


def test_decisions_are_recorded(db):
    clip_id = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="х")
    store.mark_published(db, clip_id, moderator_id=42)
    row = store.get_clip(db, clip_id)
    assert row["status"] == "published"
    assert row["moderator_id"] == 42
    assert row["decided_at"]


def test_made_today_counts_local_day_of_city(db):
    """Лимит `clips_per_day` — на ЛОКАЛЬНЫЕ сутки города, как дневной потолок постов."""
    clip_id = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="х")
    # Магнитогорск — UTC+5: 18:00 UTC это 23:00 9 сентября по местному времени,
    # а 20:00 UTC — уже 01:00 десятого. Квота дня обнуляется по местной полуночи.
    db.conn.execute("UPDATE clips SET created_at='2026-09-09T18:00:00Z' WHERE id=?", (clip_id,))
    assert store.made_today(db, CITY, tz="Asia/Yekaterinburg",
                           now_iso="2026-09-09T18:30:00Z") == 1
    assert store.made_today(db, CITY, tz="Asia/Yekaterinburg",
                           now_iso="2026-09-09T20:00:00Z") == 0


def test_made_today_ignores_rejected_and_failed(db):
    """Отклонённый и не собравшийся клип квоту дня не съедают."""
    rejected = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="a")
    store.mark_rejected(db, rejected, moderator_id=1, reason="х")
    failed = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="b")
    store.claim_render(db, failed)
    store.mark_failed(db, failed, "ffmpeg", max_attempts=1)
    assert store.made_today(db, CITY, tz="Europe/Moscow") == 0


def test_purge_old_clips_removes_files_and_keeps_rows(db, tmp_path):
    """Ретеншн 14 дней как у медиа: файл уходит с диска, строка остаётся."""
    path = tmp_path / "old.mp4"
    path.write_bytes(b"x")
    clip_id = store.save_clip(db, draft_id=draft(db), city=CITY, mode="video", score=8, hook="х")
    store.claim_render(db, clip_id)
    store.mark_rendered(db, clip_id, path=str(path), size=1, duration=20, subtitles="none")
    db.conn.execute("UPDATE clips SET rendered_at='2020-01-01T00:00:00Z' WHERE id=?", (clip_id,))
    assert store.purge_old(db, days=14) == 1
    assert not path.exists()
    row = store.get_clip(db, clip_id)
    assert row is not None and row["file_path"] is None
