"""Проверка машины перед запуском (фаза 6, деплой).

Смысл проверок — не «всё зелёное», а «что именно не даст конвейеру прожить трое
суток». Поэтому здесь проверяется деление на обязательное и предупреждения:
systemd отказывается стартовать только по первому.
"""
from dataclasses import replace

import pytest

from tgtraff import preflight
from tgtraff.config import load_config
from tgtraff.preflight import Check, failed, render, run_checks


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("TGTRAFF_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TGTRAFF_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("TGTRAFF_SESSION", str(tmp_path / "userbot.session"))
    return load_config()


def names(checks):
    return {c.name: c for c in checks}


def test_missing_session_is_fatal(cfg):
    check = names(run_checks(cfg))["session-файл юзербота"]
    assert not check.ok and check.fatal


def test_present_session_passes(cfg):
    from pathlib import Path

    Path(cfg.paths.session).write_bytes(b"session")
    assert names(run_checks(cfg))["session-файл юзербота"].ok


def test_download_allowed_is_a_warning_not_a_stopper(cfg):
    """`allow_download: true` на сервере — повод поправить конфиг, но не повод
    не пустить конвейер: с весами на диске он всё равно работает офлайн."""
    check = names(run_checks(replace(cfg, dedup=replace(cfg.dedup, allow_download=True))))
    offline = check["дедуп работает офлайн"]
    assert not offline.ok and not offline.fatal


def test_missing_model_is_fatal_only_when_download_is_off(cfg, tmp_path):
    dedup = replace(cfg.dedup, model_dir=str(tmp_path / "empty"), allow_download=False)
    check = names(run_checks(replace(cfg, dedup=dedup)))["веса модели дедупа"]
    assert not check.ok and check.fatal

    dedup = replace(dedup, allow_download=True)
    check = names(run_checks(replace(cfg, dedup=dedup)))["веса модели дедупа"]
    assert not check.ok and not check.fatal


def test_low_ram_is_fatal(cfg, monkeypatch):
    """Решение ревью §29: меньше 2 ГБ — модель дедупа не поднимется."""
    monkeypatch.setattr(preflight, "_total_ram_mb", lambda: 1024)
    check = names(run_checks(cfg))["память ≥ 2 ГБ"]
    assert not check.ok and check.fatal

    monkeypatch.setattr(preflight, "_total_ram_mb", lambda: 4096)
    assert names(run_checks(cfg))["память ≥ 2 ГБ"].ok


def test_unmeasurable_ram_does_not_block_the_start(cfg, monkeypatch):
    monkeypatch.setattr(preflight, "_total_ram_mb", lambda: None)
    assert names(run_checks(cfg))["память ≥ 2 ГБ"].ok


def test_failed_lists_only_the_fatal_ones():
    checks = [Check("ok", True), Check("предупреждение", False), Check("стоп", False, fatal=True)]
    assert [c.name for c in failed(checks)] == ["стоп"]
    assert "НЕ ГОТОВО" in render(checks)


def test_render_says_ready_when_everything_passes():
    assert "Готово к запуску" in render([Check("всё хорошо", True)])


def test_secrets_are_never_printed(cfg, monkeypatch):
    """В отчёт уходит только «задан/не задан»: значения ключей не должны
    попасть ни в консоль, ни в журнал systemd."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-секретное-значение")
    text = render(run_checks(load_config()))
    assert "секретное-значение" not in text
