"""Фото от модератора ответом на карточку (решение ревью §61.1).

Механика — по образцу правки текста: ответ на карточку ФОТОГРАФИЕЙ заменяет
медиа драфта. Подпись к фото, если есть, работает как текстовая правка.

Два правила спеки §5, которые здесь и стерегутся:
  * чужой знак не трогаем никогда — ни кропа, ни затирания; вместо этого
    бот предупреждает и просит подтвердить кнопкой;
  * НАШ знак ставится только на чистый кадр (это заявка на эксклюзив) —
    на чужое фото он не ставится даже после подтверждения.
"""
from pathlib import Path

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.moderation.photo import ModeratorPhoto
from tgtraff.rewriter.watermark import FOREIGN, NONE, UNKNOWN

CITY, MOD = "cherepovets", 777


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def incoming(tmp_path):
    """Файл, который «прислал» модератор."""
    path = tmp_path / "inbox" / "shot.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jpeg-bytes")
    return path


class FakeDetector:
    """Двойник vision-детектора: вердикт задаётся тестом, вызовы считаются."""

    def __init__(self, verdict=NONE, owner=None):
        self.verdict_value, self.owner, self.calls = verdict, owner, []

    def verdict(self, media):
        self.calls.append(media.file_path)
        return self.verdict_value, self.owner


def stamper(marked: list):
    """Двойник бренд-плашки: пишет свой файл и запоминает, что его звали."""
    def stamp(src: Path, dst: Path, city: str) -> None:
        marked.append((Path(src).name, city))
        Path(dst).write_bytes(Path(src).read_bytes() + b"-stamped")
    return stamp


def photos(db, tmp_path, verdict=NONE, owner=None, marked=None):
    return ModeratorPhoto(db, media_dir=tmp_path / "media",
                          detector=FakeDetector(verdict, owner),
                          stamper=stamper(marked if marked is not None else []))


def draft(db, **kw):
    base = dict(cluster_id=None, city=CITY, title="Такси вылетело на тротуар",
                body="Пострадали два человека.", category="dtp", urgency="normal",
                safety_verdict="manual_only", media_kind="template",
                media_reason="watermarked", media_path="/m/cover.png")
    return drafts_store.save_draft(db, **{**base, **kw})


# --- проверка на чужой знак --------------------------------------------------

def test_clean_photo_needs_no_confirmation(db, tmp_path, incoming):
    check = photos(db, tmp_path).check(incoming)

    assert check.verdict == NONE
    assert check.needs_confirmation is False


def test_foreign_watermark_asks_the_moderator_to_confirm(db, tmp_path, incoming):
    check = photos(db, tmp_path, verdict=FOREIGN, owner="Верстов.Инфо").check(incoming)

    assert check.needs_confirmation is True
    assert check.owner == "Верстов.Инфо"


def test_unchecked_photo_also_asks_to_confirm(db, tmp_path, incoming):
    """Детектор не ответил (модель недоступна). «Не проверено» — это не
    «чисто»: на кону чужие права (спека §5), поэтому решает человек."""
    assert photos(db, tmp_path, verdict=UNKNOWN).check(incoming).needs_confirmation is True


# --- подстановка медиа в драфт -----------------------------------------------

def test_clean_photo_gets_our_watermark_and_replaces_the_draft_media(db, tmp_path, incoming):
    marked = []
    did = draft(db)

    stored = photos(db, tmp_path, marked=marked).attach(did, incoming, MOD)

    row = drafts_store.get_draft(db, did)
    assert Path(row["media_path"]) == stored and stored.exists()
    assert row["media_kind"] == "photo"
    assert row["media_reason"] == "moderator"
    assert marked == [("shot.jpg", CITY)]                  # наш знак поставлен
    assert stored.read_bytes().endswith(b"-stamped")


def test_confirmed_foreign_photo_is_stored_without_our_mark(db, tmp_path, incoming):
    """Чужой знак мы не трогаем и своим не накрываем: наша плашка — заявка
    на эксклюзив, а на чужом кадре это была бы неправда (спека §5)."""
    marked = []
    did = draft(db)

    stored = photos(db, tmp_path, verdict=FOREIGN, marked=marked).attach(
        did, incoming, MOD, stamp=False)

    assert marked == []
    assert stored.read_bytes() == b"jpeg-bytes"
    assert drafts_store.get_draft(db, did)["media_reason"] == "moderator"


def test_the_file_the_moderator_sent_is_left_alone(db, tmp_path, incoming):
    photos(db, tmp_path).attach(draft(db), incoming, MOD)

    assert incoming.read_bytes() == b"jpeg-bytes"


def test_stored_photo_lands_in_the_city_folder_of_the_media_dir(db, tmp_path, incoming):
    stored = photos(db, tmp_path).attach(draft(db), incoming, MOD)

    assert stored.parent == tmp_path / "media" / "moderator" / CITY


def test_a_failing_stamper_does_not_lose_the_photo(db, tmp_path, incoming):
    """Плашка — украшение, кадр — содержание: если знак не поставился,
    фото всё равно должно доехать до драфта."""
    def broken(src, dst, city):
        raise RuntimeError("нет шрифта")

    p = ModeratorPhoto(db, media_dir=tmp_path / "media",
                       detector=FakeDetector(), stamper=broken)
    did = draft(db)

    stored = p.attach(did, incoming, MOD)

    assert stored.exists() and stored.read_bytes() == b"jpeg-bytes"
    assert drafts_store.get_draft(db, did)["media_path"] == str(stored)


def test_replacement_is_written_into_the_draft_notes(db, tmp_path, incoming):
    """Кто подменил картинку — вопрос разбора инцидента, ответ должен быть
    в базе, как и у текстовой правки."""
    did = draft(db)

    photos(db, tmp_path).attach(did, incoming, MOD)

    assert "777" in (drafts_store.get_draft(db, did)["notes"] or "")


def test_two_photos_in_a_row_do_not_collide(db, tmp_path, incoming):
    """Модератор прислал второй кадр вместо первого — второй файл не должен
    затирать первый: перевыпуск вышедшего поста ссылается на конкретный файл."""
    p = photos(db, tmp_path)
    did = draft(db)

    first = p.attach(did, incoming, MOD)
    second = p.attach(did, incoming, MOD)

    assert first != second and first.exists() and second.exists()
