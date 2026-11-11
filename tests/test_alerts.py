"""Критические алерты до появления админ-бота (решение ревью, QUESTIONS §6).

Полноценные алерты — фаза 5. Здесь минимум: если в .env есть токен бота и чат,
критическое событие уезжает голым HTTP-вызовом Bot API. Нет токена — работаем
как раньше, в консоль. Алерт НИКОГДА не должен ронять конвейер.
"""
import logging

from tgtraff.alerts import Alerter


class FakeSender:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    async def __call__(self, token, chat_id, text):
        self.calls.append((token, chat_id, text))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_alert_goes_to_bot_api_when_configured():
    sender = FakeSender()
    a = Alerter(token="123:ABC", chat_id="-100500", sender=sender)
    assert await a.alert("юзербот разлогинен") is True
    token, chat_id, text = sender.calls[0]
    assert (token, chat_id) == ("123:ABC", "-100500")
    assert "юзербот разлогинен" in text


async def test_without_token_alert_is_console_only(caplog):
    sender = FakeSender()
    a = Alerter(token=None, chat_id=None, sender=sender)
    with caplog.at_level(logging.ERROR):
        assert await a.alert("ВК-токен протух") is False
    assert sender.calls == []
    assert "ВК-токен протух" in caplog.text          # в консоль ушло всё равно


async def test_network_failure_never_breaks_the_pipeline(caplog):
    a = Alerter(token="123:ABC", chat_id="-100500", sender=FakeSender(result=OSError("нет сети")))
    assert await a.alert("очередь переполнена") is False


async def test_long_alert_is_trimmed_to_telegram_limit():
    sender = FakeSender()
    a = Alerter(token="123:ABC", chat_id="-1", sender=sender)
    await a.alert("x" * 5000)
    assert len(sender.calls[0][2]) <= 4096


async def test_alert_is_prefixed_so_it_is_findable_in_chat():
    sender = FakeSender()
    await Alerter(token="t", chat_id="c", sender=sender).alert("источник молчит 24 ч")
    assert sender.calls[0][2].startswith("tgtraff")


async def test_repeated_alert_is_not_spammed():
    """Разлогин повторяется каждым тиком — чат не должен превратиться в лог."""
    sender = FakeSender()
    a = Alerter(token="t", chat_id="c", sender=sender, repeat_after_sec=3600)
    await a.alert("юзербот разлогинен")
    await a.alert("юзербот разлогинен")
    assert len(sender.calls) == 1


async def test_different_alerts_are_not_suppressed():
    sender = FakeSender()
    a = Alerter(token="t", chat_id="c", sender=sender, repeat_after_sec=3600)
    await a.alert("юзербот разлогинен")
    await a.alert("ВК-токен протух")
    assert len(sender.calls) == 2


def test_alerter_from_env_is_disabled_without_variables(monkeypatch):
    for var in ("ADMIN_BOT_TOKEN", "BOT_TOKEN_ADMIN", "ADMIN_ALERT_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    assert Alerter.from_env().enabled is False


def test_alerter_from_env_picks_up_configured_bot(monkeypatch):
    monkeypatch.setenv("ADMIN_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("ADMIN_ALERT_CHAT_ID", "-100500")
    assert Alerter.from_env().enabled is True


async def test_moderator_list_is_used_when_no_dedicated_alert_chat(monkeypatch):
    """Фаза 5: адрес алертов — первый модератор из ADMIN_CHAT_IDS, отдельная
    переменная больше не обязательна."""
    monkeypatch.delenv("ADMIN_ALERT_CHAT_ID", raising=False)
    monkeypatch.setenv("BOT_TOKEN_ADMIN", "123:ABC")
    monkeypatch.setenv("ADMIN_CHAT_IDS", "777, 888")
    a = Alerter.from_env()
    assert a.enabled and a.chat_id == "777"


async def test_dedicated_alert_chat_wins(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN_ADMIN", "123:ABC")
    monkeypatch.setenv("ADMIN_CHAT_IDS", "777")
    monkeypatch.setenv("ADMIN_ALERT_CHAT_ID", "-100500")
    assert Alerter.from_env().chat_id == "-100500"
