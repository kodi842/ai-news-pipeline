"""Session-файл юзербота — SQLite, и его держит один процесс.
Прогрев и коллектор работают параллельно несколько суток, поэтому конфликт нужно
ловить понятным сообщением, а не трейсбеком sqlite3."""
import sqlite3

import pytest

from tgtraff.app import session_is_busy


@pytest.fixture
def session(tmp_path):
    p = tmp_path / "userbot.session"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE sessions (dc_id integer primary key, auth_key blob)")
    conn.commit()
    conn.close()
    return p


def test_free_session_is_not_busy(session):
    assert session_is_busy(session) is False


def test_missing_session_is_not_busy(tmp_path):
    """Первого входа ещё не было — это не «занято», это «нет сессии»."""
    assert session_is_busy(tmp_path / "нет-такого.session") is False


def test_session_held_by_another_process_is_busy(session):
    holder = sqlite3.connect(session, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert session_is_busy(session) is True
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_session_is_free_again_after_holder_releases(session):
    holder = sqlite3.connect(session, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("ROLLBACK")
    holder.close()
    assert session_is_busy(session) is False
