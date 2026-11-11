"""Выбор медиа под клип (VIDEO-SPEC, жёсткое правило 2).

«Медиа-каскад как у постов: UGC/официальное → чистое из кластера → НЕ БЕРЁМ
в клип медиа с чужим вотермарком (в рекомендациях это витрина бренда). Чужие
знаки не вырезаем никогда (ст. 1300 ГК, принцип проекта).»

Отличие от каскада постов: у поста медиа с чужим знаком заменяется своей
обложкой, и пост всё равно выходит. У клипа замены нет — нет пригодных
кадров, нет клипа (PROJECT-SPEC: «нет медиа — клип не делается»).
"""
import pytest

from tgtraff.clips import media as clip_media
from tgtraff.clips.settings import ClipSettings
from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.rewriter.material import collect_material
from tgtraff.rewriter.watermark import FOREIGN, NONE, OWN, UNKNOWN

CITY = "magnitogorsk"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def source(db, key, official=False):
    return db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=key, official=official))


def post(db, source_id, text="Текст новости", media=(), at="2026-09-01T10:00:00+00:00"):
    """`media` — список (kind, path, watermark, owner)."""
    pid = db.insert_raw_post(source_id=source_id, source_type="tg",
                             external_id=str(abs(hash((source_id, text, at))) % 10**7),
                             published_at=at, text=text, has_media=1 if media else 0,
                             media_state="pending" if media else "none").id
    for i, (kind, path, watermark, owner) in enumerate(media):
        mid = db.add_media(pid, i, kind)
        db.conn.execute(
            "UPDATE raw_media SET status='done', file_path=?, watermark=?, watermark_owner=?"
            " WHERE id=?", (path, watermark, owner, mid))
    return pid


def cluster(db, *post_ids):
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at)"
        " VALUES (?, 'open', ?, ?, '2026-09-01T10:00:00+00:00','2026-09-01T10:00:00+00:00')",
        (CITY, post_ids[0], len(post_ids)))
    for pid in post_ids:
        db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id, reason)"
                        " VALUES (?,?,'new')", (cur.lastrowid, pid))
    return cur.lastrowid


def material(db, *post_ids):
    return collect_material(db, cluster(db, *post_ids), city_name="Магнитогорск")


class StubDetector:
    """Вердикты по пути файла; считает вызовы."""

    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}
        self.calls = []

    def verdict(self, item):
        path = getattr(item, "file_path", str(item))
        self.calls.append(path)
        return self.verdicts.get(path, (NONE, None))


def probe(duration=20.0, has_audio=True):
    from tgtraff.clips.probe import VideoInfo

    return lambda path: VideoInfo(duration=duration, has_audio=has_audio,
                                  width=1280, height=720)


def grab(frame_path="/tmp/frame.jpg"):
    return lambda path, **kw: frame_path


SET = ClipSettings(min_video_seconds=8.0, min_photos=2, max_photos=5)


# ---------- видео ----------

def test_long_clean_video_wins(db):
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("video", "/m/a.mp4", NONE, None)])
    choice = clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                              probe=probe(), grab_frame=grab())
    assert choice.mode == "video"
    assert choice.files == ["/m/a.mp4"]
    assert choice.duration == pytest.approx(20.0)


def test_short_video_is_not_a_clip_candidate(db):
    """Спека: кандидат — живое видео ≥8 с. Пятисекундный обрывок не клип."""
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("video", "/m/short.mp4", NONE, None)])
    choice = clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                              probe=probe(duration=5.0), grab_frame=grab())
    assert choice is None


def test_foreign_watermark_on_video_is_never_used(db):
    """Чужой знак на видео — клипа из него не будет. Знак не вырезаем."""
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("video", "/m/foreign.mp4", FOREIGN, "Чужой канал")])
    choice = clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                               probe=probe(), grab_frame=grab())
    assert choice is None


def test_video_watermark_is_checked_by_frame(db):
    """Vision-детектор не смотрит видео — значит, из видео берётся КАДР
    и проверяется он: иначе правило «без чужих знаков» на видео не работает."""
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("video", "/m/unknown.mp4", None, None)])
    detector = StubDetector({"/tmp/frame.jpg": (FOREIGN, "НТВ")})
    choice = clip_media.choose(material(db, pid), SET, detector=detector,
                               probe=probe(), grab_frame=grab())
    assert detector.calls == ["/tmp/frame.jpg"]
    assert choice is None


def test_frame_verdict_is_cached_on_media_row(db):
    """Один платный запрос на файл за всю его жизнь — как у постов."""
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("video", "/m/v.mp4", None, None)])
    detector = StubDetector({"/tmp/frame.jpg": (FOREIGN, "НТВ")})
    clip_media.choose(material(db, pid), SET, detector=detector, probe=probe(),
                      grab_frame=grab(), db=db)
    row = db.conn.execute("SELECT watermark, watermark_owner FROM raw_media").fetchone()
    assert row["watermark"] == FOREIGN
    assert row["watermark_owner"] == "НТВ"


def test_unverifiable_video_is_flagged_not_silently_used(db):
    """Кадр вытащить не удалось / детектора нет: клип собирается, но с флагом —
    в V1 его смотрит человек, и он должен знать, что знак не проверен."""
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("video", "/m/v.mp4", None, None)])
    choice = clip_media.choose(material(db, pid), SET, detector=None, probe=probe(),
                               grab_frame=lambda path, **kw: None)
    assert choice.mode == "video"
    assert "media_unverified" in choice.flags


def test_official_video_goes_first(db):
    """Ступень 1 каскада: ведомство и UGC — высший приоритет."""
    paper = source(db, "example_channel_5")
    agency = source(db, "mchs74", official=True)
    a = post(db, paper, text="Пожар", media=[("video", "/m/paper.mp4", NONE, None)])
    b = post(db, agency, text="Пожар потушен", media=[("video", "/m/mchs.mp4", NONE, None)])
    choice = clip_media.choose(material(db, a, b), SET, detector=StubDetector(),
                               probe=probe(), grab_frame=grab())
    assert choice.files == ["/m/mchs.mp4"]
    assert choice.reason == "official"


def test_agency_mark_on_agency_video_is_not_foreign(db):
    """Знак МЧС на видео МЧС — подпись автора, а не чужой вотермарк."""
    agency = source(db, "mchs74", official=True)
    pid = post(db, agency, media=[("video", "/m/mchs.mp4", FOREIGN, "МЧС России")])
    choice = clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                               probe=probe(), grab_frame=grab())
    assert choice is not None and choice.files == ["/m/mchs.mp4"]


# ---------- фото ----------

def test_two_clean_photos_make_a_slideshow(db):
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("photo", "/m/1.jpg", NONE, None),
                               ("photo", "/m/2.jpg", OWN, None)])
    choice = clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                               probe=probe(), grab_frame=grab())
    assert choice.mode == "photos"
    assert choice.files == ["/m/1.jpg", "/m/2.jpg"]


def test_single_photo_is_not_enough(db):
    """Спека: слайдшоу 2–5 кадров. Один кадр — это не клип, это картинка."""
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("photo", "/m/1.jpg", NONE, None)])
    assert clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                             probe=probe(), grab_frame=grab()) is None


def test_photos_with_foreign_marks_do_not_count(db):
    """Два кадра, но один чужой — клипа нет: чужой знак не берём, а замены
    шаблонной обложкой у клипа не существует."""
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("photo", "/m/1.jpg", NONE, None),
                               ("photo", "/m/2.jpg", FOREIGN, "Чужой")])
    assert clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                             probe=probe(), grab_frame=grab()) is None


def test_slideshow_is_capped_by_max_photos(db):
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("photo", f"/m/{i}.jpg", NONE, None) for i in range(8)])
    choice = clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                               probe=probe(), grab_frame=grab())
    assert len(choice.files) == SET.max_photos


def test_unknown_photo_verdict_is_stricter_than_for_posts(db):
    """У поста `unknown` уходит модератору с пометкой. У клипа фото с
    непроверяемым знаком просто не берётся: фото в кластере обычно много,
    а клип — витрина."""
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("photo", "/m/1.jpg", UNKNOWN, None),
                               ("photo", "/m/2.jpg", UNKNOWN, None),
                               ("photo", "/m/3.jpg", NONE, None)])
    detector = StubDetector({"/m/1.jpg": (UNKNOWN, None), "/m/2.jpg": (UNKNOWN, None)})
    assert clip_media.choose(material(db, pid), SET, detector=detector,
                             probe=probe(), grab_frame=grab()) is None


def test_video_beats_photos(db):
    """Живое видео интереснее слайдшоу — режим (а) спеки приоритетнее (б)."""
    src = source(db, "example_channel_5")
    pid = post(db, src, media=[("photo", "/m/1.jpg", NONE, None),
                               ("photo", "/m/2.jpg", NONE, None),
                               ("video", "/m/v.mp4", NONE, None)])
    choice = clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                               probe=probe(), grab_frame=grab())
    assert choice.mode == "video"


def test_no_media_no_clip(db):
    src = source(db, "example_channel_5")
    pid = post(db, src, text="Только текст")
    assert clip_media.choose(material(db, pid), SET, detector=StubDetector(),
                             probe=probe(), grab_frame=grab()) is None


def test_module_has_no_watermark_removal(db):
    """Ст. 1300 ГК и принцип проекта: вырезать чужие знаки код не умеет."""
    import inspect

    text = inspect.getsource(clip_media).lower()
    for word in ("crop_watermark", "remove_watermark", "delogo", "inpaint", "затер"):
        assert word not in text, f"в модуле появилось {word!r}"
