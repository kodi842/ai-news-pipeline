"""Конфиг конвейера.

Два файла: `config/sources.yaml` (города и источники, ведётся стратегической сессией)
и `config/settings.yaml` (параметры процесса). Секреты — только из окружения/.env.

Мультигородность и мультиканальность — параметры конфига, а не константы в коде
(требование CLAUDE.md).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from tgtraff.clips.settings import ClipSettings
from tgtraff.dedup.settings import DedupSettings
from tgtraff.llm.settings import LlmSettings
from tgtraff.metrics.settings import MetricsSettings, RetentionSettings
from tgtraff.relevance.settings import RelevanceSettings
from tgtraff.publisher.channels import ChannelsConfig, load_channels
from tgtraff.publisher.settings import ModerationSettings, PublisherSettings
from tgtraff.rewriter.media import MediaSettings
from tgtraff.rewriter.settings import RewriterSettings
from tgtraff.safety.settings import SafetySettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = PROJECT_ROOT / "config" / "sources.yaml"
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_CHANNELS = PROJECT_ROOT / "config" / "channels.yaml"

# Типы источников, которые читает TG-коллектор (фаза 1).
TG_SOURCE_TYPES = ("tg", "tg_regional")


@dataclass(frozen=True)
class SourceSpec:
    """Одна запись источника из sources.yaml.

    `key` — точный адрес (username канала / screen_name паблика / url). У записей со
    `status: resolve` адреса ещё нет, есть только `query` — их разрешает фаза 2.
    """

    city: str
    source_type: str
    key: str | None = None
    query: str | None = None
    title: str | None = None
    note: str | None = None
    status: str = "verified"
    priority: str | None = None
    city_filter: str | None = None
    # Ведомство или мэрия, ведущие паблик на чужой площадке: читает их обычный
    # коллектор своей платформы, но для медиа-каскада фазы 5 это официальный
    # источник — приоритет 1 в выборе медиа (спека §5).
    official: bool = False
    # Приватные каналы: username у них нет, читать можно только вступив по инвайту
    # (ссылки снимает с TGStat оператор, решение ревью §15).
    invite: str | None = None
    invite_fallback: str | None = None

    @property
    def invites(self) -> list[str]:
        """Инвайты по порядку: основной, затем запасной."""
        return [link for link in (self.invite, self.invite_fallback) if link]

    @property
    def is_collectable(self) -> bool:
        return bool(self.key) and self.status == "verified"

    @property
    def is_official(self) -> bool:
        return self.official or self.source_type == "official"


@dataclass(frozen=True)
class CityConfig:
    slug: str
    name: str
    region: str | None
    tz: str
    population: int | None = None
    # Координаты — для утреннего прогноза сервисного автопоста (фаза 5).
    lat: float | None = None
    lon: float | None = None
    tg_sources: list[SourceSpec] = field(default_factory=list)
    tg_regional: list[SourceSpec] = field(default_factory=list)
    vk_sources: list[SourceSpec] = field(default_factory=list)
    rss_sources: list[SourceSpec] = field(default_factory=list)
    official: list[SourceSpec] = field(default_factory=list)

    def all_sources(self) -> list[SourceSpec]:
        return [
            *self.tg_sources,
            *self.tg_regional,
            *self.vk_sources,
            *self.rss_sources,
            *self.official,
        ]


@dataclass(frozen=True)
class Paths:
    db: Path
    media_dir: Path
    session: Path


@dataclass(frozen=True)
class CollectorSettings:
    # Догон после простоя ограничен временем: новости старше суток для ЧП-ленты мертвы
    # (решение ревью фазы 1, QUESTIONS §5). Лимит по числу сообщений — предохранитель
    # на очень активном источнике.
    catchup_hours: int = 24
    catchup_limit: int = 200
    media_max_mb: int = 50
    # Насколько назад вообще качаем медиа: у догона истории медиа не забираем.
    media_backfill_hours: int = 24
    media_download_timeout_sec: int = 300
    media_max_attempts: int = 3
    reconnect_delay_sec: int = 15

    @property
    def media_max_bytes(self) -> int:
        return self.media_max_mb * 1024 * 1024


@dataclass(frozen=True)
class PollFallbackSettings:
    """Временный поллинг неподписанных источников на время прогрева (QUESTIONS §8).

    Норматив доставки на это время: подписанные ≤ 1 мин (live), неподписанные ≤ 6 мин.
    Отсюда интервал 3-5 мин с джиттером. Пачки с паузой — чтобы молодой аккаунт
    не выдавал всплеск из трёх десятков запросов подряд."""

    enabled: bool = True
    interval_min_sec: int = 180
    interval_max_sec: int = 300
    batch_size: int = 8
    batch_pause_sec: int = 5
    limit_per_source: int = 20


@dataclass(frozen=True)
class VkSettings:
    """Спека: «wall.get батчами через execute (до 25 пабликов/запрос), поллинг раз в 2-3 мин»."""

    poll_seconds: int = 180
    posts_per_source: int = 20
    api_version: str = "5.199"
    request_timeout_sec: int = 20


@dataclass(frozen=True)
class RssSettings:
    """Спека: «feedparser, поллинг раз в 5-10 мин (местные СМИ, МЧС, ГИБДД, администрация)»."""

    poll_seconds: int = 420
    entries_per_feed: int = 30
    request_timeout_sec: int = 20
    # Представляемся честно: закрытая лента и жалоба на «непонятного бота» нам не нужны.
    user_agent: str = "tgtraff-newsbot/0.1 (+news aggregator; contact via channel)"


@dataclass(frozen=True)
class WarmupSettings:
    tz: str = "Europe/Moscow"
    per_day_min: int = 5
    per_day_max: int = 10
    hour_from: int = 10
    hour_to: int = 23
    gap_minutes_min: int = 12
    gap_minutes_max: int = 95


@dataclass(frozen=True)
class TelegramCredentials:
    api_id: int | None
    api_hash: str | None

    @property
    def is_complete(self) -> bool:
        return bool(self.api_id and self.api_hash)


@dataclass(frozen=True)
class DeviceProfile:
    """Отпечаток клиента. Должен совпадать с тем, на котором заведена сессия:
    расхождение device/app_version — заметный сигнал автоматизации для Telegram."""

    model: str = "Desktop"
    system_version: str = "Windows 10"
    app_version: str = "4.16.8 x64"
    lang_code: str = "en"
    system_lang_code: str = "en"

    def as_kwargs(self) -> dict[str, str]:
        return {
            "device_model": self.model,
            "system_version": self.system_version,
            "app_version": self.app_version,
            "lang_code": self.lang_code,
            "system_lang_code": self.system_lang_code,
        }


@dataclass(frozen=True)
class Config:
    cities: dict[str, CityConfig]
    active_cities: list[str]
    paths: Paths
    collector: CollectorSettings
    poll_fallback: PollFallbackSettings
    vk: VkSettings
    rss: RssSettings
    warmup: WarmupSettings
    dedup: DedupSettings
    llm: LlmSettings
    relevance: RelevanceSettings
    rewriter: RewriterSettings
    media: MediaSettings
    safety: SafetySettings
    publisher: PublisherSettings
    moderation: ModerationSettings
    metrics: MetricsSettings
    retention: RetentionSettings
    clips: ClipSettings
    channels: ChannelsConfig
    telegram: TelegramCredentials
    device: DeviceProfile
    vk_token: str | None = None
    anthropic_key: str | None = None
    # Токены ботов фазы 5. Публикация — только через Bot API (CLAUDE.md).
    admin_bot_token: str | None = None
    publisher_bot_token: str | None = None
    ugc_bot_token: str | None = None

    def city_meta(self) -> dict[str, dict]:
        """Города в виде, нужном сервисным автопостам: имя, часы, координаты."""
        return {c.slug: {"name": c.name, "tz": c.tz, "lat": c.lat, "lon": c.lon}
                for c in self.active()}

    def city_tz(self) -> dict[str, str]:
        return {slug: city.tz for slug, city in self.cities.items()}

    def active(self) -> list[CityConfig]:
        return [self.cities[slug] for slug in self.active_cities if slug in self.cities]

    def collectable_sources(self, *source_types: str) -> list[SourceSpec]:
        """Источники активных городов заданных типов, готовые к сбору."""
        out: list[SourceSpec] = []
        for city in self.active():
            for src in city.all_sources():
                if src.source_type in source_types and src.is_collectable:
                    out.append(src)
        return out

    def collectable_tg_sources(self) -> list[SourceSpec]:
        """Источники, которые TG-коллектор может читать прямо сейчас."""
        out: list[SourceSpec] = []
        for city in self.active():
            for src in (*city.tg_sources, *city.tg_regional):
                if src.is_collectable:
                    out.append(src)
        return out


def _mk_source(city: str, source_type: str, raw: dict) -> SourceSpec:
    key = raw.get("u") or raw.get("s") or raw.get("url")
    city_filter = raw.get("filter")
    return SourceSpec(
        city=city,
        source_type=source_type,
        key=key,
        query=raw.get("q"),
        title=raw.get("title"),
        note=raw.get("note"),
        status=raw.get("status", "verified"),
        priority=raw.get("priority"),
        city_filter=city_filter.lower() if city_filter else None,
        official=bool(raw.get("official", False)),
        invite=raw.get("invite"),
        invite_fallback=raw.get("invite_fallback"),
    )


def _abs(root: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (root / p)


def _moderation(raw: dict) -> ModerationSettings:
    """Список модераторов можно держать и в конфиге, и в окружении:
    ADMIN_CHAT_IDS перекрывает файл — на сервере это удобнее."""
    raw = dict(raw)
    env = os.getenv("ADMIN_CHAT_IDS") or os.getenv("ADMIN_ALERT_CHAT_ID")
    ids = raw.pop("moderators", None) or []
    if env:
        ids = [int(x) for x in env.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()]
    return ModerationSettings(**raw, moderators=tuple(int(i) for i in ids))


def _clips(raw: dict, root: Path) -> ClipSettings:
    """Настройки завода клипов. Каталоги приводим к абсолютным: процесс клипов
    запускается и из CLI, и из контейнера, а mp4 должны ложиться в один и тот
    же data/clips, а не рядом с текущим каталогом."""
    raw = dict(raw)
    for key in ("out_dir", "work_dir", "whisper_dir"):
        if raw.get(key):
            raw[key] = str(_abs(root, raw[key]))
    defaults = ClipSettings()
    for key, value in (("out_dir", defaults.out_dir), ("work_dir", defaults.work_dir),
                       ("whisper_dir", defaults.whisper_dir)):
        raw.setdefault(key, str(_abs(root, value)))
    return ClipSettings(**raw)


def _metrics(raw: dict) -> MetricsSettings:
    """Список источников трафика в yaml — это list, а настройки заморожены:
    приводим к кортежу, иначе конфиг нельзя было бы хешировать и сравнивать."""
    raw = dict(raw)
    raw["invite_sources"] = tuple(raw.get("invite_sources") or ())
    return MetricsSettings(**raw)


def load_config(
    sources_path: Path | None = None,
    settings_path: Path | None = None,
    channels_path: Path | None = None,
    root: Path | None = None,
) -> Config:
    root = root or PROJECT_ROOT
    load_dotenv(root / ".env", override=False)

    sources_raw = yaml.safe_load((sources_path or DEFAULT_SOURCES).read_text("utf-8")) or {}
    settings_raw = yaml.safe_load((settings_path or DEFAULT_SETTINGS).read_text("utf-8")) or {}

    cities: dict[str, CityConfig] = {}
    for slug, block in (sources_raw.get("cities") or {}).items():
        cities[slug] = CityConfig(
            slug=slug,
            name=block.get("name", slug),
            region=block.get("region"),
            tz=block.get("tz", "Europe/Moscow"),
            population=block.get("population"),
            lat=block.get("lat"),
            lon=block.get("lon"),
            tg_sources=[_mk_source(slug, "tg", r) for r in block.get("tg_sources") or []],
            tg_regional=[_mk_source(slug, "tg_regional", r) for r in block.get("tg_regional") or []],
            vk_sources=[_mk_source(slug, "vk", r) for r in block.get("vk_sources") or []],
            rss_sources=[_mk_source(slug, "rss", r) for r in block.get("rss_sources") or []],
            official=[_mk_source(slug, "official", r) for r in block.get("official") or []],
        )

    active = settings_raw.get("active_cities") or list(cities)
    env_active = os.getenv("TGTRAFF_ACTIVE_CITIES")
    if env_active:
        active = [c.strip() for c in env_active.split(",") if c.strip()]

    paths_raw = settings_raw.get("paths") or {}
    paths = Paths(
        db=_abs(root, os.getenv("TGTRAFF_DB_PATH") or paths_raw.get("db", "data/tgtraff.db")),
        media_dir=_abs(root, os.getenv("TGTRAFF_MEDIA_DIR") or paths_raw.get("media_dir", "data/media")),
        session=_abs(root, os.getenv("TGTRAFF_SESSION") or paths_raw.get("session", "secrets/userbot.session")),
    )

    api_id = os.getenv("TG_API_ID")
    return Config(
        cities=cities,
        active_cities=[c for c in active if c in cities],
        paths=paths,
        collector=CollectorSettings(**(settings_raw.get("collector") or {})),
        poll_fallback=PollFallbackSettings(**(settings_raw.get("poll_fallback") or {})),
        vk=VkSettings(**(settings_raw.get("vk") or {})),
        rss=RssSettings(**(settings_raw.get("rss") or {})),
        warmup=WarmupSettings(**(settings_raw.get("warmup") or {})),
        dedup=DedupSettings(**(settings_raw.get("dedup") or {})),
        llm=LlmSettings(**(settings_raw.get("llm") or {})),
        relevance=RelevanceSettings(**(settings_raw.get("relevance") or {})),
        rewriter=RewriterSettings(**(settings_raw.get("rewriter") or {})),
        media=MediaSettings(**(settings_raw.get("media") or {})),
        safety=SafetySettings(**(settings_raw.get("safety") or {})),
        publisher=PublisherSettings(**(settings_raw.get("publisher") or {})),
        moderation=_moderation(settings_raw.get("moderation") or {}),
        metrics=_metrics(settings_raw.get("metrics") or {}),
        retention=RetentionSettings(**(settings_raw.get("retention") or {})),
        clips=_clips(settings_raw.get("clips") or {}, root),
        channels=load_channels(channels_path or DEFAULT_CHANNELS),
        telegram=TelegramCredentials(
            api_id=int(api_id) if api_id and api_id.isdigit() else None,
            api_hash=os.getenv("TG_API_HASH") or None,
        ),
        device=DeviceProfile(
            model=os.getenv("TG_DEVICE_MODEL") or DeviceProfile.model,
            system_version=os.getenv("TG_SYSTEM_VERSION") or DeviceProfile.system_version,
            app_version=os.getenv("TG_APP_VERSION") or DeviceProfile.app_version,
            lang_code=os.getenv("TG_LANG_CODE") or DeviceProfile.lang_code,
            system_lang_code=os.getenv("TG_SYSTEM_LANG_CODE")
                             or os.getenv("TG_LANG_CODE") or DeviceProfile.system_lang_code,
        ),
        vk_token=os.getenv("VK_SERVICE_TOKEN") or None,
        admin_bot_token=os.getenv("BOT_TOKEN_ADMIN") or os.getenv("ADMIN_BOT_TOKEN") or None,
        # Отдельного бота-паблишера может не быть: тогда публикует админ-бот
        # (решение владельца, сессия фазы 5) — в канале пост всё равно выходит
        # от имени канала.
        publisher_bot_token=(os.getenv("BOT_TOKEN_PUBLISHER")
                             or os.getenv("BOT_TOKEN_ADMIN")
                             or os.getenv("ADMIN_BOT_TOKEN") or None),
        ugc_bot_token=os.getenv("BOT_TOKEN_UGC") or None,
        # Ключ Anthropic живёт только в окружении/.env — в конфиг он не попадает
        # и в git не уезжает (CLAUDE.md, «Секреты»).
        anthropic_key=os.getenv("ANTHROPIC_API_KEY") or None,
    )
