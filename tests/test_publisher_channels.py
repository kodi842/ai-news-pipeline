"""Конфиг доставок (спека §8): город → каналы, слоты, лимиты.

Структура живёт в config/channels.yaml, идентификаторы каналов — в окружении.
Главный предохранитель фазы 5 здесь: пока `mode: test`, боевой канал не
разрешается, даже если его id лежит в .env.
"""
import pytest
import yaml

from tgtraff.publisher.channels import load_channels

BASE = {
    "mode": "test",
    "defaults": {
        "slots": ["07:00-09:00", "12:00-14:00", "18:00-22:00"],
        "min_gap_minutes": 20,
        "max_gap_minutes": 40,
        "daily_limit": 18,
    },
    "ugc_bot": "example_channel_3",
    "cities": {
        "magnitogorsk": {
            "deep_link": "mgn",
            "tg": {
                "test_chat_id_env": "TG_CHANNEL_MAGNITOGORSK_TEST",
                "live_chat_id_env": "TG_CHANNEL_MAGNITOGORSK",
                "username": "example_channel_4",
                "title": "ЧП Магнитогорск",
            },
            "vk": {"enabled": False, "group_id_env": "VK_GROUP_MAGNITOGORSK",
                   "token_env": "VK_GROUP_TOKEN"},
        },
    },
}

TEST_ID, LIVE_ID = "-1001111111111", "-1009999999999"


def write(tmp_path, **over):
    data = {**BASE, **over}
    path = tmp_path / "channels.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), "utf-8")
    return path


def test_test_mode_resolves_the_test_channel(tmp_path):
    cfg = load_channels(write(tmp_path), env={"TG_CHANNEL_MAGNITOGORSK_TEST": TEST_ID})
    assert cfg.cities["magnitogorsk"].tg.chat_id == TEST_ID


def test_live_channel_is_unreachable_until_the_mode_is_switched(tmp_path):
    """Боевой id лежит в окружении, но режим тестовый — publisher его не видит."""
    cfg = load_channels(write(tmp_path), env={"TG_CHANNEL_MAGNITOGORSK": LIVE_ID})
    city = cfg.cities["magnitogorsk"]
    assert city.tg.chat_id is None and not city.tg.is_ready
    assert city.targets() == []


def test_live_mode_resolves_the_live_channel(tmp_path):
    cfg = load_channels(write(tmp_path, mode="live"),
                        env={"TG_CHANNEL_MAGNITOGORSK": LIVE_ID,
                             "TG_CHANNEL_MAGNITOGORSK_TEST": TEST_ID})
    assert cfg.is_live
    assert cfg.cities["magnitogorsk"].tg.chat_id == LIVE_ID


def test_vk_stays_off_until_a_group_token_appears(tmp_path):
    cfg = load_channels(write(tmp_path), env={"TG_CHANNEL_MAGNITOGORSK_TEST": TEST_ID,
                                              "VK_GROUP_MAGNITOGORSK": "123"})
    city = cfg.cities["magnitogorsk"]
    assert not city.vk.is_ready
    assert city.targets() == ["tg"]


def test_vk_switches_on_with_the_flag_and_the_token(tmp_path):
    data = {**BASE}
    data["cities"] = {**BASE["cities"]}
    data["cities"]["magnitogorsk"] = {**BASE["cities"]["magnitogorsk"],
                                      "vk": {"enabled": True,
                                             "group_id_env": "VK_GROUP_MAGNITOGORSK",
                                             "token_env": "VK_GROUP_TOKEN"}}
    cfg = load_channels(write(tmp_path, **data),
                        env={"TG_CHANNEL_MAGNITOGORSK_TEST": TEST_ID,
                             "VK_GROUP_MAGNITOGORSK": "123", "VK_GROUP_TOKEN": "vk1"})
    assert cfg.cities["magnitogorsk"].targets() == ["tg", "vk"]


def test_slots_are_parsed_into_local_windows(tmp_path):
    city = load_channels(write(tmp_path), env={}).cities["magnitogorsk"]
    assert len(city.slots) == 3
    assert (city.slots[0].start.hour, city.slots[0].end.hour) == (7, 9)


def test_defaults_can_be_overridden_per_city(tmp_path):
    data = {**BASE}
    data["cities"] = {"magnitogorsk": {**BASE["cities"]["magnitogorsk"],
                                       "daily_limit": 25, "slots": ["09:00-21:00"]}}
    city = load_channels(write(tmp_path, **data), env={}).cities["magnitogorsk"]
    assert city.daily_limit == 25
    assert len(city.slots) == 1


def test_broken_slot_is_a_config_error(tmp_path):
    data = {**BASE, "defaults": {**BASE["defaults"], "slots": ["07:00 - утро"]}}
    with pytest.raises(ValueError):
        load_channels(write(tmp_path, **data), env={})


def test_project_config_is_loadable_and_starts_in_test_mode():
    """Реальный config/channels.yaml: три города, тестовый режим по умолчанию."""
    cfg = load_channels(env={})
    assert cfg.mode == "test" and not cfg.is_live
    assert set(cfg.cities) == {"magnitogorsk", "novokuznetsk", "cherepovets"}
    assert cfg.ugc_bot == "example_channel_3"
    assert cfg.cities["cherepovets"].deep_link == "che"


def test_test_mode_does_not_borrow_the_live_username(tmp_path):
    """Юзернейм боевого канала в тестовом режиме — мёртвая ссылка: канала
    с таким адресом ещё нет. Подпись и URL поста должны обойтись без него."""
    city = load_channels(write(tmp_path),
                         env={"TG_CHANNEL_MAGNITOGORSK_TEST": TEST_ID}).cities["magnitogorsk"]
    assert city.tg.is_ready
    assert city.tg.username is None and city.tg.link is None


def test_test_channel_can_have_its_own_username(tmp_path):
    data = {**BASE}
    data["cities"] = {"magnitogorsk": {**BASE["cities"]["magnitogorsk"],
                                       "tg": {**BASE["cities"]["magnitogorsk"]["tg"],
                                              "test_username": "example_test_channel"}}}
    city = load_channels(write(tmp_path, **data),
                         env={"TG_CHANNEL_MAGNITOGORSK_TEST": TEST_ID}).cities["magnitogorsk"]
    assert city.tg.username == "example_test_channel"


def test_live_mode_uses_the_live_username(tmp_path):
    city = load_channels(write(tmp_path, mode="live"),
                         env={"TG_CHANNEL_MAGNITOGORSK": LIVE_ID}).cities["magnitogorsk"]
    assert city.tg.username == "example_channel_4"


def test_channel_title_survives_in_test_mode(tmp_path):
    """Подпись поста — бренд канала, а не его адрес: название остаётся и в тесте."""
    city = load_channels(write(tmp_path),
                         env={"TG_CHANNEL_MAGNITOGORSK_TEST": TEST_ID}).cities["magnitogorsk"]
    assert city.tg.title == "ЧП Магнитогорск"


# --- §41: включение ВК по городам --------------------------------------------

def test_vk_is_enabled_per_city(tmp_path):
    """Кросс-постинг включается городом, а не сетью целиком: группа одного
    города может быть готова, а другого — ещё нет."""
    path = tmp_path / "channels.yaml"
    path.write_text("""
mode: test
ugc_bot: example_channel_3
cities:
  cherepovets:
    deep_link: che
    tg: {test_chat_id_env: TG_CHE}
    vk: {enabled: true,  group_id_env: VK_G_CHE, token_env: VK_T_CHE}
  magnitogorsk:
    deep_link: mgn
    tg: {test_chat_id_env: TG_MGN}
    vk: {enabled: false, group_id_env: VK_G_MGN, token_env: VK_T_MGN}
""", "utf-8")
    env = {"TG_CHE": "-1001", "TG_MGN": "-1002",
           "VK_G_CHE": "42", "VK_T_CHE": "tok-che",
           "VK_G_MGN": "77", "VK_T_MGN": "tok-mgn"}

    cfg = load_channels(path, env=env)

    assert cfg.city("cherepovets").targets() == ["tg", "vk"]
    assert cfg.city("magnitogorsk").targets() == ["tg"]


def test_each_city_reads_its_own_vk_token(tmp_path):
    """§41: токен сообщества выдаёт конкретная группа — общего ключа на сеть
    у ВК не бывает."""
    path = tmp_path / "channels.yaml"
    path.write_text("""
mode: test
ugc_bot: example_channel_3
cities:
  cherepovets:
    deep_link: che
    tg: {test_chat_id_env: TG_CHE}
    vk: {enabled: true, group_id_env: VK_G_CHE, token_env: VK_GROUP_TOKEN_CHEREPOVETS}
  magnitogorsk:
    deep_link: mgn
    tg: {test_chat_id_env: TG_MGN}
    vk: {enabled: true, group_id_env: VK_G_MGN, token_env: VK_GROUP_TOKEN_MAGNITOGORSK}
""", "utf-8")
    env = {"TG_CHE": "-1001", "TG_MGN": "-1002", "VK_G_CHE": "42", "VK_G_MGN": "77",
           "VK_GROUP_TOKEN_CHEREPOVETS": "tok-che",
           "VK_GROUP_TOKEN_MAGNITOGORSK": "tok-mgn"}

    cfg = load_channels(path, env=env)

    assert cfg.city("cherepovets").vk.token == "tok-che"
    assert cfg.city("magnitogorsk").vk.token == "tok-mgn"
