"""Формат-шаблоны постов (спека §8, подпись — PINS.md §3).

Эмодзи-префикс категории, жирный хук, тело, хэштег района, подпись со ссылкой
на канал и на UGC-бота. Кнопка «Прислать новость» несёт deep-link города.
"""
from tgtraff.publisher.channels import CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.format import (PostContent, plan_delivery, render_plain,
                                      render_post)
from tgtraff.publisher.settings import parse_slots

CITY = CityChannels(
    city="cherepovets", deep_link="che",
    tg=TelegramChannel(chat_id="-1001", username="example_channel_1", title="ЧП Череповец"),
    vk=VkGroup(), slots=parse_slots(["18:00-22:00"]),
    min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)

POST = PostContent(city="cherepovets", title="Такси вылетело на тротуар у «Макси»",
                   body="Пострадали два человека, их увезли в больницу.",
                   category="dtp", urgency="normal", district="Индустриальный район")


def render(post=POST, city=CITY, **kw):
    return render_post(post, city, ugc_bot="example_channel_3", **kw)


def test_category_emoji_opens_the_post_and_the_hook_is_bold():
    text = render().text
    assert text.startswith("🚗 <b>Такси вылетело на тротуар у «Макси»</b>")


def test_body_follows_the_hook():
    assert "Пострадали два человека" in render().text


def test_district_becomes_a_hashtag():
    assert "#Индустриальный" in render().text


def test_post_without_a_district_has_no_hashtag():
    text = render(PostContent(**{**POST.__dict__, "district": None})).text
    assert "#" not in text


def test_signature_links_to_the_channel_and_to_the_ugc_bot():
    text = render().text
    assert '<a href="https://t.me/example_channel_1">ЧП Череповец</a>' in text
    assert "https://t.me/example_channel_3?start=che" in text


def test_signature_can_be_switched_off():
    city = CityChannels(**{**CITY.__dict__, "signature": False})
    assert "t.me/example_channel_1" not in render(city=city).text


def test_flash_gets_the_lightning_prefix():
    text = render(PostContent(**{**POST.__dict__, "urgency": "flash"})).text
    assert text.startswith("⚡️")


def test_every_category_has_its_own_emoji():
    from tgtraff.rewriter.settings import CATEGORIES
    seen = set()
    for category in CATEGORIES:
        text = render(PostContent(**{**POST.__dict__, "category": category})).text
        seen.add(text.split()[0])
    assert len(seen) == len(CATEGORIES)


def test_html_from_the_source_is_escaped():
    post = PostContent(**{**POST.__dict__, "body": "Пишут <b>жирным</b> & со стрелкой"})
    text = render(post).text
    assert "&lt;b&gt;" in text and "&amp;" in text


def test_ugc_button_carries_the_city_deep_link():
    button = render().button
    assert button and button.url == "https://t.me/example_channel_3?start=che"
    assert "Прислать новость" in button.text


def test_button_can_be_switched_off():
    city = CityChannels(**{**CITY.__dict__, "ugc_button": False})
    assert render(city=city).button is None


# --- как доставлять: подпись к медиа или отдельным сообщением ----------------

def test_short_post_with_a_photo_goes_one_message():
    plan = plan_delivery("короткий текст", media_path="/m/a.jpg")
    assert plan.mode == "photo_caption"


def test_long_post_with_a_photo_goes_photo_plus_text():
    """Подпись к медиа в Telegram — 1024 знака; длинный пост не режем."""
    plan = plan_delivery("х" * 1500, media_path="/m/a.jpg")
    assert plan.mode == "photo_then_text"


def test_post_without_media_is_a_plain_message():
    assert plan_delivery("текст", media_path=None).mode == "text"


def test_video_is_recognised_by_extension():
    assert plan_delivery("текст", media_path="/m/a.mp4").kind == "video"


def test_signature_without_a_username_stays_plain_text():
    """Тестовый канал без юзернейма: имя канала в подписи есть, ссылки нет."""
    from tgtraff.publisher.channels import TelegramChannel

    city = CityChannels(**{**CITY.__dict__,
                           "tg": TelegramChannel(chat_id="-1001", username=None,
                                                 title="ЧП Череповец ТЕСТ")})
    text = render(city=city).text
    assert "ЧП Череповец ТЕСТ" in text
    assert "t.me/example_channel_1" not in text
    assert "example_channel_3?start=che" in text          # кнопка UGC остаётся


def test_service_post_keeps_its_own_links():
    """Живой дефект первого топ-5: тело сервисного поста верстаем мы сами,
    и его ссылки должны дойти до канала, а не выйти экранированным текстом."""
    post = PostContent(city="cherepovets", title="Топ-5 дня",
                       body='1. <a href="https://t.me/c/1/2">Новость</a>',
                       category="city", body_html=True)
    assert '<a href="https://t.me/c/1/2">Новость</a>' in render(post).text


def test_trusted_body_does_not_open_the_door_to_source_text():
    """Доверие к разметке — свойство сервисного поста, а не общее послабление:
    у обычного драфта тело по-прежнему экранируется."""
    post = PostContent(city="cherepovets", title="Хук", body="<b>из источника</b>",
                       category="dtp")
    assert "&lt;b&gt;" in render(post).text


def test_vk_version_of_a_service_post_has_no_tags():
    post = PostContent(city="cherepovets", title="Топ-5 дня",
                       body='1. <a href="https://t.me/c/1/2">Новость</a>',
                       category="city", body_html=True)
    text = render_plain(post, CITY, "example_channel_3")
    assert "<a" not in text and "</a>" not in text
    assert "Новость" in text
