"""Речь в клипе (VIDEO-SPEC, фаза V1): Whisper-субтитры и TTS-озвучка.

Два внешних инструмента, и оба могут отсутствовать: Whisper — тяжёлая модель,
edge-tts ходит в сеть. Ни один не имеет права уронить проход завода — без них
клип просто собирается без субтитров или без голоса (но НИКОГДА с музыкой
вместо голоса).

Отдельно проверяется фильтр галлюцинаций Whisper: на шуме без речи модель
уверенно выдаёт «Субтитры сделал…» и «Продолжение следует», и такой текст,
попав на клип, выглядит как чужая подпись.
"""
import os

import pytest

from tgtraff.clips import speech
from tgtraff.clips.settings import ClipSettings

SET = ClipSettings()
LIVE_TTS = os.getenv("TGTRAFF_LIVE_TTS") == "1"


# ---------- Whisper: сегменты в субтитры ----------

def test_segments_become_captions():
    segments = [speech.Segment("Утром на Ленина", 0.5, 3.2),
                speech.Segment("движение перекрыто", 3.4, 6.0)]
    assert speech.has_speech(segments, min_chars=12) is True
    assert [s.text for s in segments] == ["Утром на Ленина", "движение перекрыто"]


def test_short_noise_is_not_speech():
    """«Ага» на 40 секунд шума — не речь: субтитров такому видео не положено."""
    assert speech.has_speech([speech.Segment("Ага", 1.0, 1.4)], min_chars=12) is False
    assert speech.has_speech([], min_chars=12) is False


HALLUCINATIONS = [
    "Субтитры сделал DimaTorzok",
    "Продолжение следует...",
    "Субтитры создавал DimaTorzok",
    "Редактор субтитров А.Синецкая",
    "Спасибо за просмотр!",
    "Подписывайтесь на канал",
]


@pytest.mark.parametrize("text", HALLUCINATIONS)
def test_whisper_hallucinations_are_dropped(text):
    """На шуме Whisper выдаёт это уверенно. На клипе такая строка читается
    как чужая подпись — то есть ровно то, чего мы избегаем."""
    assert speech.clean_segments([speech.Segment(text, 0.0, 3.0)]) == []


def test_real_speech_survives_the_filter():
    segments = [speech.Segment("На месте работают спасатели", 0.0, 3.0)]
    assert speech.clean_segments(segments) == segments


def test_repeated_segment_is_collapsed():
    """Зацикливание — вторая частая беда Whisper на шумной дорожке."""
    segments = [speech.Segment("Пожар потушен", 0.0, 2.0),
                speech.Segment("Пожар потушен", 2.0, 4.0),
                speech.Segment("Пожар потушен", 4.0, 6.0)]
    assert len(speech.clean_segments(segments)) == 1


def test_segments_are_limited_to_the_clip_window():
    segments = [speech.Segment("Первая", 1.0, 4.0), speech.Segment("Вторая", 41.0, 44.0)]
    assert [s.text for s in speech.within(segments, 0.0, 40.0)] == ["Первая"]


def test_segment_crossing_the_end_is_trimmed():
    segments = [speech.Segment("Длинная фраза", 38.0, 46.0)]
    trimmed = speech.within(segments, 0.0, 40.0)
    assert trimmed and trimmed[0].end == pytest.approx(40.0)


# ---------- Whisper: недоступность ----------

def test_missing_whisper_backend_gives_no_subtitles(monkeypatch, tmp_path):
    monkeypatch.setattr(speech, "_load_whisper", lambda settings: None)
    path = tmp_path / "a.mp4"
    path.write_bytes(b"video")
    assert speech.Transcriber(SET).transcribe(path) == []


def test_transcriber_survives_a_broken_model(monkeypatch, tmp_path):
    class Boom:
        def transcribe(self, *a, **kw):
            raise RuntimeError("модель не загрузилась")

    monkeypatch.setattr(speech, "_load_whisper", lambda settings: Boom())
    path = tmp_path / "a.mp4"
    path.write_bytes(b"video")
    assert speech.Transcriber(SET).transcribe(path) == []


def test_transcription_is_disabled_by_config(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(speech, "_load_whisper", lambda settings: called.append(1))
    path = tmp_path / "a.mp4"
    path.write_bytes(b"video")
    assert speech.Transcriber(ClipSettings(whisper_enabled=False)).transcribe(path) == []
    assert called == []


# ---------- TTS ----------

def test_voice_lines_are_synthesized_one_by_one(tmp_path):
    """Каждая строка — свой файл: только так субтитры совпадают с озвучкой
    секунда в секунду (критерий приёмки V1)."""
    made = []

    def fake_say(text, path, voice, rate):
        made.append((text, voice))
        path.write_bytes(b"audio")
        return True

    parts = speech.Voice(SET, say=fake_say, measure=lambda p: 3.0).say_lines(
        ["Первая строка", "Вторая строка"], out_dir=tmp_path)
    assert [t for t, _ in made] == ["Первая строка", "Вторая строка"]
    assert [p.duration for p in parts] == [3.0, 3.0]
    assert all(p.path.exists() for p in parts)


def test_one_voice_for_the_whole_network(tmp_path):
    """Спека: «голос один на сеть» — он приходит из конфига, а не выбирается
    случайно на каждый клип."""
    used = []

    def fake_say(text, path, voice, rate):
        used.append(voice)
        path.write_bytes(b"audio")
        return True

    settings = ClipSettings(tts_voice="ru-RU-DmitryNeural")
    speech.Voice(settings, say=fake_say, measure=lambda p: 2.0).say_lines(
        ["Раз", "Два"], out_dir=tmp_path)
    assert set(used) == {"ru-RU-DmitryNeural"}


def test_unavailable_tts_gives_none_not_a_crash(tmp_path):
    def broken_say(text, path, voice, rate):
        return False

    assert speech.Voice(SET, say=broken_say).say_lines(["Раз"], out_dir=tmp_path) is None


def test_captions_follow_the_voice_timing(tmp_path):
    parts = [speech.SpeechPart("Первая", tmp_path / "a.mp3", 3.0),
             speech.SpeechPart("Вторая", tmp_path / "b.mp3", 4.0)]
    spans = speech.spans(parts, gap=0.15)
    assert spans[0] == ("Первая", pytest.approx(0.0), pytest.approx(3.0))
    assert spans[1] == ("Вторая", pytest.approx(3.15), pytest.approx(7.15))


def test_total_voice_duration_counts_gaps(tmp_path):
    parts = [speech.SpeechPart("Первая", tmp_path / "a.mp3", 3.0),
             speech.SpeechPart("Вторая", tmp_path / "b.mp3", 4.0)]
    assert speech.total_duration(parts, gap=0.15) == pytest.approx(7.15)


# ---------- живая проверка ----------

@pytest.mark.skipif(not LIVE_TTS, reason="живой edge-tts: TGTRAFF_LIVE_TTS=1")
def test_live_edge_tts_speaks_russian(tmp_path):
    voice = speech.Voice(SET)
    parts = voice.say_lines(["Утром на Ленина столкнулись автобус и фура"], out_dir=tmp_path)
    assert parts, "edge-tts не отдал файл"
    assert parts[0].duration > 1.0
    assert parts[0].path.stat().st_size > 1000
