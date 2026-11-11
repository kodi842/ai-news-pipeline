"""Куда публикует конвейер: город → каналы доставки (спека §8).

Структура — `config/channels.yaml`, идентификаторы каналов — в окружении
(`.env`, в git не попадает). У тестового и боевого канала РАЗНЫЕ переменные,
и режим переключается одной строкой `mode:` — пока он `test`, боевой id
не разрешается, даже если лежит рядом в .env. Это и есть предохранитель
«публикуем только в тестовые каналы, пока владелец не разрешил боевые».

Доставки платформо-независимы (спека §8): город отдаёт список готовых целей,
а как их доставить — знает адаптер платформы (`telegram.py`, `vk.py`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from tgtraff.publisher.settings import SlotWindow, parse_slots

DEFAULT_CHANNELS = Path(__file__).resolve().parents[3] / "config" / "channels.yaml"

TEST_MODE, LIVE_MODE = "test", "live"


@dataclass(frozen=True)
class TelegramChannel:
    chat_id: str | None
    username: str | None = None
    title: str | None = None

    @property
    def is_ready(self) -> bool:
        return bool(self.chat_id)

    @property
    def link(self) -> str | None:
        return f"https://t.me/{self.username}" if self.username else None


@dataclass(frozen=True)
class VkGroup:
    enabled: bool = False
    group_id: str | None = None
    token: str | None = None

    @property
    def is_ready(self) -> bool:
        return bool(self.enabled and self.group_id and self.token)


@dataclass(frozen=True)
class CityChannels:
    city: str
    deep_link: str
    tg: TelegramChannel
    vk: VkGroup
    slots: tuple[SlotWindow, ...]
    min_gap_minutes: int
    max_gap_minutes: int
    daily_limit: int
    signature: bool = True
    ugc_button: bool = True

    def targets(self) -> list[str]:
        """Готовые к доставке платформы. Ненастроенная — молча пропускается:
        отсутствие ВК-токена не должно останавливать публикацию в Telegram."""
        out = []
        if self.tg.is_ready:
            out.append("tg")
        if self.vk.is_ready:
            out.append("vk")
        return out


@dataclass(frozen=True)
class ChannelsConfig:
    mode: str
    ugc_bot: str
    cities: dict[str, CityChannels]

    @property
    def is_live(self) -> bool:
        return self.mode == LIVE_MODE

    def city(self, slug: str) -> CityChannels | None:
        return self.cities.get(slug)

    def ready_cities(self) -> list[str]:
        return [slug for slug, c in self.cities.items() if c.targets()]


def _env(env, name: str | None) -> str | None:
    if not name:
        return None
    value = env.get(name)
    return str(value).strip() or None if value is not None else None


def load_channels(path: Path | str | None = None, env=None) -> ChannelsConfig:
    env = os.environ if env is None else env
    raw = yaml.safe_load(Path(path or DEFAULT_CHANNELS).read_text("utf-8")) or {}

    mode = str(raw.get("mode", TEST_MODE)).strip().lower()
    if mode not in (TEST_MODE, LIVE_MODE):
        raise ValueError(f"channels.mode должен быть test или live, получено {mode!r}")
    defaults = raw.get("defaults") or {}

    cities: dict[str, CityChannels] = {}
    for slug, block in (raw.get("cities") or {}).items():
        block = block or {}
        tg_raw = block.get("tg") or {}
        live = mode == LIVE_MODE
        key = "live_chat_id_env" if live else "test_chat_id_env"
        # Юзернейм тоже зависит от режима: боевой адрес канала в тестовом прогоне
        # ещё не существует, и подпись со ссылкой на него вела бы в никуда.
        # Тестовый канал обычно приватный — тогда username нет вовсе, и пост
        # ссылается на себя внутренней формой t.me/c/<id>/<message_id>.
        username = tg_raw.get("username") if live else tg_raw.get("test_username")
        # Название канала от режима не зависит: в подписи это бренд, а не адрес,
        # и тестовый прогон должен показывать пост ровно таким, каким он выйдет.
        title = tg_raw.get("title") if live else (tg_raw.get("test_title")
                                                  or tg_raw.get("title"))
        vk_raw = block.get("vk") or {}
        cities[slug] = CityChannels(
            city=slug,
            deep_link=str(block.get("deep_link") or slug),
            tg=TelegramChannel(
                chat_id=_env(env, tg_raw.get(key)),
                username=username,
                title=title,
            ),
            vk=VkGroup(
                enabled=bool(vk_raw.get("enabled", False)),
                group_id=_env(env, vk_raw.get("group_id_env")),
                token=_env(env, vk_raw.get("token_env")),
            ),
            slots=parse_slots(block.get("slots", defaults.get("slots"))),
            min_gap_minutes=int(block.get("min_gap_minutes",
                                          defaults.get("min_gap_minutes", 20))),
            max_gap_minutes=int(block.get("max_gap_minutes",
                                          defaults.get("max_gap_minutes", 40))),
            daily_limit=int(block.get("daily_limit", defaults.get("daily_limit", 18))),
            signature=bool(block.get("signature", defaults.get("signature", True))),
            ugc_button=bool(block.get("ugc_button", defaults.get("ugc_button", True))),
        )

    return ChannelsConfig(mode=mode, ugc_bot=str(raw.get("ugc_bot") or "example_channel_3"),
                          cities=cities)
