"""Медиа-каскад (спека §5) — правило, которое нарушать нельзя.

1. UGC из нашего бота / медиа официальных источников — высший приоритет.
2. Медиа без чужих вотермарков из любого источника кластера.
3. Чистого нет → своя шаблонная обложка или пост без медиа.

Плюс запрет из спеки: в пайплайне не должно быть функциональности удаления,
кропа или затирания чужих вотермарков (ст. 1300 ГК РФ).
"""
import pytest

from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.rewriter.material import collect_material
from tgtraff.rewriter.media import MediaSettings, choose_media
from tgtraff.rewriter.watermark import WatermarkDetector

CITY = "magnitogorsk"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def source(db, key, official=False, source_type="tg"):
    return db.upsert_source(SourceSpec(city=CITY, source_type=source_type, key=key,
                                       official=official))


def post(db, source_id, text, media=(), at="2026-09-01T10:00:00+00:00", source_type="tg"):
    """`media` — список (kind, file_path, watermark, owner)."""
    pid = db.insert_raw_post(source_id=source_id, source_type=source_type,
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


class StubDetector:
    """Вердикты заданы заранее; считает, сколько раз его звали."""

    def __init__(self, verdicts=None, default=("none", None)):
        self.verdicts = verdicts or {}
        self.default = default
        self.checked: list[int] = []

    def verdict(self, media):
        self.checked.append(media.media_id)
        return self.verdicts.get(media.file_path, self.default)


def material(db, cid):
    return collect_material(db, cid, city_name="Магнитогорск")


# --- ступень 1: официальные источники --------------------------------------

def test_official_media_wins_even_if_another_source_is_clean(db):
    chp = source(db, "example_channel_5")
    mchs = source(db, "chelmchs", official=True, source_type="vk")
    p1 = post(db, chp, "Горит склад", media=[("photo", "/m/chp.jpg", None, None)])
    p2 = post(db, mchs, "Пожар ликвидирован", source_type="vk",
              media=[("photo", "/m/mchs.jpg", None, None)])

    choice = choose_media(material(db, cluster(db, p1, p2)), StubDetector(), MediaSettings())

    assert choice.path == "/m/mchs.jpg"
    assert choice.reason == "official"


def test_official_media_is_not_sent_to_the_watermark_detector(db):
    """Ведомство ставит свой знак и распространяет фото само — проверять нечего."""
    mchs = source(db, "chelmchs", official=True, source_type="vk")
    p = post(db, mchs, "Пожар ликвидирован", source_type="vk",
             media=[("photo", "/m/mchs.jpg", None, None)])
    detector = StubDetector()

    choose_media(material(db, cluster(db, p)), detector, MediaSettings())

    assert detector.checked == []


# --- ступень 2: чистое медиа -----------------------------------------------

def test_clean_photo_is_chosen(db):
    a = source(db, "example_channel_13")
    p = post(db, a, "ДТП на кольце", media=[("photo", "/m/clean.jpg", None, None)])
    choice = choose_media(material(db, cluster(db, p)), StubDetector(), MediaSettings())
    assert choice.path == "/m/clean.jpg"
    assert choice.reason == "clean"
    assert choice.kind == "photo"


def test_foreign_watermark_is_skipped_in_favour_of_a_clean_photo(db):
    a, b = source(db, "mgn247"), source(db, "example_channel_13")
    p1 = post(db, a, "ДТП на кольце", media=[("photo", "/m/wm.jpg", None, None)])
    p2 = post(db, b, "ДТП на кольце, пострадал водитель",
              media=[("photo", "/m/clean.jpg", None, None)])
    detector = StubDetector({"/m/wm.jpg": ("foreign", "Магнитогорск 24/7")})

    choice = choose_media(material(db, cluster(db, p1, p2)), detector, MediaSettings())

    assert choice.path == "/m/clean.jpg"


def test_verdict_is_cached_in_the_database(db):
    """Одна картинка — один платный vision-запрос за всю её жизнь."""
    a = source(db, "mgn247")
    p = post(db, a, "ДТП", media=[("photo", "/m/wm.jpg", None, None)])
    cid = cluster(db, p)
    detector = StubDetector({"/m/wm.jpg": ("foreign", "Магнитогорск 24/7")})

    choose_media(material(db, cid), detector, MediaSettings(), db=db)
    row = db.conn.execute("SELECT watermark, watermark_owner FROM raw_media").fetchone()
    assert row["watermark"] == "foreign"

    # второй проход — вердикт уже в базе, детектор не зовём
    detector2 = StubDetector()
    choose_media(material(db, cid), detector2, MediaSettings(), db=db)
    assert detector2.checked == []


# --- ступень 3: своя обложка ------------------------------------------------

def test_all_media_watermarked_falls_back_to_template_cover(db):
    a = source(db, "mgn247")
    p = post(db, a, "ДТП на кольце", media=[("photo", "/m/wm.jpg", None, None)])
    detector = StubDetector(default=("foreign", "Магнитогорск 24/7"))

    choice = choose_media(material(db, cluster(db, p)), detector, MediaSettings())

    assert choice.kind == "template"
    assert choice.reason == "watermarked"
    assert choice.path is None            # файл рисуется отдельно, по категории


def test_cluster_without_media_asks_for_a_template_cover(db):
    a = source(db, "example_channel_13")
    p = post(db, a, "Отключение горячей воды в Ленинском районе продлили до пятницы")
    choice = choose_media(material(db, cluster(db, p)), StubDetector(), MediaSettings())
    assert choice.kind == "template"
    assert choice.reason == "no_media"


def test_template_can_be_switched_off_and_then_post_goes_without_media(db):
    a = source(db, "example_channel_13")
    p = post(db, a, "Отключение воды продлили")
    choice = choose_media(material(db, cluster(db, p)), StubDetector(),
                          MediaSettings(use_template_cover=False))
    assert choice.kind == "none"


# --- видео ------------------------------------------------------------------

def test_video_is_offered_but_marked_unverified(db):
    """Vision-детектор смотрит картинки; видео честно помечается непроверенным."""
    a = source(db, "example_channel_5")
    p = post(db, a, "Момент ДТП попал на видеорегистратор",
             media=[("video", "/m/dtp.mp4", None, None)])
    choice = choose_media(material(db, cluster(db, p)), StubDetector(), MediaSettings())
    assert choice.kind == "video"
    assert choice.reason == "unverified"
    assert choice.needs_check is True


def test_clean_photo_beats_unverified_video(db):
    a, b = source(db, "example_channel_5"), source(db, "example_channel_13")
    p1 = post(db, a, "Момент ДТП", media=[("video", "/m/dtp.mp4", None, None)])
    p2 = post(db, b, "ДТП на кольце", media=[("photo", "/m/clean.jpg", None, None)])
    choice = choose_media(material(db, cluster(db, p1, p2)), StubDetector(), MediaSettings())
    assert choice.kind == "photo"


# --- запрет спеки ------------------------------------------------------------

def test_pipeline_has_no_watermark_removal_code():
    """Спека §5: удаления/кропа/затирания чужих вотермарков в пайплайне быть не должно."""
    import pathlib
    forbidden = ("inpaint", "remove_watermark", "erase_watermark", "dewatermark")
    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    hits = [f"{path.name}:{word}" for path in src.rglob("*.py")
            for word in forbidden if word in path.read_text("utf-8")]
    assert hits == []


# --- детектор ---------------------------------------------------------------

class StubVisionClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    def ask_json(self, system, user, **kw):
        self.calls.append({"system": system, "user": user, **kw})

        class _R:
            model = "stub"
            cost_usd = 0.0008

            class usage:
                input_tokens = 900
                output_tokens = 20
                cache_read = 0
                cache_write = 0
        return self.reply, _R()


def test_detector_reads_verdict_and_owner_from_the_model(tmp_path):
    from PIL import Image
    path = tmp_path / "p.jpg"
    Image.new("RGB", (60, 40), (200, 30, 30)).save(path)
    client = StubVisionClient({"watermark": "foreign", "owner": "Верстов.Инфо"})

    verdict, owner = WatermarkDetector(client).verdict(_media(str(path)))

    assert (verdict, owner) == ("foreign", "Верстов.Инфо")
    assert client.calls[0]["user"][0]["type"] == "image"


def test_detector_returns_unknown_when_file_is_missing():
    client = StubVisionClient({"watermark": "none"})
    verdict, _ = WatermarkDetector(client).verdict(_media("/no/such/file.jpg"))
    assert verdict == "unknown"
    assert client.calls == []


def _media(path):
    from tgtraff.rewriter.material import MediaRef
    return MediaRef(media_id=1, raw_post_id=1, ord=0, kind="photo", file_path=path,
                    source_key="mgn247", official=False)
