"""Проверка машины перед запуском (фаза 6, деплой).

Прогон обещан трёхсуточный и без ручных рестартов, а половина причин, по
которым конвейер встаёт на новом сервере, видна ДО старта: не привезены веса
модели, мало памяти под неё (§29), нет токена, нет часовых поясов, некуда
писать медиа. Дешевле проверить всё это один раз явно, чем ловить по логам
в три часа ночи.

Проверки делятся на две категории: `fatal` — без этого процесс не поднимется
вовсе, остальное — предупреждения (конвейер поедет, но чего-то не будет).
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_RAM_MB = 2048          # решение ревью §29: не меньше 2 ГБ под модель дедупа
MIN_DISK_GB = 5
CITY_ZONES = ("Europe/Moscow", "Asia/Yekaterinburg", "Asia/Novokuznetsk")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = False

    @property
    def mark(self) -> str:
        if self.ok:
            return "✅"
        return "❌" if self.fatal else "⚠️ "


def run_checks(cfg, root: Path | None = None) -> list[Check]:
    root = root or Path(__file__).resolve().parents[2]
    return [
        _python(),
        _ram(),
        _disk(cfg),
        _timezones(),
        _model(cfg, root),
        _offline(cfg),
        _session(cfg),
        _media_dir(cfg),
        _database(cfg),
        *_secrets(cfg),
        _channels(cfg),
        _moderators(cfg),
    ]


def failed(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and c.fatal]


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"{c.mark} {c.name.ljust(width)}  {c.detail}" for c in checks]
    bad = failed(checks)
    warn = [c for c in checks if not c.ok and not c.fatal]
    lines.append("")
    if bad:
        lines.append(f"НЕ ГОТОВО: {len(bad)} обязательных проверок не пройдено")
    elif warn:
        lines.append(f"Готово к запуску, но {len(warn)} предупреждений — см. выше")
    else:
        lines.append("Готово к запуску")
    return "\n".join(lines)


# ---------- машина ----------

def _python() -> Check:
    v = sys.version_info
    ok = (v.major, v.minor) == (3, 12)
    return Check("python 3.12", ok, f"{v.major}.{v.minor}.{v.micro}", fatal=not ok)


def _ram() -> Check:
    total = _total_ram_mb()
    if total is None:
        return Check("память ≥ 2 ГБ", True, "не удалось измерить — проверьте вручную")
    ok = total >= MIN_RAM_MB
    return Check("память ≥ 2 ГБ", ok,
                 f"{total} МБ (модель дедупа занимает ~500 МБ, решение ревью §29)",
                 fatal=not ok)


def _total_ram_mb() -> int | None:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():                          # Linux (VPS)
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    try:                                          # macOS (машина разработчика)
        import subprocess
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
        return int(out.stdout.strip()) // 1048576
    except Exception:                             # noqa: BLE001
        return None


def _disk(cfg) -> Check:
    path = cfg.paths.media_dir
    while not path.exists() and path != path.parent:
        path = path.parent
    free_gb = shutil.disk_usage(path).free / 1024 ** 3
    ok = free_gb >= MIN_DISK_GB
    return Check("место на диске", ok,
                 f"{free_gb:.1f} ГБ свободно (медиа копится ~130 МБ/сут, "
                 f"ретеншн держит объём)")


def _timezones() -> Check:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    missing = []
    for tz in CITY_ZONES:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            missing.append(tz)
    return Check("часовые пояса городов", not missing,
                 "все на месте" if not missing else
                 f"нет {', '.join(missing)} — поставьте tzdata", fatal=bool(missing))


# ---------- конвейер ----------

def _model(cfg, root: Path) -> Check:
    from tgtraff.dedup.embed import LocalEmbedder

    embedder = LocalEmbedder(cfg.dedup, root=root)
    snapshot = embedder.local_snapshot()
    if snapshot is not None:
        return Check("веса модели дедупа", True, f"{snapshot}")
    return Check("веса модели дедупа", False,
                 f"не найдены в {embedder.model_dir} — привезите их заранее "
                 f"(deploy/bundle-models.sh)", fatal=not cfg.dedup.allow_download)


def _offline(cfg) -> Check:
    """На сервере скачивание должно быть выключено (решение ревью §29):
    иначе первый же промах кэша уводит дедуп в сеть, которой там может не быть."""
    ok = not cfg.dedup.allow_download
    return Check("дедуп работает офлайн", ok,
                 "dedup.allow_download: false" if ok else
                 "dedup.allow_download: true — на VPS поставьте false")


def _session(cfg) -> Check:
    path = Path(cfg.paths.session)
    return Check("session-файл юзербота", path.exists(),
                 str(path) if path.exists() else f"нет {path} — перенесите с рабочей машины "
                 f"или войдите: scripts/login.py", fatal=not path.exists())


def _media_dir(cfg) -> Check:
    path = Path(cfg.paths.media_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".preflight"
        probe.write_text("ok")
        probe.unlink()
        return Check("каталог медиа доступен", True, str(path))
    except OSError as exc:
        return Check("каталог медиа доступен", False, f"{path}: {exc}", fatal=True)


def _database(cfg) -> Check:
    from tgtraff.db import Database

    try:
        db = Database(cfg.paths.db)
        posts = db.conn.execute("SELECT COUNT(*) c FROM raw_posts").fetchone()["c"]
        version = db.schema_version()
        db.close()
        return Check("база и схема", True, f"{cfg.paths.db}, схема v{version}, постов {posts}")
    except Exception as exc:                      # noqa: BLE001
        return Check("база и схема", False, f"{exc}", fatal=True)


def _secrets(cfg) -> list[Check]:
    """Секреты живут только в окружении/.env (CLAUDE.md). Здесь проверяется
    наличие, не содержимое: значения не печатаются никуда и никогда."""
    required = (
        ("TG_API_ID/TG_API_HASH", cfg.telegram.is_complete, True,
         "юзербот не подключится к Telegram"),
        ("BOT_TOKEN_ADMIN", bool(cfg.admin_bot_token), True,
         "не поднять админ-бот и публикацию"),
        ("ANTHROPIC_API_KEY", bool(cfg.anthropic_key), True,
         "не будет рерайта и safety"),
        ("BOT_TOKEN_UGC", bool(cfg.ugc_bot_token), False,
         "приём новостей от читателей выключен"),
        ("VK_SERVICE_TOKEN", bool(cfg.vk_token), False,
         "ВК-паблики не собираются"),
    )
    return [Check(name, ok, "задан" if ok else f"не задан — {why}", fatal=fatal and not ok)
            for name, ok, fatal, why in required]


def _channels(cfg) -> Check:
    ready = cfg.channels.ready_cities()
    mode = "БОЕВОЙ" if cfg.channels.is_live else "тестовый"
    return Check("каналы публикации", bool(ready),
                 f"режим {mode}, настроены: {', '.join(ready) or 'ни одного'}")


def _moderators(cfg) -> Check:
    ids = cfg.moderation.moderators or ()
    return Check("модераторы", bool(ids),
                 f"{len(ids)}" if ids else "пусто — карточки некому показывать "
                 "(ADMIN_CHAT_IDS)")


def env_summary() -> str:
    """Что вообще видит процесс — без значений, только имена."""
    keys = sorted(k for k in os.environ if k.startswith(("TG_", "BOT_", "VK_", "ADMIN_",
                                                         "ANTHROPIC_", "TGTRAFF_")))
    return ", ".join(keys) or "переменных конвейера в окружении нет"
