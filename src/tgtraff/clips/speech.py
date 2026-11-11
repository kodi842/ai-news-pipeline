"""Речь в клипе (VIDEO-SPEC, фаза V1): Whisper-субтитры и TTS-озвучка.

Два внешних инструмента, и оба необязательные:
  * Whisper (faster-whisper, иначе openai-whisper) снимает субтитры с
    ОРИГИНАЛЬНОЙ дорожки видео — «только если есть речь»;
  * edge-tts озвучивает сценарий в режиме фото, голос один на сеть.

Ни один не имеет права уронить проход: нет модели, нет сети, битый файл —
клип собирается без субтитров или без голоса. Чего в этом случае НЕ
происходит: музыка вместо голоса (жёсткое правило 1 спеки).

Озвучка синтезируется ПО СТРОКАМ, а не одним куском: так длительность каждой
строки известна точно, и субтитры совпадают с озвучкой секунда в секунду —
это критерий приёмки V1, и держать его надо конструкцией, а не удачей.

Whisper на шумной дорожке без речи уверенно выдумывает текст («Субтитры
сделал…», «Продолжение следует») и зацикливает фразы. Такая строка на клипе
читается как чужая подпись, поэтому фильтр галлюцинаций здесь обязателен.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from tgtraff.clips.settings import ClipSettings

log = logging.getLogger("tgtraff.clips.speech")

# Типовые галлюцинации Whisper на русском: приходят на шуме и в тишине.
_HALLUCINATIONS = (
    r"субтитр\w*", r"продолжение следует", r"спасибо за просмотр",
    r"подписывайтесь", r"редактор субтитров", r"переводчик",
    r"добро пожаловать на канал", r"не забудьте поставить лайк",
    r"^\W*$",
)
_HALLUCINATION_RE = re.compile("|".join(_HALLUCINATIONS), re.I | re.U)


@dataclass(frozen=True)
class Segment:
    """Реплика с таймингами (Whisper) — она же интервал субтитра."""

    text: str
    start: float
    end: float


@dataclass(frozen=True)
class SpeechPart:
    """Озвученная строка сценария: файл и его длительность."""

    text: str
    path: Path
    duration: float


# ---------- разбор и чистка распознанного ----------

def clean_segments(segments: list[Segment]) -> list[Segment]:
    """Убирает галлюцинации и зацикленные повторы."""
    out: list[Segment] = []
    seen: set[str] = set()
    for seg in segments:
        text = " ".join((seg.text or "").split())
        if not text or _HALLUCINATION_RE.fullmatch(text.strip(" .!?…")) \
                or _HALLUCINATION_RE.match(text.lower()):
            continue
        key = text.lower().strip(" .!?…")
        if key in seen:                            # Whisper зациклился на фразе
            continue
        seen.add(key)
        out.append(Segment(text=text, start=seg.start, end=seg.end))
    return out


def has_speech(segments: list[Segment], min_chars: int) -> bool:
    """Есть ли в дорожке речь. Спека: субтитры — «только если есть речь»."""
    return sum(len(s.text) for s in segments) >= min_chars


def within(segments: list[Segment], start: float, end: float) -> list[Segment]:
    """Реплики, попадающие в окно клипа; выходящая за конец — подрезается."""
    out: list[Segment] = []
    for seg in segments:
        if seg.end <= start or seg.start >= end:
            continue
        out.append(Segment(text=seg.text, start=max(seg.start - start, 0.0),
                           end=min(seg.end, end) - start if start else min(seg.end, end)))
    return out


# ---------- Whisper ----------

class Transcriber:
    """Субтитры с оригинальной дорожки. Пустой список — речи нет или Whisper
    недоступен; и то, и другое означает «клип без субтитров»."""

    def __init__(self, settings: ClipSettings | None = None, model=None):
        self.settings = settings or ClipSettings()
        self._model = model
        self._loaded = model is not None

    def transcribe(self, path) -> list[Segment]:
        if not self.settings.whisper_enabled:
            return []
        model = self._ensure()
        if model is None:
            return []
        try:
            segments = _transcribe(model, Path(path), self.settings)
        except Exception as exc:                   # noqa: BLE001 — клип важнее субтитров
            log.warning("распознавание речи не прошло (%s) — клип будет без субтитров", exc)
            return []
        cleaned = clean_segments(segments)
        if not has_speech(cleaned, self.settings.speech_min_chars):
            return []
        return cleaned

    def _ensure(self):
        if not self._loaded:
            self._model = _load_whisper(self.settings)
            self._loaded = True
        return self._model


def _load_whisper(settings: ClipSettings):
    """faster-whisper предпочтительнее: на CPU он в разы быстрее, а модель та
    же. openai-whisper — запасной путь (в образе уже стоит torch)."""
    try:
        from faster_whisper import WhisperModel

        return WhisperModel(settings.whisper_model, device="cpu",
                            compute_type=settings.whisper_compute,
                            download_root=settings.whisper_dir,
                            local_files_only=not settings.whisper_allow_download)
    except Exception as exc:                       # noqa: BLE001
        log.info("faster-whisper недоступен (%s) — пробуем openai-whisper", exc)
    try:
        import whisper

        return whisper.load_model(settings.whisper_model, device="cpu",
                                  download_root=settings.whisper_dir)
    except Exception as exc:                       # noqa: BLE001
        log.warning("Whisper недоступен (%s) — клипы с видео пойдут без субтитров", exc)
        return None


def _transcribe(model, path: Path, settings: ClipSettings) -> list[Segment]:
    """Оба бэкенда отвечают по-разному, наружу — один вид."""
    if hasattr(model, "transcribe") and type(model).__name__ == "WhisperModel":
        segments, _info = model.transcribe(
            str(path), language=settings.whisper_language, vad_filter=True,
            condition_on_previous_text=False)
        return [Segment(text=s.text, start=float(s.start), end=float(s.end))
                for s in segments]
    result = model.transcribe(str(path), language=settings.whisper_language,
                              condition_on_previous_text=False, fp16=False)
    return [Segment(text=s.get("text", ""), start=float(s.get("start", 0.0)),
                    end=float(s.get("end", 0.0))) for s in result.get("segments", [])]


# ---------- TTS ----------

class Voice:
    """Озвучка сценария (edge-tts). `say` и `measure` подставляются в тестах."""

    def __init__(self, settings: ClipSettings | None = None, say=None, measure=None):
        self.settings = settings or ClipSettings()
        self._say = say or _edge_say
        self._measure = measure or self._probe_duration

    def say_lines(self, lines, out_dir) -> list[SpeechPart] | None:
        """Озвучивает строки по одной. None — озвучки не будет (нет модуля,
        нет сети): слайдшоу выйдет молча, но НЕ с музыкой."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        parts: list[SpeechPart] = []
        for i, line in enumerate(lines):
            text = " ".join(str(line).split())
            if not text:
                continue
            path = out / f"voice{i}.mp3"
            if not self._say(text, path, self.settings.tts_voice, self.settings.tts_rate):
                log.warning("озвучка строки %s не получилась — клип пойдёт без голоса", i)
                return None
            duration = self._measure(path)
            if not duration:
                return None
            parts.append(SpeechPart(text=text, path=path, duration=duration))
        return parts or None

    def track(self, parts: list[SpeechPart], out_path, gap: float = 0.15) -> Path | None:
        """Одна дорожка из озвученных строк. None — склеить не удалось, и тогда
        клип идёт без голоса (но не с музыкой)."""
        return concat_parts(parts, out_path, self.settings, gap=gap)

    def _probe_duration(self, path: Path) -> float:
        from tgtraff.clips.probe import _run, parse_probe

        raw = _run([self.settings.ffprobe, "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(path)])
        info = parse_probe(raw)
        if info is not None and info.duration:
            return info.duration
        # У mp3 без видеопотока parse_probe вернёт None: длительность лежит
        # в format, и её достаточно.
        return _format_duration(raw)


def _edge_say(text: str, path: Path, voice: str, rate: str) -> bool:
    try:
        import edge_tts
    except ImportError:
        log.warning("edge-tts не установлен — озвучки не будет")
        return False
    try:
        asyncio.run(_edge_write(edge_tts, text, path, voice, rate))
    except Exception as exc:                       # noqa: BLE001 — сеть, голос, лимит
        log.warning("edge-tts не ответил (%s)", exc)
        return False
    return path.exists() and path.stat().st_size > 0


async def _edge_write(edge_tts, text: str, path: Path, voice: str, rate: str) -> None:
    await edge_tts.Communicate(text, voice, rate=rate).save(str(path))


def concat_parts(parts: list[SpeechPart], out_path, settings: ClipSettings,
                 gap: float = 0.15) -> Path | None:
    """Склеивает озвученные строки в одну дорожку с паузами между ними.

    Пауза не косметика: без неё фразы наезжают друг на друга, а субтитр
    сменяется раньше, чем дочитан."""
    import subprocess

    if not parts:
        return None
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [settings.ffmpeg, "-y", "-v", "error", "-nostdin"]
    for part in parts:
        argv += ["-i", str(part.path)]
    chain = []
    for i in range(len(parts)):
        chain.append(f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:"
                     f"channel_layouts=stereo{f',apad=pad_dur={gap}' if gap else ''}[a{i}]")
    graph = ";".join(chain) + ";" + "".join(f"[a{i}]" for i in range(len(parts))) \
        + f"concat=n={len(parts)}:v=0:a=1[a]"
    argv += ["-filter_complex", graph, "-map", "[a]", "-c:a", "aac",
             "-b:a", settings.audio_bitrate, str(out)]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("склейка озвучки не прошла (%s)", exc)
        return None
    if done.returncode != 0 or not out.exists():
        log.warning("склейка озвучки не прошла: %s", (done.stderr or "")[-200:])
        return None
    return out


def spans(parts: list[SpeechPart], gap: float = 0.15) -> list[tuple[str, float, float]]:
    """Интервалы строк в склеенной дорожке — тайминги субтитров."""
    out: list[tuple[str, float, float]] = []
    cursor = 0.0
    for part in parts:
        out.append((part.text, cursor, cursor + part.duration))
        cursor += part.duration + gap
    return out


def total_duration(parts: list[SpeechPart], gap: float = 0.15) -> float:
    if not parts:
        return 0.0
    return sum(p.duration for p in parts) + gap * (len(parts) - 1)


def _format_duration(raw: str | None) -> float:
    import json

    try:
        return float(json.loads(raw or "").get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0
