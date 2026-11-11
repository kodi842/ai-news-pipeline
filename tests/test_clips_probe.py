"""ffprobe-обвязка: длительность, звук, размеры (VIDEO-SPEC, фаза V1).

Разбор ответа проверяется на данных (чистая функция), работа с настоящим
ffmpeg — отдельным тестом, который пропускается там, где ffmpeg не установлен
(в образ он добавлен, на рабочей машине может не быть).
"""
import json
import shutil
import subprocess

import pytest

from tgtraff.clips import probe as probe_mod
from tgtraff.clips.settings import ClipSettings

HAS_FFMPEG = bool(shutil.which(ClipSettings().ffmpeg) and shutil.which(ClipSettings().ffprobe))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe не установлены")


def payload(duration="21.5", streams=None):
    return json.dumps({
        "format": {"duration": duration},
        "streams": streams if streams is not None else [
            {"codec_type": "video", "width": 1280, "height": 720, "duration": duration},
            {"codec_type": "audio", "channels": 2},
        ],
    })


def test_parses_duration_audio_and_size():
    info = probe_mod.parse_probe(payload())
    assert info.duration == pytest.approx(21.5)
    assert info.has_audio is True
    assert (info.width, info.height) == (1280, 720)


def test_video_without_audio_stream():
    info = probe_mod.parse_probe(payload(streams=[
        {"codec_type": "video", "width": 720, "height": 1280}]))
    assert info.has_audio is False
    assert info.is_vertical is True


def test_duration_falls_back_to_stream_when_format_is_empty():
    """У ТГ-видео формат иногда без длительности, а поток её знает."""
    info = probe_mod.parse_probe(json.dumps({
        "format": {}, "streams": [{"codec_type": "video", "width": 640, "height": 480,
                                   "duration": "9.9"}]}))
    assert info.duration == pytest.approx(9.9)


def test_broken_answer_gives_none():
    assert probe_mod.parse_probe("не json") is None
    assert probe_mod.parse_probe(json.dumps({"streams": []})) is None


@needs_ffmpeg
def test_probes_real_file(tmp_path):
    """Живой ffprobe на файле, который сами и сделали: 2 секунды, со звуком."""
    path = tmp_path / "a.mp4"
    subprocess.run([ClipSettings().ffmpeg, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)],
                   check=True, timeout=120)
    info = probe_mod.probe_video(path)
    assert info is not None
    assert info.duration == pytest.approx(2.0, abs=0.3)
    assert info.has_audio is True
    assert (info.width, info.height) == (320, 240)


@needs_ffmpeg
def test_grabs_frame_for_watermark_check(tmp_path):
    """Кадр из видео — то, что уходит в vision-детектор чужих знаков."""
    video = tmp_path / "a.mp4"
    subprocess.run([ClipSettings().ffmpeg, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
                    "-c:v", "libx264", str(video)], check=True, timeout=120)
    frame = probe_mod.grab_frame(video, at=1.0, out_dir=tmp_path)
    assert frame is not None and frame.exists() and frame.stat().st_size > 0


def test_missing_file_probes_to_none(tmp_path):
    assert probe_mod.probe_video(tmp_path / "нет.mp4") is None


def test_probe_never_raises_when_ffprobe_is_absent(tmp_path):
    """Нет ffprobe в системе — завод должен сказать это внятно, а не упасть."""
    path = tmp_path / "a.mp4"
    path.write_bytes(b"not a video")
    assert probe_mod.probe_video(path, ffprobe="ffprobe-которого-нет") is None
