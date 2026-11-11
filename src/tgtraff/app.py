"""Общая обвязка процессов: логи и клиент юзербота."""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

from tgtraff.collector.readonly import ReadOnlyTelegramClient
from tgtraff.config import Config, load_config


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    # httpx логирует каждый GET ленты и каждую картинку — в рабочем логе это шум,
    # за которым не видно самих постов.
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def session_is_busy(session_path: str | Path) -> bool:
    """Session-файл Telethon — SQLite, и живой клиент держит его залоченным.
    Один аккаунт = один процесс: прогрев и коллектор нельзя запускать порознь
    одновременно (для этого у коллектора есть --with-warmup)."""
    path = Path(session_path)
    if not path.exists():
        return False
    conn = None
    try:
        conn = sqlite3.connect(path, timeout=0.2)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        if conn is not None:
            conn.close()


def require_free_session(cfg: Config) -> None:
    if session_is_busy(cfg.paths.session):
        raise SystemExit(
            f"Session-файл {cfg.paths.session} уже занят другим процессом.\n"
            "Один аккаунт Telegram = один процесс. Варианты:\n"
            "  * остановить тот процесс, или\n"
            "  * запускать всё одним: .venv/bin/python scripts/collector.py --with-warmup"
        )


def build_client(cfg: Config) -> ReadOnlyTelegramClient:
    """Юзербот. Клиент физически не умеет писать в Telegram (см. collector/readonly.py)."""
    if not cfg.telegram.is_complete:
        raise SystemExit(
            "Нет TG_API_ID / TG_API_HASH. Возьмите их на https://my.telegram.org → API development tools\n"
            "и положите в .env (шаблон — .env.example). В git .env не попадёт."
        )
    cfg.paths.session.parent.mkdir(parents=True, exist_ok=True)
    return ReadOnlyTelegramClient(
        str(cfg.paths.session),
        cfg.telegram.api_id,
        cfg.telegram.api_hash,
        **cfg.device.as_kwargs(),
        auto_reconnect=True,
        connection_retries=None,       # бесконечный реконнект: коллектор должен переживать сеть
        retry_delay=cfg.collector.reconnect_delay_sec,
        request_retries=5,
        flood_sleep_threshold=60,
    )


async def ensure_authorized(client, cfg: Config) -> None:
    """Разлогин юзербота — аварийная ситуация (спека: «алерт при разлогине»).
    До появления админ-бота (фаза 5) алерт = громкая ошибка в консоль и выход с кодом 2."""
    if not await client.is_user_authorized():
        logging.getLogger("tgtraff").error(
            "ЮЗЕРБОТ РАЗЛОГИНЕН. Сессия %s недействительна — выполните: python scripts/login.py",
            cfg.paths.session,
        )
        raise SystemExit(2)
    me = await client.get_me()
    logging.getLogger("tgtraff").info(
        "юзербот: %s (id=%s) — режим только чтение", me.first_name or me.username, me.id
    )


def sync_sources(cfg: Config, db) -> int:
    """Переносит метаданные источников из конфига в базу.

    Нужна фазе 4: медиа-каскад (спека §5) ставит ведомства на первую ступень,
    а признак `official` живёт в sources.yaml — без синхронизации в базе он
    останется нулём, и фото МЧС будет соревноваться с пабликами на равных."""
    count = 0
    for city in cfg.active():
        for spec in city.all_sources():
            if not spec.key:
                continue
            db.upsert_source(spec)
            count += 1
    return count


def load() -> Config:
    return load_config()
