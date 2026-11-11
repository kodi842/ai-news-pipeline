"""ffprobe/ffmpeg: что мы знаем о файле до сборки (VIDEO-SPEC, фаза V1).

Три вопроса, на которые отвечает модуль:
  * сколько идёт видео — от этого зависит, кандидат ли оно вообще (≥8 с) и
    как его резать (15-40 с);
  * есть ли звуковая дорожка — от этого зависит, звать ли Whisper: субтитры
    делаются «только если есть речь» (спека);
  * какой кадр показать детектору чужих знаков — vision-модель видео не
    смотрит, поэтому из видео вынимается кадр.

Ни одна ошибка внешнего процесса не имеет права ронять проход завода: нет
ffprobe, битый файл, таймаут — возвращается None, и клип просто не делается.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("tgtraff.clips.probe")

PROBE_TIMEOUT_SEC = 60


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    has_audio: bool
    width: int
    height: int

    @property
    def is_vertical(self) -> bool:
        return self.height >= self.width


def parse_probe(raw: str | None) -> VideoInfo | None:
    """Ответ `ffprobe -print_format json` → VideoInfo. None — разобрать нечего."""
    try:
        data = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    duration = _float(data.get("format", {}).get("duration")) or _float(video.get("duration"))
    return VideoInfo(
        duration=duration or 0.0,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
    )


def probe_video(path: str | Path, ffprobe: str = "ffprobe") -> VideoInfo | None:
    path = Path(path)
    if not path.exists():
        return None
    out = _run([ffprobe, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path)])
    return parse_probe(out)


def grab_frame(path: str | Path, at: float = 1.0, out_dir: str | Path = "data/clips/work",
               ffmpeg: str = "ffmpeg", name: str | None = None) -> Path | None:
    """Один кадр из видео в JPEG — материал для детектора чужих знаков.

    Кадр берётся не с нулевой секунды: первый кадр часто чёрный, а знак
    площадки появляется чуть позже."""
    path = Path(path)
    if not path.exists():
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = out_dir / (name or f"{path.stem}-frame.jpg")
    ok = _run([ffmpeg, "-y", "-v", "error", "-ss", f"{max(at, 0):.2f}", "-i", str(path),
               "-frames:v", "1", "-q:v", "3", str(frame)]) is not None
    if not ok or not frame.exists() or frame.stat().st_size == 0:
        return None
    return frame


def _run(cmd: list[str]) -> str | None:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_SEC, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s не запустился (%s) — файл остаётся непроверенным", cmd[0], exc)
        return None
    if done.returncode != 0:
        log.debug("%s вернул %s: %s", cmd[0], done.returncode, (done.stderr or "")[:200])
        return None
    return done.stdout


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
