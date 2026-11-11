"""Уровень 1 дедупа: нормализация текста и точный хэш (спека §3).

Задача нормализации — снять всё, чем один и тот же текст отличается у разных
источников: подписи каналов, призывы подписаться, ссылки, хэштеги, эмодзи.
После неё копипаст между пабликами даёт ОДИН хэш, а разные новости — разные.
"""
from tgtraff.dedup.text import (
    MIN_HASH_CHARS,
    embed_text,
    entity_keys,
    normalize_text,
    strip_boilerplate,
    text_fingerprint,
)


# ---- нормализация ----

def test_case_and_yo_are_folded():
    assert normalize_text("Пожар в Посёлке") == normalize_text("пожар в поселке")


def test_punctuation_and_whitespace_collapse():
    assert normalize_text("Пожар,   на  улице Ленина!!!") == normalize_text("Пожар на улице Ленина")


def test_digits_are_kept():
    """Дом 12 и дом 21 — разные новости, цифры терять нельзя."""
    assert normalize_text("сгорел дом 12") != normalize_text("сгорел дом 21")


def test_emoji_and_symbols_dropped():
    assert normalize_text("🔥‼️ Пожар на Ленина 🚒") == normalize_text("Пожар на Ленина")


def test_urls_and_mentions_and_hashtags_dropped():
    assert normalize_text(
        "Пожар на Ленина https://verstov.info/news/123 @example_channel_13 #Магнитогорск"
    ) == normalize_text("Пожар на Ленина")


def test_empty_text_normalizes_to_empty():
    assert normalize_text("") == ""
    assert normalize_text("   \n\n  ") == ""
    assert normalize_text(None) == ""


# ---- футеры и призывы источников ----

def test_subscribe_footer_removed():
    a = "На Ленина горел склад.\n\nПодписаться | Прислать новость"
    assert normalize_text(a) == normalize_text("На Ленина горел склад.")


def test_photo_credit_line_removed():
    a = "Горел склад на Ленина.\nФото: ГУ МЧС по Челябинской области"
    assert normalize_text(a) == normalize_text("Горел склад на Ленина.")


def test_read_more_tail_removed():
    a = "Горел склад на Ленина. Читать далее: https://mgorsk.ru/news/1"
    assert normalize_text(a) == normalize_text("Горел склад на Ленина.")


def test_channel_signature_line_removed():
    a = "Горел склад на Ленина.\n\n💬 ЧП Магнитогорск | Подписывайтесь"
    assert normalize_text(a) == normalize_text("Горел склад на Ленина.")


def test_body_words_that_look_like_cta_survive():
    """«Подписаться» вырезаем только в служебной строке, а не из фактуры новости."""
    text = "Жители дома подписались под коллективным обращением к мэру города"
    assert "обращением" in strip_boilerplate(text)
    assert "подписались" in strip_boilerplate(text).lower()


def test_strip_boilerplate_keeps_multiline_body():
    text = "Пожар на Ленина.\nПострадавших нет.\n\nПодписаться"
    stripped = strip_boilerplate(text)
    assert "Пострадавших нет." in stripped
    assert "Подписаться" not in stripped


# ---- точный хэш ----

def test_same_news_from_two_sources_gives_one_fingerprint():
    a = "🚨 В Магнитогорске сгорел склад на улице Ленина. Пострадавших нет.\n\nПодписаться"
    b = "В Магнитогорске сгорел склад на улице Ленина. Пострадавших нет. #Магнитогорск @example_channel_5"
    assert text_fingerprint(a) == text_fingerprint(b) is not None


def test_different_news_give_different_fingerprints():
    a = "В Магнитогорске сгорел склад на улице Ленина. Пострадавших нет."
    b = "В Магнитогорске столкнулись два автобуса на проспекте Карла Маркса."
    assert text_fingerprint(a) != text_fingerprint(b)


def test_short_text_has_no_fingerprint():
    """«Доброе утро» у пяти источников — не одна новость. Короткие тексты
    точным хэшем не склеиваем, ими занимается уровень 2."""
    assert text_fingerprint("Доброе утро!") is None
    assert text_fingerprint("а" * (MIN_HASH_CHARS - 1)) is None
    assert text_fingerprint("а" * MIN_HASH_CHARS) is not None


def test_fingerprint_is_stable_hex():
    fp = text_fingerprint("В Магнитогорске сгорел склад на улице Ленина. Пострадавших нет.")
    assert fp == text_fingerprint("В Магнитогорске сгорел склад на улице Ленина. Пострадавших нет.")
    assert len(fp) == 40 and all(c in "0123456789abcdef" for c in fp)


# ---- текст для эмбеддинга ----

def test_embed_text_keeps_case_and_sentences():
    """Модели работают с обычным текстом: регистр и пунктуацию оставляем,
    убираем только служебное — ссылки, футеры, эмодзи-мусор."""
    out = embed_text("🚨 Пожар на Ленина, 12. Пострадавших нет.\n\nПодписаться | @example_channel_5")
    assert "Пожар на Ленина, 12." in out
    assert "Подписаться" not in out and "@example_channel_5" not in out


def test_embed_text_is_truncated():
    out = embed_text("Пожар. " * 500, limit=200)
    assert len(out) <= 200


def test_embed_text_empty_for_media_only_post():
    assert embed_text("") == ""
    assert embed_text("🔥🔥🔥") == ""


def test_imperative_footer_is_removed_regardless_of_length():
    """Футер «Публикуйте на своих страницах…» длиннее шести слов, но фактуры
    в нём нет: без него у поста-рубрики остаётся три слова, и семантика его
    ни с чем не склеит (живая лента, PROGRESS фазы 3)."""
    text = ("Спокойной ночи, магнитогорцы 🌛\n\n---\n"
            "Публикуйте на своих страницах во «ВКонтакте» фотографии города, "
            "а мы разместим лучшие в нашем паблике")
    assert normalize_text(text) == normalize_text("Спокойной ночи, магнитогорцы")


def test_imperative_footer_does_not_eat_the_news_body():
    text = "Подписан контракт на строительство школы в посёлке Димитрова"
    assert "контракт" in strip_boilerplate(text)


# ---- имена собственные (предохранитель от «одинаковых по шаблону» новостей) ----

def test_entity_keys_take_proper_nouns_not_sentence_starts():
    """Розыскные ориентировки полиции написаны по одному шаблону: отличают их
    имена и адреса, а не формулировки. «Приметы» и «Полицейские» начинают
    предложение — это шаблон, а не фактура."""
    text = ("В Междуреченске разыскивают 14-летнего Александра Кудреватова. "
            "Приметы: рост 175 см. Полицейские просят помощи.")
    keys = entity_keys(text)
    assert "междур" in keys and "алекса" in keys and "кудрев" in keys
    assert "примет" not in keys and "полице" not in keys


def test_entity_keys_ignore_word_endings():
    """«Череповца» и «Череповце» — один и тот же город."""
    assert entity_keys("Школьники Череповца пошли в школу") & \
           entity_keys("Первого сентября в Череповце открыли школу")


def test_entity_keys_of_two_different_orientations_do_not_intersect():
    a = ("В Междуреченске разыскивают 14-летнего Александра Кудреватова, "
         "он ушёл из дома по улице Гули Королевой")
    b = ("В отдел полиции Рудничный по городу Прокопьевску обратился житель: "
         "его сын Захар Кузнецов ушёл из дома по улице 2-я Березовская")
    assert not (entity_keys(a) & entity_keys(b))


def test_platform_menu_footer_removed():
    """«ДТП Новокузнецка👇 Telegram | VK | MAХ» — меню площадок, не фактура."""
    text = "01.09. Момент ДТП на ул. Кутузова.\n\nДТП Новокузнецка👇 Telegram | VK | MAХ"
    assert normalize_text(text) == normalize_text("01.09. Момент ДТП на ул. Кутузова.")


def test_vk_link_markup_removed():
    """ВК-разметка [id123|Имя Фамилия] — подпись автора фото, а не фактура."""
    text = "Доброе утро, магнитогорцы\n\n📷 [id35462855|Лариса Смородина]"
    assert normalize_text(text) == normalize_text("Доброе утро, магнитогорцы")


def test_channel_signature_above_platform_menu_removed():
    """Подпись канала над меню площадок — часть того же подвала."""
    text = "01.09. Момент ДТП на ул. Кутузова.\n\nДТП Новокузнецка👇\nTelegram | VK | MAХ"
    assert normalize_text(text) == normalize_text("01.09. Момент ДТП на ул. Кутузова.")


def test_long_line_above_platform_menu_survives():
    text = ("В Новокузнецке столкнулись автобус и легковой автомобиль, пострадали двое\n"
            "Telegram | VK | MAХ")
    assert "автобус" in strip_boilerplate(text) and "пострадали двое" in strip_boilerplate(text)
