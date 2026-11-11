"""Доставка в Telegram (спека §8: постинг ТОЛЬКО через официальный Bot API).

Юзербот к публикации не прикасается — здесь работает aiogram-бот. Медиа
уходит своим файлом (перезалив), форвардов и копипаста нет.
"""
import pytest

from tgtraff.publisher.channels import CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.delivery import DeliveryError, TelegramDelivery
from tgtraff.publisher.format import Button, RenderedPost
from tgtraff.publisher.settings import parse_slots


def city(username="example_channel_1", chat_id="-1001234567890"):
    return CityChannels(city="cherepovets", deep_link="che",
                        tg=TelegramChannel(chat_id=chat_id, username=username,
                                           title="ЧП Череповец"),
                        vk=VkGroup(), slots=parse_slots(["18:00-22:00"]),
                        min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)


POST = RenderedPost("🚗 <b>Хук</b>\n\nТело поста.",
                    Button("📸 Прислать новость", "https://t.me/example_channel_3?start=che"))


class FakeMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    """Ведёт себя как aiogram.Bot: те же имена методов и message_id в ответе."""

    def __init__(self, fail_on=None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_on = fail_on
        self._next = 100

    async def _call(self, name, kw):
        self.calls.append((name, kw))
        if self.fail_on == name:
            raise RuntimeError("Bad Request: chat not found")
        self._next += 1
        return FakeMessage(self._next)

    async def send_message(self, **kw):
        return await self._call("send_message", kw)

    async def send_photo(self, **kw):
        return await self._call("send_photo", kw)

    async def send_video(self, **kw):
        return await self._call("send_video", kw)

    async def edit_message_text(self, **kw):
        return await self._call("edit_message_text", kw)

    async def edit_message_caption(self, **kw):
        return await self._call("edit_message_caption", kw)

    async def edit_message_media(self, **kw):
        return await self._call("edit_message_media", kw)

    async def delete_message(self, **kw):
        return await self._call("delete_message", kw)

    def names(self):
        return [name for name, _ in self.calls]


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    return str(path)


async def test_text_only_post_goes_as_a_message():
    bot = FakeBot()
    result = await TelegramDelivery(bot).publish(city(), POST, media_path=None)
    assert bot.names() == ["send_message"]
    assert result.message_id == "101"


async def test_short_post_with_a_photo_is_one_message_with_caption(photo):
    bot = FakeBot()
    await TelegramDelivery(bot).publish(city(), POST, media_path=photo)
    assert bot.names() == ["send_photo"]
    assert bot.calls[0][1]["caption"] == POST.text


async def test_long_post_goes_as_photo_then_text_and_keeps_both_ids(photo):
    bot = FakeBot()
    long_post = RenderedPost("х" * 1500, POST.button)
    result = await TelegramDelivery(bot).publish(city(), long_post, media_path=photo)
    assert bot.names() == ["send_photo", "send_message"]
    assert result.message_id == "102"           # текстовое сообщение — главное
    assert result.extra_message_ids == ["101"]  # медиа удаляется вместе с ним


async def test_video_is_sent_as_video(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"video")
    bot = FakeBot()
    await TelegramDelivery(bot).publish(city(), POST, media_path=str(path))
    assert bot.names() == ["send_video"]


async def test_button_is_attached_to_the_post():
    bot = FakeBot()
    await TelegramDelivery(bot).publish(city(), POST, media_path=None)
    markup = bot.calls[0][1]["reply_markup"]
    assert markup.inline_keyboard[0][0].url.endswith("?start=che")


async def test_post_without_a_button_has_no_markup():
    bot = FakeBot()
    await TelegramDelivery(bot).publish(city(), RenderedPost("текст"), media_path=None)
    assert bot.calls[0][1]["reply_markup"] is None


async def test_html_is_the_parse_mode():
    bot = FakeBot()
    await TelegramDelivery(bot).publish(city(), POST, media_path=None)
    assert bot.calls[0][1]["parse_mode"] == "HTML"


async def test_url_of_a_public_channel_uses_the_username():
    result = await TelegramDelivery(FakeBot()).publish(city(), POST, media_path=None)
    assert result.url == "https://t.me/example_channel_1/101"


async def test_url_of_a_private_channel_uses_the_internal_form():
    result = await TelegramDelivery(FakeBot()).publish(
        city(username=None), POST, media_path=None)
    assert result.url == "https://t.me/c/1234567890/101"


async def test_edit_of_a_text_post_rewrites_the_text():
    bot = FakeBot()
    await TelegramDelivery(bot).edit(city(), "101", "новый текст", has_media=False)
    assert bot.names() == ["edit_message_text"]


async def test_edit_of_a_post_with_a_caption_rewrites_the_caption():
    bot = FakeBot()
    await TelegramDelivery(bot).edit(city(), "101", "новый текст", has_media=True)
    assert bot.names() == ["edit_message_caption"]


async def test_delete_removes_every_message_of_the_post():
    bot = FakeBot()
    await TelegramDelivery(bot).delete(city(), ["102", "101"])
    assert bot.names() == ["delete_message", "delete_message"]


async def test_failed_delivery_is_reported_as_delivery_error():
    with pytest.raises(DeliveryError):
        await TelegramDelivery(FakeBot(fail_on="send_message")).publish(
            city(), POST, media_path=None)


# --- §61.1: замена картинки в уже вышедшем посте ------------------------------

async def test_edit_media_swaps_the_photo_of_a_published_post(tmp_path):
    """Модератор прислал кадр к вышедшему посту — Bot API умеет заменить
    сам файл только через editMessageMedia (§43: editMessageCaption не умеет)."""
    photo = tmp_path / "new.jpg"
    photo.write_bytes(b"jpeg")
    bot = FakeBot()

    await TelegramDelivery(bot).edit_media(city(), "101", str(photo), caption="подпись")

    name, kw = bot.calls[-1]
    assert name == "edit_message_media"
    assert kw["chat_id"] == "-1001234567890" and kw["message_id"] == 101
    assert kw["media"].caption == "подпись"


async def test_edit_media_reports_a_refusal_as_a_delivery_error(tmp_path):
    photo = tmp_path / "new.jpg"
    photo.write_bytes(b"jpeg")
    bot = FakeBot(fail_on="edit_message_media")

    with pytest.raises(DeliveryError):
        await TelegramDelivery(bot).edit_media(city(), "101", str(photo))


async def test_edit_media_refuses_a_file_that_is_not_there(tmp_path):
    """Файл исчез (ретеншн, сбой скачивания) — в канал уходить нечему,
    и молчаливого «успеха» здесь быть не должно."""
    with pytest.raises(DeliveryError):
        await TelegramDelivery(FakeBot()).edit_media(city(), "101", str(tmp_path / "нет.jpg"))
