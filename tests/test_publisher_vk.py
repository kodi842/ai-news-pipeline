"""Кросс-постинг в VK-группу (спека §8): одна запись очереди → доставки в ТГ и ВК.

Адаптер написан, но в конфиге выключен: сервисный ключ фазы 2 писать на стену
не умеет, нужен токен сообщества с правами wall+photos.
"""
import pytest

from tgtraff.publisher.channels import CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.delivery import DeliveryError
from tgtraff.publisher.format import PostContent, render_plain
from tgtraff.publisher.settings import parse_slots
from tgtraff.publisher.vk import VkDelivery


def city(vk=None):
    return CityChannels(city="cherepovets", deep_link="che",
                        tg=TelegramChannel(chat_id="-1001", username="example_channel_1",
                                           title="ЧП Череповец"),
                        vk=vk or VkGroup(enabled=True, group_id="42", token="vk1"),
                        slots=parse_slots(["18:00-22:00"]), min_gap_minutes=20,
                        max_gap_minutes=40, daily_limit=18)


POST = PostContent(city="cherepovets", title="Хук", body="Тело поста.",
                   category="dtp", district="Индустриальный район")


class FakeVk:
    """Двойник VK API: записывает вызовы, отвечает как настоящий."""

    def __init__(self, fail=None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def call(self, method, **params):
        self.calls.append((method, params))
        if self.fail == method:
            from tgtraff.collector.vk_collector import VkError
            raise VkError(15, "Access denied")
        if method == "photos.getWallUploadServer":
            return {"upload_url": "https://upload.vk/x"}
        if method == "photos.saveWallPhoto":
            return [{"owner_id": -42, "id": 7}]
        if method == "wall.post":
            return {"post_id": 555}
        raise AssertionError(f"незапланированный вызов {method}")

    def methods(self):
        return [m for m, _ in self.calls]


async def fake_upload(url, path):
    return {"server": 1, "photo": "[]", "hash": "h"}


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(b"jpeg")
    return str(path)


def test_vk_text_has_no_html_tags():
    text = render_plain(POST, city(), ugc_bot="example_channel_3")
    assert "<b>" not in text and "&" not in text
    assert text.startswith("🚗 Хук")
    assert "#Индустриальный" in text


async def test_post_without_media_is_a_plain_wall_post():
    api = FakeVk()
    result = await VkDelivery(api, uploader=fake_upload).publish(
        city(), render_plain(POST, city(), "example_channel_3"), media_path=None)
    assert api.methods() == ["wall.post"]
    assert result.message_id == "555"
    assert result.url == "https://vk.com/wall-42_555"


async def test_photo_is_uploaded_and_attached(photo):
    api = FakeVk()
    await VkDelivery(api, uploader=fake_upload).publish(
        city(), "текст", media_path=photo)
    assert api.methods() == ["photos.getWallUploadServer", "photos.saveWallPhoto", "wall.post"]
    assert api.calls[-1][1]["attachments"] == "photo-42_7"


async def test_post_goes_from_the_group_not_from_a_person(photo):
    api = FakeVk()
    await VkDelivery(api, uploader=fake_upload).publish(city(), "текст", media_path=None)
    params = api.calls[-1][1]
    assert params["owner_id"] == -42 and params["from_group"] == 1


async def test_upload_failure_does_not_lose_the_post(photo):
    """Картинка не загрузилась — пост всё равно выходит текстом."""
    async def broken(url, path):
        raise RuntimeError("upload 500")

    api = FakeVk()
    result = await VkDelivery(api, uploader=broken).publish(city(), "текст", media_path=photo)
    assert api.methods() == ["photos.getWallUploadServer", "wall.post"]
    assert result.message_id == "555"


async def test_api_error_is_reported_as_delivery_error():
    api = FakeVk(fail="wall.post")
    with pytest.raises(DeliveryError):
        await VkDelivery(api, uploader=fake_upload).publish(city(), "текст", media_path=None)


# --- §41: у каждого города СВОЙ токен сообщества ------------------------------

async def test_each_city_posts_with_its_own_community_token():
    """Токен сообщества выдаётся конкретной группой: одним ключом на сеть
    в чужую группу не написать. Адаптер обязан брать токен из города."""
    seen = {}

    def factory(token):
        seen[token] = seen.get(token) or FakeVk()
        return seen[token]

    delivery = VkDelivery(api_factory=factory, uploader=fake_upload)
    che = city(VkGroup(enabled=True, group_id="42", token="token-che"))
    mgn = city(VkGroup(enabled=True, group_id="77", token="token-mgn"))

    await delivery.publish(che, "текст", None)
    await delivery.publish(mgn, "текст", None)

    assert set(seen) == {"token-che", "token-mgn"}
    assert seen["token-mgn"].calls[0][1]["owner_id"] == -77


async def test_the_api_object_is_reused_between_posts_of_one_city():
    """Новая сессия на каждый пост — лишние коннекты и лишний шанс словить
    лимит частоты: клиент на токен создаётся один раз."""
    made = []

    def factory(token):
        made.append(token)
        return FakeVk()

    delivery = VkDelivery(api_factory=factory, uploader=fake_upload)
    await delivery.publish(city(), "раз", None)
    await delivery.publish(city(), "два", None)

    assert made == ["vk1"]


async def test_city_without_a_token_is_refused_before_any_call():
    """`enabled: true` без токена — ошибка конфига, а не молчаливый пропуск:
    иначе ВК-группа тихо остаётся пустой и посевы бьют в мёртвую площадку."""
    delivery = VkDelivery(api_factory=lambda token: FakeVk(), uploader=fake_upload)

    with pytest.raises(DeliveryError):
        await delivery.publish(city(VkGroup(enabled=True, group_id="42", token=None)),
                               "текст", None)
