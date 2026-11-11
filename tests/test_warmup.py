"""Плавный прогрев юзербота: 5-10 подписок в день, случайные паузы, человеческие часы."""
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tgtraff.config import SourceSpec, WarmupSettings
from tgtraff.db import Database
from tgtraff.warmup import WarmupPlanner

MSK = ZoneInfo("Europe/Moscow")
SETTINGS = WarmupSettings(tz="Europe/Moscow", per_day_min=5, per_day_max=10,
                          hour_from=10, hour_to=23, gap_minutes_min=12, gap_minutes_max=95)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def planner(db):
    return WarmupPlanner(db, SETTINGS, rng=random.Random(42))


def _seed(db, n=30):
    ids = []
    for i in range(n):
        prio = ["high", "mid", "low"][i % 3]
        sid = db.upsert_source(SourceSpec(city="cherepovets", source_type="tg", key=f"chan{i}", priority=prio))
        db.ensure_warmup_target(sid, f"chan{i}")
        ids.append(sid)
    return ids


# ---- дневная квота ----

def test_daily_quota_within_configured_range(planner):
    quotas = {planner.daily_quota() for _ in range(200)}
    assert quotas <= set(range(5, 11))
    assert len(quotas) > 1, "квота должна быть случайной, а не фиксированной"


def test_quota_for_a_day_is_decided_once_and_survives_restart(db):
    p1 = WarmupPlanner(db, SETTINGS, rng=random.Random(1))
    quota = p1.day_plan("2026-08-28")["quota"]
    p2 = WarmupPlanner(db, SETTINGS, rng=random.Random(999))     # другой rng, «новый процесс»
    assert p2.day_plan("2026-08-28")["quota"] == quota


def test_remaining_today_decreases_as_targets_are_joined(db, planner):
    _seed(db, 30)
    day = "2026-08-28"
    quota = planner.day_plan(day)["quota"]
    assert planner.remaining_today(day) == quota
    db.bump_warmup_day(day)
    assert planner.remaining_today(day) == quota - 1


def test_quota_is_not_exceeded_even_with_many_pending(db, planner):
    _seed(db, 30)
    day = "2026-08-28"
    quota = planner.day_plan(day)["quota"]
    for _ in range(quota):
        assert planner.next_target(day) is not None
        db.bump_warmup_day(day)
    assert planner.next_target(day) is None       # дневной лимит выбран


def test_next_day_gets_fresh_quota(db, planner):
    _seed(db, 30)
    for _ in range(planner.day_plan("2026-08-28")["quota"]):
        db.bump_warmup_day("2026-08-28")
    assert planner.next_target("2026-08-28") is None
    assert planner.next_target("2026-08-29") is not None


# ---- порядок и состояние ----

def test_high_priority_sources_go_first(db, planner):
    _seed(db, 9)
    target = planner.next_target("2026-08-28")
    assert db.get_source(target["source_id"])["priority"] == "high"


def test_joined_target_is_not_offered_again(db, planner):
    _seed(db, 9)
    first = planner.next_target("2026-08-28")
    db.mark_warmup(first["source_id"], "joined")
    db.bump_warmup_day("2026-08-28")
    assert planner.next_target("2026-08-28")["source_id"] != first["source_id"]


def test_failed_target_retries_but_gives_up_after_three_attempts(db, planner):
    _seed(db, 3)
    sid = planner.next_target("2026-08-28")["source_id"]
    for _ in range(3):
        db.mark_warmup(sid, "failed", "USERNAME_INVALID")
    assert all(t["source_id"] != sid for t in db.warmup_pending())


def test_only_verified_sources_with_username_are_enqueued(db):
    verified = db.upsert_source(SourceSpec(city="cherepovets", source_type="tg", key="example_channel_7"))
    db.upsert_source(SourceSpec(city="cherepovets", source_type="tg", key=None,
                                query="Череповец 360°", status="resolve"))
    planner = WarmupPlanner(db, SETTINGS, rng=random.Random(0))
    planner.enqueue([
        SourceSpec(city="cherepovets", source_type="tg", key="example_channel_7"),
        SourceSpec(city="cherepovets", source_type="tg", key=None, query="Череповец 360°", status="resolve"),
    ])
    assert [t["source_id"] for t in db.warmup_pending()] == [verified]


# ---- человеческие часы и паузы ----

@pytest.mark.parametrize("hour,expected", [(3, False), (9, False), (10, True), (15, True), (22, True), (23, False)])
def test_human_hours_window(planner, hour, expected):
    now = datetime(2026, 8, 28, hour, 0, tzinfo=MSK)
    assert planner.in_human_hours(now) is expected


def test_sleep_until_window_when_too_early(planner):
    now = datetime(2026, 8, 28, 4, 0, tzinfo=MSK)
    assert planner.seconds_until_window(now) == pytest.approx(6 * 3600, abs=1)


def test_sleep_until_next_morning_when_too_late(planner):
    now = datetime(2026, 8, 28, 23, 30, tzinfo=MSK)
    assert planner.seconds_until_window(now) == pytest.approx(10.5 * 3600, abs=1)


def test_no_sleep_inside_window(planner):
    assert planner.seconds_until_window(datetime(2026, 8, 28, 12, 0, tzinfo=MSK)) == 0


def test_gaps_are_random_within_configured_minutes(planner):
    gaps = [planner.next_gap_seconds() for _ in range(200)]
    assert all(12 * 60 <= g <= 95 * 60 for g in gaps)
    assert len(set(gaps)) > 50, "паузы должны быть случайными"


# ---- сон по календарю, а не по монотонным часам ----
# Прогрев планируется по стенным часам («завтра в 10:00»), а asyncio.sleep отмеряет
# монотонное время, которое стоит, пока машина спит. Один длинный sleep из-за этого
# промахивается на сутки: 28.08 сон «10.8 ч» закончился 02.09 (потеряно 4 дня подписок).

from datetime import timedelta  # noqa: E402

from tgtraff.warmup import sleep_until  # noqa: E402


class FakeClock:
    """Стенные часы + сон, который может «перепрыгнуть» вперёд (машина уснула)."""

    def __init__(self, start, jump_after_first=None):
        self.now = start
        self.slept = []
        self.jump = jump_after_first

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.jump and len(self.slept) == 1:
            self.now += self.jump          # проснулись сильно позже, чем просили


async def test_sleep_until_waits_the_whole_interval_when_clock_runs_normally():
    start = datetime(2026, 8, 28, 23, 0, tzinfo=MSK)
    clock = FakeClock(start)
    await sleep_until(start + timedelta(hours=1), clock=clock.time, sleep=clock.sleep, step=60)
    assert sum(clock.slept) == 3600
    assert clock.now == start + timedelta(hours=1)


async def test_sleep_until_never_sleeps_longer_than_one_step():
    start = datetime(2026, 8, 28, 23, 0, tzinfo=MSK)
    clock = FakeClock(start)
    await sleep_until(start + timedelta(hours=11), clock=clock.time, sleep=clock.sleep, step=60)
    assert max(clock.slept) <= 60


async def test_sleep_until_returns_at_once_if_deadline_passed():
    start = datetime(2026, 8, 28, 23, 0, tzinfo=MSK)
    clock = FakeClock(start)
    await sleep_until(start - timedelta(minutes=5), clock=clock.time, sleep=clock.sleep)
    assert clock.slept == []


async def test_machine_suspend_does_not_overshoot_the_deadline():
    """Ровно тот случай, что съел 4 дня прогрева: заснули на 10.8 ч, очнулись через 4 суток."""
    start = datetime(2026, 8, 28, 23, 13, tzinfo=MSK)
    deadline = datetime(2026, 8, 29, 10, 0, tzinfo=MSK)
    clock = FakeClock(start, jump_after_first=timedelta(days=4))
    await sleep_until(deadline, clock=clock.time, sleep=clock.sleep, step=60)
    assert len(clock.slept) == 1, "после скачка часов надо выходить сразу, а не досыпать интервал"


async def test_sleep_until_does_not_overshoot_the_last_step():
    start = datetime(2026, 8, 28, 23, 0, tzinfo=MSK)
    clock = FakeClock(start)
    await sleep_until(start + timedelta(seconds=90), clock=clock.time, sleep=clock.sleep, step=60)
    assert clock.slept == [60, 30]


def test_next_window_open_is_tomorrow_morning(planner):
    """Дневная квота выбрана — ждём начала завтрашнего окна, а не 24 ч от текущего момента."""
    assert planner.next_window_open(datetime(2026, 8, 28, 19, 40, tzinfo=MSK)) == \
           datetime(2026, 8, 29, 10, 0, tzinfo=MSK)
    assert planner.next_window_open(datetime(2026, 8, 28, 23, 30, tzinfo=MSK)) == \
           datetime(2026, 8, 29, 10, 0, tzinfo=MSK)


def test_window_opens_at_returns_now_when_window_is_open(planner):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=MSK)
    assert planner.window_opens_at(now) == now
