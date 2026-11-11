"""Параметры метрик и ретеншна (спека §9, фаза 6)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MetricsSettings:
    enabled: bool = True

    # ---- просмотры собственных постов ----
    # Bot API просмотры не отдаёт (QUESTIONS §39) — их снимает юзербот, поэтому
    # съём живёт в процессе коллектора: session-файл держит один процесс.
    views_poll_seconds: int = 900
    # Пост добирает просмотры несколько суток; после окна число уже не меняется
    # настолько, чтобы платить за него запросом.
    views_window_hours: int = 72
    views_batch: int = 100

    # ---- снимки канала ----
    subs_poll_seconds: int = 3600
    # Окно охвата: спека §9 просит «охваты постов и ER». Считаем по постам
    # последних суток — это то, на что смотрит и рекламодатель.
    reach_window_hours: int = 24

    # ---- дневной дайджест ----
    digest_enabled: bool = True
    digest_hour: int = 22            # локальный час зоны ниже
    digest_tz: str = "Europe/Moscow"
    digest_top: int = 3              # сколько постов дня показывать

    # ---- инвайт-ссылки под источники трафика ----
    # Ссылки заводятся ЗАРАНЕЕ, до первого рубля закупа: пункт 5 чек-листа
    # прогрева в спеке. Каждой площадке — своя, иначе CPF не разложить.
    invite_links_enabled: bool = True
    invite_sources: tuple[dict, ...] = field(default_factory=tuple)

    def source_specs(self) -> list["InviteSource"]:
        return [InviteSource.from_raw(raw) for raw in self.invite_sources]


@dataclass(frozen=True)
class InviteSource:
    """Один будущий источник трафика: под него заводится своя инвайт-ссылка."""

    name: str
    title: str | None = None
    kind: str = "other"          # paid | organic | own — как считать CPF
    note: str | None = None

    @classmethod
    def from_raw(cls, raw) -> "InviteSource":
        if isinstance(raw, str):
            return cls(name=raw)
        return cls(name=raw["name"], title=raw.get("title"),
                   kind=raw.get("kind", "other"), note=raw.get("note"))

    @property
    def label(self) -> str:
        return self.title or self.name

    @property
    def is_paid(self) -> bool:
        """Платная площадка: только у неё есть CPF — у органики нет знаменателя."""
        return self.kind == "paid"


@dataclass(frozen=True)
class RetentionSettings:
    """Ретеншн фазы 6.

    Спека называет два правила: «`filtered_out`-посты старше 30 дней» и «медиа
    по политике». Политика медиа — ниже: файл живёт `media_days`, после чего
    удаляется с диска, если на него не ссылается драфт. Запись в `raw_media`
    остаётся: по ней видно, что медиа было, и не сломается дедуп по phash.
    """

    enabled: bool = True
    filtered_out_days: int = 30
    media_days: int = 14
    # Снимки и счета — данные гейта месяца 2, их не чистим весь пилот.
    subs_metrics_days: int = 400
    llm_usage_days: int = 400
    hour: int = 4                  # локальный час уборки
    tz: str = "Europe/Moscow"
