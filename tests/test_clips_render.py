"""Сборка клипа ffmpeg (VIDEO-SPEC, фаза V1) и жёсткое правило 1: НИКАКОЙ МУЗЫКИ.

«Никакой музыкальной подложки. Только оригинальный звук видео или TTS-озвучка.
Музыка = Content ID, жалобы правообладателей, бан клипа.»

Поэтому план сборки — данные, а не строка команды: каждый вход у него с ролью
(кадр кластера / плашка / озвучка), и правило проверяется не глазами в code
review, а функцией, которая зовётся перед КАЖДЫМ запуском ffmpeg.

Ещё здесь проверяется геометрия (1080×1920), длительность (15-40 с, спека),
пресет и битрейт, аутро с CTA и то, что субтитры в режиме фото совпадают
с озвучкой (критерий приёмки V1).
"""
import shutil
import subprocess

import pytest

from tgtraff.clips import render
from tgtraff.clips.settings import ClipSettings

SET = ClipSettings()
HAS_FFMPEG = bool(shutil.which(SET.ffmpeg) and shutil.which(SET.ffprobe))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg не установлен")

HOOK = "Автобус смял столб"
LINES = ["Утром на Ленина столкнулись автобус и фура", "Движение перекрыто"]
CTA = "Подробности — в нашем канале\n@example_channel_4"


def plates(tmp_path):
    """Плашки как их отдаёт overlay.py — тестам достаточно путей."""
    out = {}
    for name in ("hook", "badge", "outro", "sub0", "sub1"):
        path = tmp_path / f"{name}.png"
        path.write_bytes(b"png")
        out[name] = path
    return out


def video_plan(tmp_path, has_audio=True, subtitles=(), duration=27.0):
    p = plates(tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    return render.plan_video(
        source=source, out_path=tmp_path / "clip.mp4", settings=SET,
        source_duration=duration, source_size=(1280, 720), has_audio=has_audio,
        hook=p["hook"], badge=p["badge"], outro=p["outro"],
        subtitles=[render.Caption(text="Раз", path=p["sub0"], start=1.0, end=4.0),
                   *subtitles],
        voice=None)


def photos_plan(tmp_path, n=3):
    p = plates(tmp_path)
    photos = []
    for i in range(n):
        path = tmp_path / f"photo{i}.jpg"
        path.write_bytes(b"jpg")
        photos.append(path)
    voice = tmp_path / "voice.m4a"
    voice.write_bytes(b"audio")
    return render.plan_photos(
        photos=photos, out_path=tmp_path / "clip.mp4", settings=SET,
        hook=p["hook"], badge=p["badge"], outro=p["outro"],
        subtitles=[render.Caption(text=LINES[0], path=p["sub0"], start=0.0, end=6.0),
                   render.Caption(text=LINES[1], path=p["sub1"], start=6.0, end=12.0)],
        voice=voice, voice_duration=12.0)


# ---------- жёсткое правило 1: без музыки ----------

def test_video_plan_keeps_original_audio_only(tmp_path):
    plan = video_plan(tmp_path)
    assert plan.audio == "original"
    assert [s.role for s in plan.sources].count("voice") == 0


def test_photo_plan_has_exactly_one_voice_track(tmp_path):
    plan = photos_plan(tmp_path)
    assert plan.audio == "voice"
    assert [s.role for s in plan.sources].count("voice") == 1


def test_every_input_is_a_cluster_frame_a_plate_or_the_voice(tmp_path):
    """Ни одного постороннего файла на входе: музыке просто негде появиться."""
    for plan in (video_plan(tmp_path), photos_plan(tmp_path)):
        assert {s.role for s in plan.sources} <= {"media", "plate", "voice"}


def test_no_music_check_passes_for_both_modes(tmp_path):
    render.assert_no_music(video_plan(tmp_path))
    render.assert_no_music(photos_plan(tmp_path))


def test_no_music_check_catches_a_second_audio_track(tmp_path):
    """Так выглядела бы подложка: второй звуковой вход рядом с оригиналом."""
    plan = video_plan(tmp_path)
    music = tmp_path / "trend_sound.mp3"
    music.write_bytes(b"audio")
    spoiled = render.RenderPlan(
        mode=plan.mode, out_path=plan.out_path, duration=plan.duration,
        sources=[*plan.sources, render.Source(str(music), "voice")],
        argv=[*plan.argv, "-i", str(music)], audio=plan.audio, subtitles=plan.subtitles)
    with pytest.raises(render.MusicForbidden):
        render.assert_no_music(spoiled)


def test_no_music_check_catches_audio_mixing(tmp_path):
    """amix/amerge — единственный способ подмешать звук поверх оригинала."""
    plan = video_plan(tmp_path)
    spoiled = render.RenderPlan(
        mode=plan.mode, out_path=plan.out_path, duration=plan.duration,
        sources=plan.sources, argv=[*plan.argv, "-filter_complex", "[0:a][1:a]amix=inputs=2[a]"],
        audio=plan.audio, subtitles=plan.subtitles)
    with pytest.raises(render.MusicForbidden):
        render.assert_no_music(spoiled)


def test_no_music_check_catches_unknown_input_role(tmp_path):
    plan = video_plan(tmp_path)
    spoiled = render.RenderPlan(
        mode=plan.mode, out_path=plan.out_path, duration=plan.duration,
        sources=[*plan.sources, render.Source("/sounds/trend.mp3", "music")],
        argv=plan.argv, audio=plan.audio, subtitles=plan.subtitles)
    with pytest.raises(render.MusicForbidden):
        render.assert_no_music(spoiled)


def test_render_refuses_to_run_a_plan_with_music(tmp_path, monkeypatch):
    """Проверка стоит ПЕРЕД запуском: с музыкой ffmpeg не зовётся вообще."""
    called = []
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **kw: called.append(a))
    plan = video_plan(tmp_path)
    spoiled = render.RenderPlan(
        mode=plan.mode, out_path=plan.out_path, duration=plan.duration,
        sources=[*plan.sources, render.Source("/sounds/x.mp3", "music")],
        argv=plan.argv, audio=plan.audio, subtitles=plan.subtitles)
    with pytest.raises(render.MusicForbidden):
        render.run(spoiled, SET)
    assert called == []


# ---------- геометрия и длительность ----------

def test_output_is_vertical_1080x1920(tmp_path):
    for plan in (video_plan(tmp_path), photos_plan(tmp_path)):
        graph = _filter_graph(plan)
        assert "1080:1920" in graph or "1080x1920" in graph


def test_horizontal_video_gets_blurred_padding_not_a_deep_crop(tmp_path):
    """Кроп горизонтального кадра в 9:16 выкидывает три четверти картинки —
    спека разрешает паддинг, и он тут размытым фоном."""
    plan = video_plan(tmp_path)
    assert "boxblur" in _filter_graph(plan) or "gblur" in _filter_graph(plan)


def test_vertical_source_is_cropped_without_blur(tmp_path):
    p = plates(tmp_path)
    source = tmp_path / "v.mp4"
    source.write_bytes(b"v")
    plan = render.plan_video(source=source, out_path=tmp_path / "c.mp4", settings=SET,
                             source_duration=20.0, source_size=(720, 1280), has_audio=True,
                             hook=p["hook"], badge=p["badge"], outro=p["outro"],
                             subtitles=[], voice=None)
    assert "boxblur" not in _filter_graph(plan)


def test_long_video_is_cut_to_max_seconds(tmp_path):
    plan = video_plan(tmp_path, duration=300.0)
    assert plan.duration == pytest.approx(SET.max_seconds)
    assert "-t" in plan.argv


def test_short_video_is_not_stretched(tmp_path):
    """Двенадцатисекундное видео остаётся двенадцатисекундным: минимум 15 с
    спеки — это цель, а не повод крутить пустой кадр."""
    plan = video_plan(tmp_path, duration=12.0)
    assert plan.duration == pytest.approx(12.0 + SET.outro_seconds)


def test_outro_is_appended_to_the_end(tmp_path):
    plan = video_plan(tmp_path, duration=20.0)
    assert plan.duration == pytest.approx(20.0 + SET.outro_seconds)
    assert "concat" in _filter_graph(plan)


def test_photo_slideshow_follows_the_voice_length(tmp_path):
    """Слайдшоу идёт столько, сколько говорит голос: оборванная на полуслове
    фраза и молчащий хвост одинаково плохи."""
    p = plates(tmp_path)
    photos = []
    for i in range(3):
        path = tmp_path / f"ph{i}.jpg"
        path.write_bytes(b"jpg")
        photos.append(path)
    voice = tmp_path / "voice.m4a"
    voice.write_bytes(b"audio")
    plan = render.plan_photos(photos=photos, out_path=tmp_path / "c.mp4", settings=SET,
                              hook=p["hook"], badge=p["badge"], outro=p["outro"],
                              subtitles=[], voice=voice, voice_duration=24.0)
    assert plan.duration == pytest.approx(24.6 + SET.outro_seconds, abs=0.2)


def test_short_voice_still_gives_a_clip_of_at_least_fifteen_seconds(tmp_path):
    """Длину слайдшоу задаём мы сами — значит, держим полосу спеки 15-40 с
    (клип короче в рекомендациях живёт хуже), а не обрываем на 13-й секунде."""
    plan = photos_plan(tmp_path, n=3)                # озвучка на 12 секунд
    assert plan.duration == pytest.approx(SET.min_seconds)


def test_photo_slideshow_uses_ken_burns(tmp_path):
    assert "zoompan" in _filter_graph(photos_plan(tmp_path))


def test_photo_is_fed_to_ken_burns_as_a_single_frame(tmp_path):
    """Ловушка zoompan: если кадр подать зациклённым (`-loop 1 -t N`), фильтр
    получит СОТНИ входных кадров и на каждый выдаст d выходных. Замер на живом
    фото: 12-секундное слайдшоу собиралось больше двух минут вместо трёх
    секунд. Длительность задаёт сам zoompan, а фото подаётся одним кадром.

    Зациклённый вход в плане ровно один — аутро (там zoompan'а нет)."""
    plan = photos_plan(tmp_path, n=3)
    assert plan.argv.count("-loop") == 1
    graph = _filter_graph(plan)
    assert "d=" in graph                       # длительность задаёт zoompan'у d


# ---------- параметры кодирования ----------

def test_encoder_settings_come_from_config(tmp_path):
    plan = video_plan(tmp_path)
    assert SET.preset in plan.argv
    assert SET.video_bitrate in plan.argv
    assert "libx264" in plan.argv
    assert "yuv420p" in " ".join(plan.argv)


def test_faststart_for_platform_upload(tmp_path):
    assert "+faststart" in plan_args(video_plan(tmp_path))


def test_overlays_are_time_limited(tmp_path):
    """Хук держится первые секунды, бейдж — весь клип, субтитр — свой интервал."""
    graph = _filter_graph(video_plan(tmp_path))
    assert "enable='between(t,0," in graph.replace(" ", "")
    assert "enable='between(t,1.0,4.0)'" in graph.replace(" ", "")


# ---------- субтитры = озвучка ----------

def test_photo_subtitles_are_the_spoken_lines(tmp_path):
    """Критерий приёмки V1: субтитры совпадают с озвучкой. В режиме фото это
    один и тот же текст, поэтому проверяется буквально."""
    plan = photos_plan(tmp_path)
    assert [c.text for c in plan.subtitles] == LINES
    assert plan.subtitles_source == "tts"


def test_video_subtitles_are_marked_as_whisper(tmp_path):
    plan = video_plan(tmp_path)
    assert plan.subtitles_source == "whisper"


def test_video_without_speech_has_no_subtitles(tmp_path):
    p = plates(tmp_path)
    source = tmp_path / "v.mp4"
    source.write_bytes(b"v")
    plan = render.plan_video(source=source, out_path=tmp_path / "c.mp4", settings=SET,
                             source_duration=20.0, source_size=(1280, 720), has_audio=True,
                             hook=p["hook"], badge=p["badge"], outro=p["outro"],
                             subtitles=[], voice=None)
    assert plan.subtitles_source == "none"


# ---------- живой ffmpeg ----------

@needs_ffmpeg
def test_builds_a_real_clip_from_a_real_video(tmp_path):
    """Сквозная проверка: настоящий ffmpeg, настоящий mp4 на выходе."""
    from tgtraff.clips import overlay, probe

    source = tmp_path / "source.mp4"
    subprocess.run([SET.ffmpeg, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=20",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=20",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)],
                   check=True, timeout=300)
    hook = overlay.hook_plate("magnitogorsk", HOOK, out_dir=tmp_path, settings=SET)
    badge = overlay.badge_plate("magnitogorsk", out_dir=tmp_path, settings=SET)
    outro = overlay.outro_frame("magnitogorsk", CTA, out_dir=tmp_path, settings=SET)
    sub = overlay.subtitle_plate("magnitogorsk", LINES[0], out_dir=tmp_path, settings=SET)

    plan = render.plan_video(
        source=source, out_path=tmp_path / "clip.mp4", settings=SET,
        source_duration=20.0, source_size=(640, 360), has_audio=True,
        hook=hook, badge=badge, outro=outro,
        subtitles=[render.Caption(text=LINES[0], path=sub, start=1.0, end=6.0)], voice=None)
    result = render.run(plan, SET)

    assert result.ok, result.error
    info = probe.probe_video(result.path)
    assert info is not None
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration == pytest.approx(21.0, abs=1.0)
    assert info.has_audio is True
    assert result.size <= SET.max_file_bytes


@needs_ffmpeg
def test_builds_a_real_slideshow_with_silence_instead_of_music(tmp_path):
    """Слайдшоу без озвучки (edge-tts недоступен) — тишина, но НЕ музыка."""
    from tgtraff.clips import overlay, probe

    photos = []
    for i in range(2):
        path = tmp_path / f"p{i}.png"
        subprocess.run([SET.ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                        "-i", f"testsrc=size=800x600:rate=1:duration=1",
                        "-frames:v", "1", str(path)], check=True, timeout=120)
        photos.append(path)
    hook = overlay.hook_plate("cherepovets", HOOK, out_dir=tmp_path, settings=SET)
    badge = overlay.badge_plate("cherepovets", out_dir=tmp_path, settings=SET)
    outro = overlay.outro_frame("cherepovets", CTA, out_dir=tmp_path, settings=SET)

    plan = render.plan_photos(photos=photos, out_path=tmp_path / "slides.mp4", settings=SET,
                              hook=hook, badge=badge, outro=outro, subtitles=[],
                              voice=None, voice_duration=None)
    result = render.run(plan, SET)
    assert result.ok, result.error
    info = probe.probe_video(result.path)
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration >= SET.min_seconds - 1


def plan_args(plan):
    return " ".join(plan.argv)


def _filter_graph(plan) -> str:
    argv = plan.argv
    return argv[argv.index("-filter_complex") + 1] if "-filter_complex" in argv else ""
