"""Сборщик клипа (VIDEO-SPEC, фаза V1): плашки + речь + ffmpeg.

Здесь сходятся оба режима спеки и правило про звук: у видео звук
ОРИГИНАЛЬНЫЙ, у слайдшоу — TTS, и третьего варианта не существует. Whisper
зовётся только там, где есть речь.
"""
import shutil
import subprocess

import pytest

from tgtraff.clips import store
from tgtraff.clips.builder import ClipBuilder
from tgtraff.clips.probe import VideoInfo
from tgtraff.clips.settings import ClipSettings
from tgtraff.clips.speech import Segment, SpeechPart
from tgtraff.db import Database

CITY = "magnitogorsk"
SET = ClipSettings()
HAS_FFMPEG = bool(shutil.which(SET.ffmpeg) and shutil.which(SET.ffprobe))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg не установлен")


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def clip_row(db, mode="video", hook="Автобус смял столб",
             lines=("Утром на Ленина столкнулись автобус и фура", "Движение перекрыто")):
    draft_id = db.conn.execute(
        "INSERT INTO drafts (city, title, body, category, safety_verdict, status)"
        " VALUES (?,?,?,?,'safe','published')",
        (CITY, "Заголовок", "Тело новости", "dtp")).lastrowid
    clip_id = store.save_clip(db, draft_id=draft_id, city=CITY, mode=mode, score=8,
                              hook=hook, lines=list(lines),
                              cta="Подробности — в канале\n@example_channel_4")
    return (store.get_clip(db, clip_id),
            db.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


class StubTranscriber:
    def __init__(self, segments=()):
        self.segments = list(segments)
        self.calls = []

    def transcribe(self, path):
        self.calls.append(str(path))
        return self.segments


class StubVoice:
    def __init__(self, parts=None, fail=False):
        self.parts, self.fail, self.calls = parts, fail, []

    def say_lines(self, lines, out_dir):
        self.calls.append(list(lines))
        if self.fail:
            return None
        from pathlib import Path

        made = []
        for i, line in enumerate(lines):
            path = Path(out_dir) / f"v{i}.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            made.append(SpeechPart(text=line, path=path, duration=4.0))
        return made

    def track(self, parts, out_path, gap=0.15):
        from pathlib import Path

        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return path


class FakeRunner:
    """Вместо ffmpeg: запоминает план и делает вид, что файл собрался."""

    def __init__(self):
        self.plans = []

    def __call__(self, plan, settings):
        from tgtraff.clips.render import RenderResult, assert_no_music
        from pathlib import Path

        assert_no_music(plan)                     # правило проверяется и здесь
        self.plans.append(plan)
        path = Path(plan.out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp4")
        return RenderResult(True, path=path, size=1024)


def builder(tmp_path, transcriber=None, voice=None, runner=None, probe=None, settings=None):
    return ClipBuilder(settings or ClipSettings(out_dir=str(tmp_path / "out"),
                                                work_dir=str(tmp_path / "work")),
                       transcriber=transcriber, voice=voice,
                       runner=runner or FakeRunner(),
                       probe=probe or (lambda p: VideoInfo(20.0, True, 1280, 720)))


def source_file(tmp_path, name="a.mp4"):
    path = tmp_path / name
    path.write_bytes(b"media")
    return str(path)


# ---------- режим видео ----------

def test_video_with_speech_gets_whisper_subtitles(db, tmp_path):
    clip, draft = clip_row(db)
    runner = FakeRunner()
    b = builder(tmp_path, transcriber=StubTranscriber([Segment("На месте работают спасатели",
                                                               1.0, 4.0)]),
                runner=runner)
    result = b.build(clip, draft, [source_file(tmp_path)], city_name="Магнитогорск")
    assert result.ok
    assert result.subtitles == "whisper"
    assert [c.text for c in runner.plans[0].subtitles] == ["На месте работают спасатели"]
    assert runner.plans[0].audio == "original"


def test_video_without_speech_has_no_subtitles(db, tmp_path):
    """Спека: субтитры — только если есть речь. Шум субтитров не рождает."""
    clip, draft = clip_row(db)
    runner = FakeRunner()
    b = builder(tmp_path, transcriber=StubTranscriber([]), runner=runner)
    result = b.build(clip, draft, [source_file(tmp_path)])
    assert result.subtitles == "none"
    assert runner.plans[0].subtitles == []
    assert runner.plans[0].audio == "original"


def test_silent_video_is_voiced_not_left_mute(db, tmp_path):
    """У видео без звуковой дорожки берём TTS — спека разрешает ровно два
    источника звука, и музыки среди них нет."""
    clip, draft = clip_row(db)
    voice, runner = StubVoice(), FakeRunner()
    b = builder(tmp_path, transcriber=StubTranscriber([]), voice=voice, runner=runner,
                probe=lambda p: VideoInfo(20.0, False, 1280, 720))
    result = b.build(clip, draft, [source_file(tmp_path)])
    assert result.ok
    assert runner.plans[0].audio == "voice"
    assert result.subtitles == "tts"
    assert voice.calls, "озвучку не позвали"


def test_whisper_is_not_called_for_a_silent_video(db, tmp_path):
    clip, draft = clip_row(db)
    transcriber = StubTranscriber([])
    b = builder(tmp_path, transcriber=transcriber, voice=StubVoice(),
                probe=lambda p: VideoInfo(20.0, False, 1280, 720))
    b.build(clip, draft, [source_file(tmp_path)])
    assert transcriber.calls == []


def test_unreadable_video_fails_cleanly(db, tmp_path):
    clip, draft = clip_row(db)
    b = builder(tmp_path, probe=lambda p: None)
    result = b.build(clip, draft, [source_file(tmp_path)])
    assert not result.ok
    assert "прочит" in result.error or "ffprobe" in result.error


# ---------- режим фото ----------

def test_slideshow_speaks_the_script_and_subtitles_match(db, tmp_path):
    """Критерий приёмки V1: субтитры совпадают с озвучкой."""
    clip, draft = clip_row(db, mode="photos")
    voice, runner = StubVoice(), FakeRunner()
    b = builder(tmp_path, voice=voice, runner=runner)
    result = b.build(clip, draft, [source_file(tmp_path, "1.jpg"),
                                   source_file(tmp_path, "2.jpg")])
    assert result.ok
    plan = runner.plans[0]
    assert plan.audio == "voice"
    spoken = voice.calls[0]
    assert [c.text for c in plan.subtitles] == spoken[1:]     # хук уже на плашке
    assert result.subtitles == "tts"


def test_slideshow_voice_starts_with_the_hook(db, tmp_path):
    clip, draft = clip_row(db, mode="photos")
    voice = StubVoice()
    b = builder(tmp_path, voice=voice)
    b.build(clip, draft, [source_file(tmp_path, "1.jpg"), source_file(tmp_path, "2.jpg")])
    assert voice.calls[0][0] == "Автобус смял столб"


def test_slideshow_without_tts_still_shows_the_facts(db, tmp_path):
    """edge-tts недоступен: клип выходит молча, но с текстом на экране —
    и уж точно не с музыкой вместо голоса."""
    clip, draft = clip_row(db, mode="photos")
    runner = FakeRunner()
    b = builder(tmp_path, voice=StubVoice(fail=True), runner=runner)
    result = b.build(clip, draft, [source_file(tmp_path, "1.jpg"),
                                   source_file(tmp_path, "2.jpg")])
    assert result.ok
    assert runner.plans[0].audio == "none"
    assert len(runner.plans[0].subtitles) == 2
    assert result.subtitles == "lines"


def test_no_files_is_an_error(db, tmp_path):
    clip, draft = clip_row(db, mode="photos")
    result = builder(tmp_path).build(clip, draft, [])
    assert not result.ok


# ---------- сквозная сборка ----------

@needs_ffmpeg
def test_end_to_end_video_clip(db, tmp_path):
    """Настоящий ffmpeg, настоящий вертикальный mp4 с плашками."""
    from tgtraff.clips import probe

    source = tmp_path / "source.mp4"
    subprocess.run([SET.ffmpeg, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=18",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=18",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)],
                   check=True, timeout=300)
    clip, draft = clip_row(db)
    b = ClipBuilder(ClipSettings(out_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work")),
                    transcriber=StubTranscriber([Segment("Работают спасатели", 2.0, 6.0)]),
                    voice=StubVoice())
    result = b.build(clip, draft, [str(source)], city_name="Магнитогорск",
                     username="example_channel_4")
    assert result.ok, result.error
    info = probe.probe_video(result.path)
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration == pytest.approx(19.0, abs=1.0)
    assert info.has_audio
    assert result.size < SET.max_file_bytes


@needs_ffmpeg
def test_end_to_end_slideshow(db, tmp_path):
    from tgtraff.clips import probe

    photos = []
    for i in range(3):
        path = tmp_path / f"p{i}.png"
        subprocess.run([SET.ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "testsrc=size=900x600:rate=1:duration=1", "-frames:v", "1",
                        str(path)], check=True, timeout=120)
        photos.append(str(path))
    clip, draft = clip_row(db, mode="photos")
    b = ClipBuilder(ClipSettings(out_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work")),
                    transcriber=StubTranscriber([]), voice=StubVoice(fail=True))
    result = b.build(clip, draft, photos, city_name="Магнитогорск",
                     username="example_channel_4")
    assert result.ok, result.error
    info = probe.probe_video(result.path)
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration >= SET.min_seconds - 1
