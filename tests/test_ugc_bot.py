"""UGC-бот «Прислать новость» (спека, фаза 5).

Один бот на сеть: город приходит deep-link'ом из кнопки под постом. Пришедшему
без ссылки бот предлагает выбрать город кнопками — иначе заявка повисает.
"""
import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.moderation.ugc import UgcIntake
from tgtraff.moderation.ugc_bot import UgcBot

DEEP_LINKS = {"mgn": "magnitogorsk", "nvk": "novokuznetsk", "che": "cherepovets"}
NAMES = {"magnitogorsk": "Магнитогорск", "novokuznetsk": "Новокузнецк",
         "cherepovets": "Череповец"}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


class FakeBot:
    def __init__(self, tmp_path=None):
        self.calls = []
        self.tmp_path = tmp_path

    async def send_message(self, **kw):
        self.calls.append(("send_message", kw))
        return type("M", (), {"message_id": 1})()

    async def download(self, file_id, destination):
        self.calls.append(("download", {"file_id": file_id, "destination": destination}))
        with open(destination, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0")

    def texts(self):
        return [kw.get("text", "") for n, kw in self.calls if n == "send_message"]


@pytest.fixture
def bot_and_ugc(db, tmp_path):
    bot = FakeBot(tmp_path)
    intake = UgcIntake(db, DEEP_LINKS, media_dir=tmp_path / "media")
    return bot, UgcBot(db, bot, intake, city_names=NAMES, download_dir=tmp_path / "in")


async def test_deep_link_greets_with_the_city(db, bot_and_ugc):
    bot, ugc = bot_and_ugc
    await ugc.handle_start(chat_id=5, user_id=5, username="reader", payload="che")
    assert "Череповец" in bot.texts()[0]
    assert ugc.intake.city_of(5) == "cherepovets"


async def test_visitor_without_a_link_is_asked_for_the_city(db, bot_and_ugc):
    bot, ugc = bot_and_ugc
    await ugc.handle_start(chat_id=5, user_id=5, username="reader", payload=None)
    markup = bot.calls[0][1]["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert set(labels) == set(NAMES.values())


async def test_text_submission_creates_a_draft(db, bot_and_ugc):
    bot, ugc = bot_and_ugc
    await ugc.handle_start(5, 5, "reader", "che")
    draft_id = await ugc.handle_submission(chat_id=5, user_id=5, username="reader",
                                           text="Горит трава у Северстали")
    row = drafts_store.get_draft(db, draft_id)
    assert row["origin"] == "ugc" and row["city"] == "cherepovets"
    assert "спасибо" in " ".join(bot.texts()).lower()


async def test_photo_submission_is_downloaded_and_attached(db, bot_and_ugc):
    bot, ugc = bot_and_ugc
    await ugc.handle_start(5, 5, "reader", "che")
    draft_id = await ugc.handle_submission(5, 5, "reader", text="Пожар",
                                           file_id="AgACphoto", media_kind="photo")
    row = drafts_store.get_draft(db, draft_id)
    assert any(n == "download" for n, _ in bot.calls)
    assert row["media_path"] and row["media_kind"] == "photo"


async def test_submission_without_a_city_asks_first(db, bot_and_ugc):
    bot, ugc = bot_and_ugc
    assert await ugc.handle_submission(9, 9, "reader", text="Что-то горит") is None
    markup = bot.calls[-1][1].get("reply_markup")
    assert markup is not None


async def test_city_button_sets_the_city(db, bot_and_ugc):
    bot, ugc = bot_and_ugc
    await ugc.handle_start(9, 9, "reader", None)
    await ugc.handle_city_choice(user_id=9, chat_id=9, data="city:cherepovets")
    assert ugc.intake.city_of(9) == "cherepovets"


async def test_empty_message_gets_a_hint_not_a_crash(db, bot_and_ugc):
    bot, ugc = bot_and_ugc
    await ugc.handle_start(5, 5, "reader", "che")
    assert await ugc.handle_submission(5, 5, "reader", text="") is None
    assert bot.texts()
