"""UGC-приём (спека, приёмка фазы 5): присланное в бот фото/видео с текстом
попадает в ТУ ЖЕ очередь модерации с пометкой `ugc`.

Город приходит deep-link'ом `?start=mgn|nvk|che` (DECISIONS.md) — один бот
на всю сеть. Автор запоминается: выплаты за эксклюзив ручные, но знать,
кому платить, конвейер обязан.
"""
import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.moderation.ugc import UgcIntake, UnknownCity

DEEP_LINKS = {"mgn": "magnitogorsk", "nvk": "novokuznetsk", "che": "cherepovets"}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def intake(db, tmp_path):
    return UgcIntake(db, DEEP_LINKS, media_dir=tmp_path / "media")


@pytest.fixture
def photo(tmp_path):
    from PIL import Image
    path = tmp_path / "ugc.jpg"
    Image.new("RGB", (1280, 720), (40, 60, 90)).save(path)
    return path


def test_deep_link_registers_the_sender_city(db, intake):
    assert intake.register(user_id=555, username="reader", payload="che") == "cherepovets"
    assert intake.city_of(555) == "cherepovets"


def test_unknown_payload_leaves_the_sender_without_a_city(db, intake):
    assert intake.register(user_id=555, username="reader", payload="xxx") is None
    assert intake.city_of(555) is None


def test_submission_lands_in_the_moderation_queue_with_the_ugc_flag(db, intake):
    intake.register(555, "reader", "che")
    draft_id = intake.submit(user_id=555, username="reader",
                             text="Горит трава у Северстали, дым на весь район")
    row = drafts_store.get_draft(db, draft_id)
    assert row["status"] == "new" and row["origin"] == "ugc"
    assert "ugc" in drafts_store.decode_list(row["flags"])
    assert row["city"] == "cherepovets"


def test_submission_without_a_city_is_refused(db, intake):
    with pytest.raises(UnknownCity):
        intake.submit(user_id=999, username="reader", text="Что-то случилось")


def test_first_line_becomes_the_hook(db, intake):
    intake.register(555, "reader", "che")
    row = drafts_store.get_draft(db, intake.submit(
        555, "reader", "Пожар на Наседкина\nГорит расселённый дом, приехали три расчёта."))
    assert row["title"] == "Пожар на Наседкина"
    assert row["body"].startswith("Горит расселённый дом")


def test_author_is_remembered_for_the_payout(db, intake):
    intake.register(555, "reader", "che")
    row = drafts_store.get_draft(db, intake.submit(555, "reader", "Новость"))
    assert row["ugc_user_id"] == 555 and row["ugc_username"] == "reader"


def test_sender_submissions_are_counted(db, intake):
    intake.register(555, "reader", "che")
    intake.submit(555, "reader", "Раз")
    intake.submit(555, "reader", "Два")
    assert intake.sender(555)["submissions"] == 2


def test_stop_word_submission_goes_to_manual_only(db, intake):
    """Присланное читателем проходит бесплатный словарь safety — без LLM."""
    intake.register(555, "reader", "che")
    row = drafts_store.get_draft(db, intake.submit(
        555, "reader", "Завтра сбор на несогласованный митинг у мэрии в 15:00"))
    assert row["safety_verdict"] in ("manual_only", "reject")


def test_photo_is_saved_and_stamped_with_our_watermark(db, intake, photo):
    intake.register(555, "reader", "che")
    row = drafts_store.get_draft(db, intake.submit(
        555, "reader", "Пожар", media_path=photo, media_kind="photo"))
    saved = row["media_path"]
    assert saved and saved != str(photo)
    from PIL import Image
    assert Image.open(saved).size == (1280, 720)
    assert row["media_kind"] == "photo"


def test_video_goes_through_without_a_stamp(db, intake, tmp_path):
    """Наш знак на видео ставить нечем (ffmpeg в пилот не тянем) — видео идёт
    как есть, модератор видит пометку."""
    intake.register(555, "reader", "che")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    row = drafts_store.get_draft(db, intake.submit(
        555, "reader", "Видео с места", media_path=video, media_kind="video"))
    assert row["media_kind"] == "video"
    assert "не размечено" in (row["notes"] or "") or row["media_reason"] == "ugc"


def test_empty_submission_is_refused(db, intake):
    intake.register(555, "reader", "che")
    with pytest.raises(ValueError):
        intake.submit(555, "reader", text="   ")
