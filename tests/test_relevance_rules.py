"""Правила relevance-фильтра (спека §4): отсев без единого запроса к LLM.

Тексты взяты с живой ленты (docs/reports/dedup-sample-2026-09-02.md) — правило,
написанное под выдуманный пример, на реальном посте обычно не срабатывает.
"""
from tgtraff.relevance.rules import check_post

# --- реклама ---------------------------------------------------------------

ERID_AD = (
    "Что такое ОСАГО и почему оно важно? ОСАГО – это обязательное страхование "
    "автогражданской ответственности. Где оформить ОСАГО быстро и без проблем? "
    "Адрес: Тореза, д. 83 Телефон: +7 (900) 123-45-67 erid:2Vtzqxkmj5d"
)
MARKED_AD = (
    "АО «Горторг» напоминает о способах оплаты школьного питания Организатор питания "
    "напоминает горожанам о доступных способах пополнения лицевого счёта ребёнка. "
    "Подробности Реклама. Erid: 2Vfnxy5PBMe АО «ГОРТОРГ» ИНН 7456040274"
)
VK_AD = (
    "Он дал тебе жизнь. Дай ему покой. Мы — «Профиль Милосердия» — становимся теми самыми "
    "«надёжными руками». Звоните нам ежедневно с 09:00 до 21:00 по номеру 8 800 35 05 405 "
    "Узнать больше #реклама vk.ru О рекламодателе"
)


def test_erid_marker_drops_post_as_ad():
    v = check_post(ERID_AD)
    assert v.verdict == "drop"
    assert v.reason == "ad"


def test_word_reklama_with_advertiser_details_drops_post():
    assert check_post(MARKED_AD).verdict == "drop"


def test_hashtag_reklama_drops_post():
    v = check_post(VK_AD)
    assert v.verdict == "drop"
    assert v.reason == "ad"


def test_vk_marked_as_ads_flag_drops_post_without_any_text_marker():
    v = check_post("Скидки в нашем магазине только сегодня", marked_as_ads=True)
    assert v.verdict == "drop"
    assert v.marker == "marked_as_ads"


def test_news_about_advertising_is_not_an_ad():
    # Слово «реклама» в фактуре новости — не маркер рекламы поста.
    text = ("Прокуратура проверит рекламу микрозаймов на остановках Череповца: "
            "жители пожаловались на баннеры у школ")
    assert check_post(text).verdict != "drop"


# --- чужие конкурсы --------------------------------------------------------

def test_giveaway_with_subscribe_and_repost_is_dropped():
    text = ("🎁 Розыгрыш! Разыгрываем сертификат на 5000 рублей. Условия: подписаться "
            "на канал, поставить лайк и сделать репост. Победителя определим 10 сентября.")
    v = check_post(text)
    assert v.verdict == "drop"
    assert v.reason == "contest"


def test_news_about_a_competition_is_not_a_giveaway():
    # Живой пример: конкурс баянистов «Кубок мира» — это новость города.
    text = ("Дуэт и солист из Магнитогорска завоевали серебро и бронзу на конкурсе "
            "баянистов «Кубок мира». Участвовали 147 ребят из 40 стран, их мастерство "
            "оценивали 50 членов жюри.")
    assert check_post(text).verdict != "drop"


# --- гороскопы -------------------------------------------------------------

def test_horoscope_is_dropped():
    text = ("Гороскоп на 3 сентября: Овнам стоит избегать конфликтов, Тельцам звёзды "
            "советуют заняться финансами, а Близнецам — отдохнуть.")
    v = check_post(text)
    assert v.verdict == "drop"
    assert v.reason == "horoscope"


def test_zodiac_sign_horoscope_is_dropped():
    assert check_post("Какой знак зодиака самый удачливый в сентябре — астропрогноз").verdict == "drop"


# --- всё остальное решает классификатор ------------------------------------

def test_ordinary_news_is_not_decided_by_rules():
    text = ("На перекрёстке Ленина и Гагарина столкнулись две легковушки, "
            "пострадал водитель, на месте работают ГИБДД и скорая.")
    v = check_post(text)
    assert v.verdict == "unsure"
    assert v.reason is None


def test_empty_text_is_left_to_the_classifier():
    assert check_post("").verdict == "unsure"


# --- регрессии, найденные прогоном по живой ленте --------------------------

def test_word_taro_inside_another_word_is_not_a_horoscope():
    # Живой пример: «вдохновлялись старой книгой» — подстрока «таро» внутри «старой».
    text = ("В Москве разместили 55 рекламных билбордов для привлечения туристов в "
            "Вологодскую область. Авторы вдохновлялись старой книгой «Вологодские просторы».")
    assert check_post(text).verdict == "unsure"


def test_news_about_an_advertiser_with_company_details_is_not_an_ad():
    # Живой пример #6505: падежные формы «рекламу»/«рекламы» + ООО в фактуре.
    text = ("УФАС остановило рекламу дезинфекции в Магнитогорске. Челябинское УФАС выдало "
            "предписание ООО «Дезинфекционная станция» из-за рекламы, имитирующей "
            "официальные уведомления государственного учреждения.")
    assert check_post(text).verdict == "unsure"


def test_ord_disclosure_next_to_company_details_is_an_ad():
    # Живой пример #11803: ярлык маркировки в подвале рекламного поста автосалона.
    text = ("Новый JAECOO J7 от 2 649 000 ₽. Приезжайте в ДЦ «СибМоторс». "
            "Реклама. ООО «СибМоторс». ИНН 4217125362. Токен 2СtzqxXPguR")
    v = check_post(text)
    assert v.verdict == "drop"
    assert v.reason == "ad"
