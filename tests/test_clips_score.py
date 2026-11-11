"""Клип-скоринг (VIDEO-SPEC): виральный потенциал новости 1-10.

Модель подставная — проверяется контракт вокруг неё: что уходит в промпт (у
модели должно быть чем решать: категория, что за медиа, длительность), как
разбирается ответ и что происходит с мусором.
"""
import pytest

from tgtraff.clips.media import ClipMedia
from tgtraff.clips.score import ClipScorer
from tgtraff.clips.settings import ClipSettings
from tgtraff.llm.client import LlmError


class StubClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def ask_json(self, system, user, **kw):
        self.calls.append({"system": system, "user": user, **kw})
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply

        class _R:
            model = "stub"
            cost_usd = 0.0007
        return reply, _R()


def draft(title="Автобус смял столб на Ленина", body="Столкновение произошло утром.",
          category="dtp", city="magnitogorsk"):
    return {"id": 7, "city": city, "title": title, "body": body, "category": category,
            "district": "Ленинский", "urgency": "normal"}


VIDEO = ClipMedia(mode="video", kind="video", files=["/m/a.mp4"], media_ids=[1],
                  source_key="example_channel_5", reason="clean", duration=27.0, has_audio=True)
PHOTOS = ClipMedia(mode="photos", kind="photo", files=["/m/1.jpg", "/m/2.jpg"],
                   media_ids=[1, 2], source_key="example_channel_5", reason="clean")


def test_score_is_parsed():
    client = StubClient({"score": 8, "reason": "живое видео, узнаваемое место"})
    result = ClipScorer(client, ClipSettings()).score(draft(), VIDEO, city_name="Магнитогорск")
    assert result.score == 8
    assert "видео" in result.reason
    assert result.cost_usd == pytest.approx(0.0007)


def test_prompt_tells_the_model_what_media_we_have():
    """Без этого модель оценивает текст, а не клип: живое видео и слайдшоу
    из двух фото — совершенно разный виральный потенциал."""
    client = StubClient({"score": 5, "reason": "ок"})
    ClipScorer(client, ClipSettings()).score(draft(), VIDEO, city_name="Магнитогорск")
    user = client.calls[0]["user"]
    assert "видео" in user.lower()
    assert "27" in user                                   # длительность
    assert "Магнитогорск" in user
    assert "dtp" in user or "ДТП" in user


def test_photo_mode_is_visible_in_prompt():
    client = StubClient({"score": 4, "reason": "ок"})
    ClipScorer(client, ClipSettings()).score(draft(), PHOTOS, city_name="Магнитогорск")
    assert "фото" in client.calls[0]["user"].lower()


def test_score_is_clamped_to_the_scale():
    client = StubClient({"score": 42, "reason": "восторг"})
    assert ClipScorer(client, ClipSettings()).score(draft(), VIDEO).score == 10
    client = StubClient({"score": -3, "reason": "тоска"})
    assert ClipScorer(client, ClipSettings()).score(draft(), VIDEO).score == 1


def test_score_as_string_is_accepted():
    """Модель нередко возвращает число строкой — это не повод терять кандидата."""
    client = StubClient({"score": "7", "reason": "ок"})
    assert ClipScorer(client, ClipSettings()).score(draft(), VIDEO).score == 7


def test_answer_without_score_is_an_error():
    client = StubClient({"reason": "забыл оценку"})
    with pytest.raises(LlmError):
        ClipScorer(client, ClipSettings()).score(draft(), VIDEO)


def test_scoring_uses_its_own_purpose_for_accounting():
    """Деньги завода должны быть видны отдельно от рерайта постов."""
    client = StubClient({"score": 6, "reason": "ок"})
    ClipScorer(client, ClipSettings()).score(draft(), VIDEO)
    assert client.calls[0]["purpose"] == "clip_score"


def test_favorite_categories_are_named_in_the_prompt():
    """Спека называет фаворитов (dtp/fire/weather/city) — модель должна знать
    приоритеты сетки, а не угадывать их."""
    client = StubClient({"score": 6, "reason": "ок"})
    settings = ClipSettings(favorite_categories=("dtp", "fire"))
    ClipScorer(client, settings).score(draft(), VIDEO)
    system = client.calls[0]["system"]
    assert "dtp" in system and "fire" in system


def test_passes_returns_whether_threshold_is_met():
    client = StubClient({"score": 6, "reason": "ок"})
    result = ClipScorer(client, ClipSettings(min_score=7)).score(draft(), VIDEO)
    assert result.passes(7) is False
    assert result.passes(6) is True
