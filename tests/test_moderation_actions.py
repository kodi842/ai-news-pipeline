"""Решения модератора (спека §7) и правило §28 о повторном предложении.

Кнопки админ-бота — тонкая обвязка над этими функциями: вся логика решений
здесь, поэтому её видно тестами без Telegram.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.moderation import actions
from tgtraff.publisher import store
from tgtraff.publisher.channels import CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.planner import Planner
from tgtraff.publisher.settings import parse_slots

CITY, TZ = "cherepovets", "Europe/Moscow"
NOW = datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)      # 18:30 локального, внутри слота


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def city_cfg(**kw):
    base = dict(city=CITY, deep_link="che",
                tg=TelegramChannel(chat_id="-1001", username="example_channel_1",
                                   title="ЧП Череповец"),
                vk=VkGroup(), slots=parse_slots(["07:00-09:00", "18:00-22:00"]),
                min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)
    return CityChannels(**{**base, **kw})


def planner(city=None):
    channels = type("C", (), {"cities": {CITY: city or city_cfg()}})()
    return Planner(channels, {CITY: TZ})


def draft(db, cluster_id=None, urgency="normal", category="dtp", **kw):
    return drafts_store.save_draft(db, cluster_id=cluster_id, city=CITY,
                                   title="Хук", body="Тело", category=category,
                                   urgency=urgency, **kw)


def cluster(db, posts=1, media=0):
    cid = db.conn.execute(
        "INSERT INTO clusters (city, size) VALUES (?,?)", (CITY, posts)).lastrowid
    for i in range(posts):
        sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=f"src{i}"))
        pid = db.insert_raw_post(source_id=sid, source_type="tg", external_id=f"{cid}-{i}",
                                 published_at="2026-09-04T12:00:00Z", text="текст").id
        db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id) VALUES (?,?)",
                        (cid, pid))
        if i < media:
            mid = db.add_media(pid, 0, "photo")
            db.conn.execute("UPDATE raw_media SET status='done', file_path='/m/a.jpg' WHERE id=?",
                            (mid,))
    return cid


# --- одобрение ---------------------------------------------------------------

def test_approve_puts_the_draft_into_the_queue_at_its_slot(db):
    did = draft(db)
    result = actions.approve(db, did, moderator_id=7, planner=planner(), now=NOW)
    assert result.scheduled_at == NOW            # мы внутри вечернего слота
    assert store.get_queued(db, result.queue_id)["status"] == "queued"
    assert drafts_store.get_draft(db, did)["status"] == "approved"


def test_flash_is_scheduled_immediately_even_outside_slots(db):
    did = draft(db, urgency="flash")
    night = datetime(2026, 9, 4, 22, 0, tzinfo=timezone.utc)      # 01:00 локального
    result = actions.approve(db, did, moderator_id=7, planner=planner(), now=night)
    assert result.scheduled_at == night


def test_second_post_waits_out_the_pause(db):
    actions.approve(db, draft(db), 7, planner(), now=NOW)
    second = actions.approve(db, draft(db), 7, planner(), now=NOW)
    assert timedelta(minutes=20) <= second.scheduled_at - NOW <= timedelta(minutes=40)


def test_daily_limit_moves_the_post_to_the_next_day(db):
    small = planner(city_cfg(daily_limit=1))
    actions.approve(db, draft(db), 7, small, now=NOW)
    second = actions.approve(db, draft(db), 7, small, now=NOW)
    assert second.scheduled_at.date() > NOW.date()


def test_approving_twice_is_refused(db):
    did = draft(db)
    actions.approve(db, did, 7, planner(), now=NOW)
    with pytest.raises(store.AlreadyQueued):
        actions.approve(db, did, 7, planner(), now=NOW)


def test_city_without_a_channel_cannot_be_approved(db):
    empty = Planner(type("C", (), {"cities": {}})(), {CITY: TZ})
    with pytest.raises(ValueError):
        actions.approve(db, draft(db), 7, empty, now=NOW)


# --- отклонение и правка -----------------------------------------------------

def test_reject_records_the_reason_and_the_moderator(db):
    did = draft(db, cluster_id=cluster(db))
    actions.reject(db, did, moderator_id=7, reason=actions.NOT_NEWS)
    row = drafts_store.get_draft(db, did)
    assert row["status"] == "rejected" and row["reject_reason"] == "not_news"
    assert row["moderator_id"] == 7 and row["decided_at"]


def test_unknown_reject_reason_is_refused(db):
    with pytest.raises(ValueError):
        actions.reject(db, draft(db), 7, reason="просто так")


def test_reject_cancels_a_queued_publication(db):
    did = draft(db)
    actions.approve(db, did, 7, planner(), now=NOW)
    actions.reject(db, did, 7, actions.NOT_NEWS)
    assert store.queue_row_for_draft(db, did) is None


def test_edit_replaces_the_hook_and_the_body(db):
    did = draft(db)
    actions.edit(db, did, "Новый хук\nНовое тело поста.", moderator_id=7)
    row = drafts_store.get_draft(db, did)
    assert row["title"] == "Новый хук" and row["body"] == "Новое тело поста."
    assert "правка модератора" in (row["notes"] or "")


def test_edit_of_a_single_line_keeps_the_body(db):
    did = draft(db)
    actions.edit(db, did, "Только новый хук", moderator_id=7)
    assert drafts_store.get_draft(db, did)["body"] == "Тело"


def test_defer_takes_the_draft_out_of_the_stack_but_not_out_of_the_queue(db):
    did = draft(db)
    actions.defer(db, did, moderator_id=7)
    assert drafts_store.get_draft(db, did)["status"] == "deferred"
    assert did in [r["id"] for r in actions.deferred(db)]


# --- очередь модератора ------------------------------------------------------

def test_unseen_returns_only_drafts_without_a_card(db):
    shown, fresh = draft(db), draft(db)
    actions.mark_card(db, shown, chat_id=1, message_id=10)
    assert [r["id"] for r in actions.unseen(db)] == [fresh]


def test_rejected_draft_never_comes_back_to_the_stack(db):
    did = draft(db)
    actions.reject(db, did, 7, actions.NOT_NEWS)
    assert [r["id"] for r in actions.unseen(db)] == []


# --- §28: повторное предложение отклонённого кластера ------------------------

def test_low_facts_rejection_snapshots_the_cluster(db):
    cid = cluster(db, posts=1)
    did = draft(db, cluster_id=cid)
    actions.reject(db, did, 7, actions.LOW_FACTS)
    row = drafts_store.get_draft(db, did)
    assert row["reoffer_size"] == 1 and row["reoffer_media"] == 0


def test_two_new_sources_bring_the_cluster_back(db):
    cid = cluster(db, posts=1)
    actions.reject(db, draft(db, cluster_id=cid), 7, actions.LOW_FACTS)
    _grow(db, cid, posts=2)
    assert actions.reoffer_candidates(db) == [cid]


def test_one_new_source_is_not_enough(db):
    cid = cluster(db, posts=1)
    actions.reject(db, draft(db, cluster_id=cid), 7, actions.LOW_FACTS)
    _grow(db, cid, posts=1)
    assert actions.reoffer_candidates(db) == []


def test_first_media_brings_the_cluster_back(db):
    cid = cluster(db, posts=1)
    actions.reject(db, draft(db, cluster_id=cid), 7, actions.LOW_FACTS)
    _grow(db, cid, posts=1, with_media=True)
    assert actions.reoffer_candidates(db) == [cid]


def test_not_news_means_the_cluster_stays_silent_forever(db):
    cid = cluster(db, posts=1)
    actions.reject(db, draft(db, cluster_id=cid), 7, actions.NOT_NEWS)
    _grow(db, cid, posts=5, with_media=True)
    assert actions.reoffer_candidates(db) == []


def test_reoffer_supersedes_the_old_draft_so_a_new_one_can_be_written(db):
    cid = cluster(db, posts=1)
    old = draft(db, cluster_id=cid)
    actions.reject(db, old, 7, actions.LOW_FACTS)
    _grow(db, cid, posts=2)
    actions.supersede(db, cid)
    assert drafts_store.get_draft(db, old)["status"] == "superseded"
    assert drafts_store.draft_for_cluster(db, cid) is None
    assert actions.reoffer_candidates(db) == []


def _grow(db, cluster_id, posts=1, with_media=False):
    """Кластеру прилетела новая фактура: новые источники и/или первое медиа."""
    start = db.conn.execute("SELECT COUNT(*) c FROM cluster_members WHERE cluster_id=?",
                            (cluster_id,)).fetchone()["c"]
    for i in range(posts):
        n = start + i
        sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=f"grow{n}"))
        pid = db.insert_raw_post(source_id=sid, source_type="tg", external_id=f"g{n}",
                                 published_at="2026-09-04T13:00:00Z", text="ещё текст").id
        db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id) VALUES (?,?)",
                        (cluster_id, pid))
        if with_media and i == 0:
            mid = db.add_media(pid, 0, "photo")
            db.conn.execute("UPDATE raw_media SET status='done', file_path='/m/b.jpg' WHERE id=?",
                            (mid,))
    db.conn.execute("UPDATE clusters SET size=(SELECT COUNT(*) FROM cluster_members "
                    "WHERE cluster_id=?) WHERE id=?", (cluster_id, cluster_id))


def test_publish_now_does_not_relabel_the_post_as_a_flash(db):
    """«Вне очереди, сейчас» — решение модератора о времени, а не о тексте:
    молнию в посте (⚡️ в префиксе) оно рисовать не должно."""
    did = draft(db)
    result = actions.approve(db, did, 7, planner(), now=NOW, immediate=True)
    assert result.scheduled_at == NOW
    assert drafts_store.get_draft(db, did)["urgency"] == "normal"


# --- §55: протухание ---------------------------------------------------------

def aged(db, draft_id, hours):
    """Драфт ждёт решения уже столько часов."""
    db.conn.execute("UPDATE drafts SET created_at=? WHERE id=?",
                    ((NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"), draft_id))


def test_normal_draft_without_a_decision_for_a_day_goes_stale(db):
    old, fresh = draft(db), draft(db)
    aged(db, old, 25)
    aged(db, fresh, 23)
    assert [r["id"] for r in actions.expire(db, now=NOW)] == [old]
    row = drafts_store.get_draft(db, old)
    assert row["status"] == "stale" and row["decided_at"]
    assert drafts_store.get_draft(db, fresh)["status"] == "new"


def test_flash_goes_stale_after_six_hours(db):
    """Молния, не вышедшая за 6 часов, молнией быть перестала (§55)."""
    old, fresh = draft(db, urgency="flash"), draft(db, urgency="flash")
    aged(db, old, 7)
    aged(db, fresh, 5)
    assert [r["id"] for r in actions.expire(db, now=NOW)] == [old]


def test_deferred_draft_goes_stale_too(db):
    did = draft(db)
    actions.defer(db, did, moderator_id=7)
    aged(db, did, 25)
    actions.expire(db, now=NOW)
    assert drafts_store.get_draft(db, did)["status"] == "stale"


def test_decided_drafts_never_go_stale(db):
    approved, rejected = draft(db), draft(db)
    actions.approve(db, approved, 7, planner(), now=NOW)
    actions.reject(db, rejected, 7, actions.NOT_NEWS)
    aged(db, approved, 48)
    aged(db, rejected, 48)
    assert actions.expire(db, now=NOW) == []
    assert drafts_store.get_draft(db, approved)["status"] == "approved"
    assert drafts_store.get_draft(db, rejected)["status"] == "rejected"


def test_stale_draft_leaves_the_stack(db):
    did = draft(db)
    aged(db, did, 25)
    actions.expire(db, now=NOW)
    assert did not in [r["id"] for r in actions.unseen(db)]
    assert did not in [r["id"] for r in actions.pending(db)]


def test_stale_is_not_a_rejection_the_cluster_returns_with_facts(db):
    """§55: протухание — не отклонение. Кластер не блокируется: §28 вернёт
    новость, когда придёт заметная фактура — как после «мало фактуры»."""
    cid = cluster(db, posts=1)
    did = draft(db, cluster_id=cid)
    aged(db, did, 25)
    actions.expire(db, now=NOW)
    row = drafts_store.get_draft(db, did)
    assert row["reoffer_size"] == 1 and row["reoffer_media"] == 0
    assert actions.reoffer_candidates(db) == []
    _grow(db, cid, posts=2)
    assert actions.reoffer_candidates(db) == [cid]
    assert actions.supersede(db, cid) == 1
    assert drafts_store.get_draft(db, did)["status"] == "superseded"


def test_expire_returns_the_card_of_each_stale_draft(db):
    """Боту нужны координаты карточки, чтобы переписать её в «⏳ протух»."""
    did = draft(db)
    actions.mark_card(db, did, chat_id=1, message_id=10)
    aged(db, did, 25)
    (row,) = actions.expire(db, now=NOW)
    assert row["card_chat_id"] == 1 and row["card_message_id"] == 10


def test_expire_is_limited_per_call(db):
    for _ in range(3):
        aged(db, draft(db), 25)
    assert len(actions.expire(db, now=NOW, limit=2)) == 2
    assert len(actions.expire(db, now=NOW)) == 1


# --- §55: пуш только срочного, остальное — пулом по /queue ------------------

def test_only_urgent_drafts_are_pushed(db):
    draft(db)                                                    # обычный — копится
    flash = draft(db, urgency="flash")
    ugc = draft(db, origin="ugc")
    manual = draft(db, safety_verdict="manual_only")
    assert {r["id"] for r in actions.unseen(db, urgent_only=True)} == {flash, ugc, manual}


def test_pool_gives_the_freshest_first(db):
    first, second = draft(db), draft(db)
    aged(db, first, 2)
    aged(db, second, 1)
    assert [r["id"] for r in actions.pool(db)] == [second, first]


def test_pool_holds_deferred_but_not_drafts_already_on_a_card(db):
    on_card, deferred, waiting = draft(db), draft(db), draft(db)
    actions.mark_card(db, on_card, chat_id=1, message_id=10)
    actions.defer(db, deferred, moderator_id=7)
    assert {r["id"] for r in actions.pool(db)} == {deferred, waiting}


def test_queue_summary_counts_by_city_and_category(db):
    draft(db, category="dtp")
    draft(db, category="dtp")
    draft(db, category="fire")
    drafts_store.save_draft(db, cluster_id=None, city="magnitogorsk", title="х", body="т",
                            category="crime")
    summary = actions.queue_summary(db)
    assert summary["total"] == 4
    assert summary["cities"][CITY]["categories"] == {"dtp": 2, "fire": 1}
    assert summary["cities"]["magnitogorsk"]["total"] == 1


def test_queue_summary_separates_what_is_already_on_cards(db):
    pushed, waiting, deferred = draft(db), draft(db), draft(db)
    actions.mark_card(db, pushed, chat_id=1, message_id=10)
    actions.defer(db, deferred, moderator_id=7)
    summary = actions.queue_summary(db)
    assert summary["total"] == 3 and summary["on_cards"] == 1
    assert summary["deferred"] == 1 and summary["pool"] == 2


# --- §59: автопубликация нейтральной новости ---------------------------------
#
# Решение владельца 2026-09-05: драфт, прошедший ВСЕ шесть условий, уходит в
# канал без карточки. Это единственный путь в ленту мимо человека, поэтому
# каждое условие проверяется отдельно и в обе стороны.

AUTO = ("weather", "utility", "city", "other")


def neutral(db, **kw):
    """Драфт, проходящий все шесть условий §59."""
    kw.setdefault("category", "city")
    kw.setdefault("urgency", "normal")
    kw.setdefault("safety_verdict", "safe")
    kw.setdefault("origin", "news")
    return draft(db, **kw)


def test_neutral_safe_news_may_autopublish(db):
    assert actions.may_autopublish(drafts_store.get_draft(db, neutral(db)), AUTO) is True


def test_empty_category_list_switches_the_whole_mode_off(db):
    """Kill-switch §59: пустой autopublish_categories — автопубликации нет вовсе."""
    assert actions.may_autopublish(drafts_store.get_draft(db, neutral(db)), ()) is False


def test_manual_only_never_autopublishes(db):
    row = drafts_store.get_draft(db, neutral(db, safety_verdict="manual_only"))
    assert actions.may_autopublish(row, AUTO) is False


def test_category_outside_the_list_never_autopublishes(db):
    """dtp/fire/crime — руками: жертвы, имена, презумпция невиновности."""
    for category in ("dtp", "fire", "crime"):
        row = drafts_store.get_draft(db, neutral(db, category=category))
        assert actions.may_autopublish(row, AUTO) is False, category


@pytest.mark.parametrize("flag", ["low_facts", "unsupported_facts", "media_unverified"])
def test_any_doubt_flag_blocks_autopublish(db, flag):
    row = drafts_store.get_draft(db, neutral(db, flags=[flag]))
    assert actions.may_autopublish(row, AUTO) is False


def test_ugc_never_autopublishes(db):
    """UGC — всегда руками: за эксклюзив платят и отвечают лично."""
    row = drafts_store.get_draft(db, neutral(db, origin="ugc"))
    assert actions.may_autopublish(row, AUTO) is False


def test_flash_never_autopublishes(db):
    row = drafts_store.get_draft(db, neutral(db, urgency="flash"))
    assert actions.may_autopublish(row, AUTO) is False


def test_autopublish_queues_the_draft_without_a_moderator(db):
    did = neutral(db)
    result = actions.autopublish(db, did, planner(), now=NOW)
    row = drafts_store.get_draft(db, did)
    assert result is not None
    assert row["status"] == "approved"
    assert row["moderator_id"] is None
    assert row["autopublished_at"]
    assert store.get_queued(db, result.queue_id)["status"] == "queued"


def test_autopost_waits_for_the_slot_instead_of_going_out_at_night(db):
    """Шестое условие §59: автопост идёт обычными слотами, а не вне очереди."""
    night = datetime(2026, 9, 4, 22, 0, tzinfo=timezone.utc)      # 01:00 локального
    result = actions.autopublish(db, neutral(db), planner(), now=night)
    assert result.immediate is False
    assert result.scheduled_at > night


def test_second_autopost_waits_out_the_pause(db):
    first = actions.autopublish(db, neutral(db), planner(), now=NOW)
    second = actions.autopublish(db, neutral(db), planner(), now=NOW)
    assert timedelta(minutes=20) <= second.scheduled_at - first.scheduled_at <= timedelta(minutes=40)


def test_autopost_beyond_the_news_lifetime_is_left_to_the_moderator(db):
    """Дневной потолок выбран — слот уезжает на завтра. Сутками позже новость
    мертва (§55), поэтому автопост отменяется и драфт остаётся в пуле."""
    small = planner(city_cfg(daily_limit=1))
    actions.autopublish(db, neutral(db), small, now=NOW)
    did = neutral(db)
    assert actions.autopublish(db, did, small, now=NOW) is None
    row = drafts_store.get_draft(db, did)
    assert row["status"] == "new"
    assert row["autopublished_at"] is None
    assert store.queue_row_for_draft(db, did) is None


def test_candidates_skip_the_draft_already_shown_to_the_moderator(db):
    """Карточка в чате — решение уже за человеком, за его спиной пост не выходит."""
    did = neutral(db)
    actions.mark_card(db, did, chat_id=1, message_id=2)
    assert actions.autopublish_candidates(db, AUTO) == []


def test_candidates_skip_deferred_drafts(db):
    did = neutral(db)
    actions.defer(db, did, moderator_id=7)
    assert actions.autopublish_candidates(db, AUTO) == []


def test_candidates_are_empty_without_categories(db):
    neutral(db)
    assert actions.autopublish_candidates(db, ()) == []


def test_candidates_return_only_what_passes_all_six_conditions(db):
    good = neutral(db)
    neutral(db, category="crime")
    neutral(db, safety_verdict="manual_only")
    neutral(db, flags=["low_facts"])
    neutral(db, urgency="flash")
    neutral(db, origin="ugc")
    assert [r["id"] for r in actions.autopublish_candidates(db, AUTO)] == [good]


def test_autopublished_are_counted_for_the_day(db):
    actions.autopublish(db, neutral(db), planner(), now=NOW)
    actions.approve(db, neutral(db), moderator_id=7, planner=planner(), now=NOW)
    since = store.iso(datetime.now(timezone.utc) - timedelta(hours=24))
    assert actions.autopublished_since(db, since) == 1
