"""Проход завода клипов (VIDEO-SPEC, фаза V1): кандидаты → скор → сценарий →
очередь рендера.

Здесь проверяется поведение конвейера, которое дороже всего ошибается:
  * в клипы идут только опубликованные safe-драфты и только свежие;
  * дневной лимит на ГОРОД соблюдается (`clips_per_day`);
  * один и тот же драфт не оценивается платной моделью дважды — иначе завод
    жжёт деньги каждые пять минут (та же ловушка, что §70 в QUESTIONS);
  * рендер идёт очередью с параллельностью 1.
"""
import pytest

from tgtraff.clips import store
from tgtraff.clips.factory import ClipFactory
from tgtraff.clips.media import ClipMedia
from tgtraff.clips.render import RenderResult
from tgtraff.clips.score import ClipScore
from tgtraff.clips.script import ClipScript
from tgtraff.clips.settings import ClipSettings
from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.llm.client import LlmError, LlmUnavailable

CITY = "magnitogorsk"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def source(db, key="example_channel_5", official=False):
    return db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=key, official=official))


def cluster_with_media(db, city=CITY, kind="video", n=1):
    """Медиа кладём НА ДИСК: конвейер перед рендером проверяет, что файл на
    месте (ретеншн мог его убрать), и на выдуманных путях это не проверить."""
    media_dir = db.path.parent / "media"
    media_dir.mkdir(exist_ok=True)
    src = db.upsert_source(SourceSpec(city=city, source_type="tg", key=f"src_{city}"))
    pid = db.insert_raw_post(source_id=src, source_type="tg",
                             external_id=str(abs(hash((city, kind, n))) % 10**7),
                             published_at="2026-09-09T10:00:00+00:00",
                             text="Полный текст новости с фактурой", has_media=1).id
    for i in range(n):
        mid = db.add_media(pid, i, kind)
        path = media_dir / f"{city}_{mid}.{'mp4' if kind == 'video' else 'jpg'}"
        path.write_bytes(b"media")
        db.conn.execute("UPDATE raw_media SET status='done', file_path=?, watermark='none'"
                        " WHERE id=?", (str(path), mid))
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at)"
        " VALUES (?, 'open', ?, 1, '2026-09-09T10:00:00+00:00','2026-09-09T10:00:00+00:00')",
        (city, pid))
    db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id, reason)"
                    " VALUES (?,?,'new')", (cur.lastrowid, pid))
    return cur.lastrowid


def draft(db, city=CITY, status="published", verdict="safe", category="dtp",
          title="Автобус смял столб", body="Утром на Ленина столкнулись автобус и фура.",
          flags=None, kind="video", n=1, created="2026-09-09T10:05:00Z"):
    cluster_id = cluster_with_media(db, city, kind=kind, n=n)
    return db.conn.execute(
        """INSERT INTO drafts (cluster_id, city, title, body, category, safety_verdict,
                               flags, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cluster_id, city, title, body, category, verdict, flags, status, created,
         created)).lastrowid


VIDEO = ClipMedia(mode="video", kind="video", files=["/m/a.mp4"], media_ids=[1],
                  source_key="example_channel_5", reason="clean", duration=25.0, has_audio=True)


class StubScorer:
    def __init__(self, score=8, error=None):
        self.value, self.error, self.calls = score, error, []

    def score(self, draft, media, city_name=None):
        self.calls.append(draft["id"])
        if self.error:
            raise self.error
        return ClipScore(score=self.value, reason="живое видео", model="stub", cost_usd=0.001)


class StubWriter:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def write(self, draft, media, city_name=None, username=None, hint=None):
        self.calls.append(draft["id"])
        if self.error:
            raise self.error
        return ClipScript(hook="Автобус смял столб", lines=["Утром на Ленина", "Движение перекрыто"],
                          cta="Подробности — в канале", username=username, model="stub",
                          cost_usd=0.002)


class StubBuilder:
    """Вместо ffmpeg: делает вид, что файл собран."""

    def __init__(self, ok=True, error="ffmpeg упал"):
        self.ok, self.error, self.calls = ok, error, []

    def build(self, clip, draft, files, city_name=None, username=None):
        self.calls.append(clip["id"])
        if not self.ok:
            return RenderResult(False, error=self.error)

        class _R:
            path = "/clips/a.mp4"
            size = 12 * 1024 * 1024
            duration = 26.0
            subtitles = "whisper"
            ok = True
            error = ""
        return _R()


def factory(db, settings=None, scorer=None, writer=None, builder=None, media=None):
    return ClipFactory(
        db, settings or ClipSettings(min_score=6, clips_per_day=3),
        scorer=scorer or StubScorer(), writer=writer or StubWriter(),
        builder=builder or StubBuilder(),
        pick_media=media if media is not None else (lambda material, **kw: VIDEO),
        city_names={CITY: "Магнитогорск", "cherepovets": "Череповец"},
        usernames={CITY: "example_channel_4"},
        city_tz={CITY: "Asia/Yekaterinburg", "cherepovets": "Europe/Moscow"})


NOW = "2026-09-09T11:00:00Z"


# ---------- кандидаты ----------

def test_published_safe_draft_becomes_a_clip(db):
    draft_id = draft(db)
    made = factory(db).plan_clips(now=NOW)
    assert [o.draft_id for o in made if o.status == "made"] == [draft_id]
    clip = store.clip_for_draft(db, draft_id)
    assert clip["hook"] == "Автобус смял столб"
    assert clip["status"] == "planned"
    assert clip["score"] == 8


def test_unpublished_draft_is_not_a_candidate(db):
    """Спека: клип делается по новости, которая ВЫШЛА в ТГ."""
    draft(db, status="new")
    assert factory(db).plan_clips(now=NOW) == []


def test_manual_only_draft_is_not_a_candidate(db):
    draft(db, verdict="manual_only")
    assert factory(db).plan_clips(now=NOW) == []


def test_old_news_is_not_a_candidate(db):
    """Новость города старше суток мертва — как и в §55."""
    draft(db, created="2026-09-01T10:00:00Z")
    assert factory(db).plan_clips(now=NOW) == []


def test_gore_draft_is_skipped_with_a_reason(db):
    draft_id = draft(db, body="Тело погибшего накрыли простынёй.")
    outcomes = factory(db).plan_clips(now=NOW)
    assert [o.status for o in outcomes] == ["skipped"]
    assert "жесть" in outcomes[0].reason
    # Решение записано: второй раз тот же драфт в модель не поедет.
    assert store.get_clip(db, 1)["status"] == "skipped"


def test_skipped_draft_is_not_scored_twice(db):
    """Главная защита денег: повторный проход не платит за то же самое."""
    draft(db, body="Труп у гаражей.")
    scorer = StubScorer()
    made = factory(db, scorer=scorer)
    made.plan_clips(now=NOW)
    made.plan_clips(now=NOW)
    assert scorer.calls == []


def test_made_clip_is_not_planned_again(db):
    draft(db)
    scorer = StubScorer()
    f = factory(db, scorer=scorer)
    f.plan_clips(now=NOW)
    f.plan_clips(now=NOW)
    assert len(scorer.calls) == 1


def test_low_score_is_recorded_as_skip(db):
    draft(db)
    outcomes = factory(db, scorer=StubScorer(score=4)).plan_clips(now=NOW)
    assert outcomes[0].status == "skipped"
    assert "4" in outcomes[0].reason
    assert store.get_clip(db, 1)["score"] == 4


def test_no_suitable_media_is_recorded_as_skip(db):
    draft(db)
    outcomes = factory(db, media=lambda material, **kw: None).plan_clips(now=NOW)
    assert outcomes[0].status == "skipped"
    assert "медиа" in outcomes[0].reason


# ---------- лимиты ----------

def test_daily_limit_is_per_city(db):
    for _ in range(4):
        draft(db)
    for _ in range(2):
        draft(db, city="cherepovets")
    f = ClipFactory(
        db, ClipSettings(clips_per_day=2, min_score=6), scorer=StubScorer(),
        writer=StubWriter(), builder=StubBuilder(),
        pick_media=lambda material, **kw: VIDEO,
        city_names={CITY: "Магнитогорск", "cherepovets": "Череповец"},
        usernames={}, city_tz={CITY: "Asia/Yekaterinburg", "cherepovets": "Europe/Moscow"})
    made = [o for o in f.plan_clips(now=NOW) if o.status == "made"]
    assert len(made) == 4                              # по два на каждый город
    assert store.made_today(db, CITY, "Asia/Yekaterinburg", NOW) == 2
    assert store.made_today(db, "cherepovets", "Europe/Moscow", NOW) == 2


def test_quota_skip_is_not_recorded_as_a_decision(db):
    """Лимит дня — не решение по драфту: завтра он мог бы стать клипом,
    и запись «пропущен» была бы неправдой."""
    for _ in range(3):
        draft(db)
    f = factory(db, settings=ClipSettings(clips_per_day=1, min_score=6))
    f.plan_clips(now=NOW)
    assert store.counts(db) == {"planned": 1}


def test_pass_limit_bounds_the_money(db):
    for _ in range(5):
        draft(db)
    scorer = StubScorer()
    f = factory(db, settings=ClipSettings(score_per_pass=2, clips_per_day=9, min_score=6),
                scorer=scorer)
    f.plan_clips(now=NOW)
    assert len(scorer.calls) == 2


# ---------- ошибки модели ----------

def test_model_outage_stops_the_pass(db):
    """Гео-блок и упавший ключ: следующие кандидаты не перебираются
    (та же логика, что в фазе 4)."""
    draft(db)
    draft(db)
    scorer = StubScorer(error=LlmUnavailable("гео-блок"))
    outcomes = factory(db, scorer=scorer).plan_clips(now=NOW)
    assert len(scorer.calls) == 1
    assert outcomes and outcomes[0].status == "failed"


def test_single_draft_model_error_is_recorded_and_pass_continues(db):
    a, b = draft(db), draft(db)
    outcomes = factory(db, writer=StubWriter(error=LlmError("мусор в ответе"))).plan_clips(now=NOW)
    assert [o.status for o in outcomes] == ["skipped", "skipped"]
    assert {o.draft_id for o in outcomes} == {a, b}


# ---------- очередь рендера ----------

def test_render_takes_one_clip_at_a_time(db):
    draft(db)
    draft(db)
    f = factory(db, settings=ClipSettings(clips_per_day=9, min_score=6))
    f.plan_clips(now=NOW)
    assert store.counts(db)["planned"] == 2
    f.render_next()
    counts = store.counts(db)
    assert counts["ready"] == 1 and counts["planned"] == 1


def test_rendered_clip_carries_the_file(db):
    draft(db)
    f = factory(db)
    f.plan_clips(now=NOW)
    outcome = f.render_next()
    assert outcome.status == "made"
    clip = store.get_clip(db, outcome.clip_id)
    assert clip["status"] == "ready"
    assert clip["file_path"] == "/clips/a.mp4"
    assert clip["duration_sec"] == pytest.approx(26.0)
    assert clip["subtitles"] == "whisper"


def test_failed_render_returns_to_the_queue(db):
    draft(db)
    f = factory(db, builder=StubBuilder(ok=False))
    f.plan_clips(now=NOW)
    outcome = f.render_next()
    assert outcome.status == "failed"
    assert store.get_clip(db, outcome.clip_id)["status"] == "planned"


def test_render_gives_up_after_max_attempts(db):
    draft(db)
    f = factory(db, settings=ClipSettings(max_attempts=2, min_score=6),
                builder=StubBuilder(ok=False))
    f.plan_clips(now=NOW)
    f.render_next()
    f.render_next()
    assert store.counts(db)["failed"] == 1


def test_render_next_on_empty_queue_is_none(db):
    assert factory(db).render_next() is None


def test_disabled_factory_does_nothing(db):
    draft(db)
    f = factory(db, settings=ClipSettings(enabled=False))
    assert f.plan_clips(now=NOW) == []
    assert f.render_next() is None


# ---------- перегенерация хука ----------

class HookWriter:
    """Второй сценарий с другим хуком — как после кнопки «Перегенерить хук»."""

    def __init__(self, error=None):
        self.error, self.hints = error, []

    def write(self, draft, media, city_name=None, username=None, hint=None):
        self.hints.append(hint)
        if self.error:
            raise self.error
        from tgtraff.clips.script import ClipScript

        return ClipScript(hook="Фура смяла автобус", lines=["Утром на Ленина"],
                          cta="Подробности — в канале", username=username, model="stub",
                          cost_usd=0.001)


def rehooked(db, writer):
    draft(db)
    f = factory(db)
    f.plan_clips(now=NOW)
    clip_id = f.render_next().clip_id
    store.mark_card(db, clip_id, chat_id=1, message_id=10)
    store.request_rehook(db, clip_id, moderator_id=7)
    return clip_id, factory(db, writer=writer)


def test_rehook_writes_a_new_script_and_returns_to_render(db):
    writer = HookWriter()
    clip_id, f = rehooked(db, writer)
    assert len(f.rescript_pending()) == 1
    row = store.get_clip(db, clip_id)
    assert row["hook"] == "Фура смяла автобус"
    assert row["status"] == "planned"
    assert row["card_sent_at"] is None


def test_rehook_asks_the_model_for_a_different_hook(db):
    """Без предыдущего хука в промпте модель нередко выдаёт тот же самый."""
    writer = HookWriter()
    _, f = rehooked(db, writer)
    f.rescript_pending()
    assert writer.hints and "Автобус смял столб" in writer.hints[0]


def test_rehook_keeps_the_money_counted(db):
    writer = HookWriter()
    clip_id, f = rehooked(db, writer)
    f.rescript_pending()
    assert store.get_clip(db, clip_id)["cost_usd"] == pytest.approx(0.004)


def test_failed_rehook_returns_the_old_clip_to_the_moderator(db):
    """Модель не ответила: клип не теряется — возвращается карточкой как был."""
    clip_id, f = rehooked(db, HookWriter(error=LlmError("нет связи")))
    f.rescript_pending()
    row = store.get_clip(db, clip_id)
    assert row["status"] == "ready"
    assert row["hook"] == "Автобус смял столб"
    assert row["card_sent_at"] is None


# ---------- калибровочный прогон ----------

def test_calibration_flag_allows_unpublished_drafts(db):
    """Приёмка и калибровка идут по живой ленте, в которой почти всё лежит
    в очереди модерации (тестовый режим каналов). Рабочий проход при этом
    обязан оставаться по спеке — «драфты, опубликованные в ТГ»."""
    draft(db, status="deferred")
    f = factory(db)
    assert f.plan_clips(now=NOW) == []
    made = f.plan_clips(now=NOW, any_status=True)
    assert [o.status for o in made] == ["made"]


def test_calibration_flag_does_not_relax_safety(db):
    """Ослабляется только «опубликован» — вердикт safety и клип-фильтр нет."""
    draft(db, status="deferred", verdict="manual_only")
    assert factory(db).plan_clips(now=NOW, any_status=True) == []


def test_service_autopost_is_not_a_clip_candidate(db):
    """Сервисные автопосты (погода, вечерний топ-5) кластера не имеют: снимать
    в клип нечего, а в отчёте они выглядели «кластера уже нет» — шум."""
    db.conn.execute(
        """INSERT INTO drafts (cluster_id, city, title, body, category, safety_verdict,
                               status, origin, created_at)
           VALUES (NULL,?,?,?,'weather','safe','published','service',?)""",
        (CITY, "Погода в Магнитогорске", "Днём +18.", "2026-09-09T10:00:00Z"))
    f = factory(db)
    assert f.candidates(now=NOW) == []
    assert f.plan_clips(now=NOW) == []


def test_draft_warning_flag_reaches_the_clip_card(db):
    """`unsupported_facts` клип не запрещает, но модератор должен увидеть его
    в карточке: иначе предупреждение автосверки просто теряется."""
    draft(db, flags='["unsupported_facts"]')
    f = factory(db)
    outcome = f.plan_clips(now=NOW)[0]
    assert outcome.status == "made"
    assert "unsupported_facts" in store.decode_flags(store.get_clip(db, outcome.clip_id))


def test_clip_without_channel_username_is_flagged(db):
    """Аутро без @ника — выброшенный клип: зрителю некуда идти. В тестовом
    режиме каналов username'а нет вовсе (channels.yaml), и молчать об этом
    нельзя — пометка уходит в карточку."""
    draft(db)
    f = ClipFactory(db, ClipSettings(min_score=6, clips_per_day=3), scorer=StubScorer(),
                    writer=StubWriter(), builder=StubBuilder(),
                    pick_media=lambda material, **kw: VIDEO,
                    city_names={CITY: "Магнитогорск"}, usernames={},   # режим test
                    city_tz={CITY: "Asia/Yekaterinburg"})
    outcome = f.plan_clips(now=NOW)[0]
    assert "no_username" in store.decode_flags(store.get_clip(db, outcome.clip_id))


def test_clip_with_username_has_no_such_flag(db):
    draft(db)
    outcome = factory(db).plan_clips(now=NOW)[0]
    assert "no_username" not in store.decode_flags(store.get_clip(db, outcome.clip_id))
