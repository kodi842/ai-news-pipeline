"""Сборка клипа ffmpeg (VIDEO-SPEC, фаза V1).

Два режима спеки:
  (а) есть видео — кроп/паддинг в 1080×1920, обрезка до 15-40 с, хук-плашка
      сверху, субтитры снизу (Whisper — только если есть речь), бейдж города,
      аутро 1 с с CTA и юзернеймом;
  (б) только фото — слайдшоу 2-5 кадров с Ken Burns (zoompan), TTS-озвучка,
      субтитры = озвучка.

ЖЁСТКОЕ ПРАВИЛО 1: никакой музыкальной подложки. Только оригинальный звук
видео или TTS-озвучка. Поэтому план сборки — это ДАННЫЕ: у каждого входа своя
роль (кадр кластера / плашка / озвучка), и `assert_no_music` зовётся перед
каждым запуском ffmpeg. Правило, которое проверяет только code review, рано
или поздно нарушается; это — проверяет процесс.

Почему один вызов ffmpeg, а не три с промежуточными файлами: клип собирается
на 2 vCPU, которые мы делим с дедупом, и каждая лишняя перекодировка — это
минуты и ещё один способ уронить очередь.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tgtraff.clips.settings import ClipSettings

log = logging.getLogger("tgtraff.clips.render")

# Роли входов. Ничего, кроме этих трёх, в команде появиться не может.
MEDIA, PLATE, VOICE = "media", "plate", "voice"
ALLOWED_ROLES = frozenset({MEDIA, PLATE, VOICE})

# Фильтры смешивания звука: единственный способ подложить музыку под оригинал.
MIXERS = ("amix", "amerge", "asidedata", "sidechaincompress")

SAMPLE_RATE = 44100
# Хук держится столько: спека — «первые 1.5 сек решают», плашка живёт дольше,
# чтобы её успели прочитать, но не мешает смотреть само событие.
HOOK_SECONDS = 3.5


class MusicForbidden(RuntimeError):
    """В плане сборки появился посторонний звук. Клип не собирается.

    Спека: «Музыка = Content ID, жалобы правообладателей, бан клипа»."""


class RenderError(RuntimeError):
    """ffmpeg не собрал файл."""


@dataclass(frozen=True)
class Source:
    path: str
    role: str


@dataclass(frozen=True)
class Caption:
    """Одна реплика субтитров: текст, готовая плашка и её интервал."""

    text: str
    path: str | Path
    start: float
    end: float


@dataclass(frozen=True)
class RenderPlan:
    mode: str                        # video | photos
    out_path: str | Path
    duration: float
    sources: list[Source]
    argv: list[str]
    audio: str                       # original | voice | none
    subtitles: list[Caption] = field(default_factory=list)
    subtitles_source: str = "none"   # whisper | tts | none

    @property
    def media_files(self) -> list[str]:
        return [s.path for s in self.sources if s.role == MEDIA]


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    path: Path | None = None
    size: int = 0
    error: str = ""


# ---------- правило «без музыки» ----------

def assert_no_music(plan: RenderPlan) -> None:
    """Проверка перед запуском. Ловит три способа получить музыку в клипе:
    посторонний вход, второй звуковой вход рядом с оригиналом и микширование."""
    roles = [s.role for s in plan.sources]
    bad = sorted({r for r in roles if r not in ALLOWED_ROLES})
    if bad:
        raise MusicForbidden(f"посторонний вход в сборке клипа: {', '.join(bad)}")

    voices = roles.count(VOICE)
    if voices > 1:
        raise MusicForbidden(f"звуковых дорожек {voices} — в клипе допустима одна")
    if plan.audio == "original" and voices:
        raise MusicForbidden("к оригинальному звуку добавлена вторая дорожка")
    if plan.audio == "voice" and voices != 1:
        raise MusicForbidden("режим озвучки без файла озвучки")

    command = " ".join(str(a) for a in plan.argv)
    for mixer in MIXERS:
        if mixer in command:
            raise MusicForbidden(f"в команде смешивание звука ({mixer})")


# ---------- режим (а): живое видео ----------

def plan_video(source, out_path, settings: ClipSettings, source_duration: float,
               source_size: tuple[int, int], has_audio: bool, hook, badge, outro,
               subtitles: list[Caption] | None = None, voice=None,
               voice_duration: float | None = None, start: float = 0.0) -> RenderPlan:
    """План сборки из живого видео.

    Звук: оригинальный. Если дорожки нет вовсе — TTS-озвучка сценария (спека
    разрешает ровно эти два источника), и тогда субтитры считаются озвучкой.
    """
    subtitles = list(subtitles or [])
    # 15-40 с спеки — это длина КЛИПА, аутро внутри неё, а не сверх.
    body = min(float(source_duration) - start, settings.max_seconds - settings.outro_seconds)
    body = max(body, 0.5)
    total = body + settings.outro_seconds

    sources = [Source(str(source), MEDIA), Source(str(hook), PLATE), Source(str(badge), PLATE),
               *[Source(str(c.path), PLATE) for c in subtitles], Source(str(outro), PLATE)]
    audio = "original" if has_audio else ("voice" if voice else "none")
    if audio == "voice":
        sources.append(Source(str(voice), VOICE))

    argv = [settings.ffmpeg, "-y", "-v", "error", "-nostdin"]
    if start:
        argv += ["-ss", f"{start:.2f}"]
    argv += ["-t", f"{body:.2f}", "-i", str(source)]
    argv += ["-i", str(hook), "-i", str(badge)]
    for caption in subtitles:
        argv += ["-i", str(caption.path)]
    argv += ["-loop", "1", "-t", f"{settings.outro_seconds:.2f}", "-i", str(outro)]
    if audio == "voice":
        argv += ["-i", str(voice)]

    outro_index = 3 + len(subtitles)
    graph = _video_graph(settings, source_size, subtitles, outro_index, audio, body)
    argv += ["-filter_complex", graph, "-map", "[v]"]
    if audio != "none":
        argv += ["-map", "[a]"]
    argv += _encode_args(settings, total, with_audio=audio != "none")
    argv.append(str(out_path))

    return RenderPlan(mode="video", out_path=out_path, duration=total, sources=sources,
                      argv=argv, audio=audio, subtitles=subtitles,
                      subtitles_source=_subs_source(subtitles, audio, "whisper"))


def _video_graph(settings, source_size, subtitles, outro_index, audio, body) -> str:
    W, H = settings.width, settings.height
    width, height = source_size
    parts: list[str] = []

    if height and width and height / max(width, 1) >= 1.2:
        # Вертикальный источник: кроп по центру, ничего не теряем.
        parts.append(f"[0:v]fps={settings.fps},scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},setsar=1[base]")
    else:
        # Горизонтальный: паддинг размытым фоном. Кроп 16:9 в 9:16 выкинул бы
        # три четверти кадра — вместе с тем, ради чего клип и делается.
        parts.append(
            f"[0:v]fps={settings.fps},split=2[bg][fg];"
            f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=40:2[bgb];"
            f"[fg]scale={W}:-2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")

    last = "base"
    parts.append(f"[{last}][1:v]overlay=0:0:enable='between(t,0,{HOOK_SECONDS})'[v1]")
    last = "v1"
    parts.append(f"[{last}][2:v]overlay=0:0[v2]")
    last = "v2"
    for i, caption in enumerate(subtitles):
        tag = f"v{3 + i}"
        parts.append(f"[{last}][{3 + i}:v]overlay=0:0:"
                     f"enable='between(t,{caption.start:.1f},{caption.end:.1f})'[{tag}]")
        last = tag
    parts.append(f"[{last}]format=yuv420p[main]")
    parts.append(f"[{outro_index}:v]fps={settings.fps},scale={W}:{H},setsar=1,"
                 f"format=yuv420p[outv]")

    if audio == "none":
        parts.append("[main][outv]concat=n=2:v=1:a=0[v]")
        return ";".join(parts)

    voice_index = outro_index + 1
    audio_in = "[0:a]" if audio == "original" else f"[{voice_index}:a]"
    parts.append(f"{audio_in}aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:"
                 f"channel_layouts=stereo,apad,atrim=0:{body:.2f},asetpts=N/SR/TB[maina]")
    parts.append(f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE},"
                 f"atrim=0:{settings.outro_seconds:.2f}[outa]")
    parts.append("[main][maina][outv][outa]concat=n=2:v=1:a=1[v][a]")
    return ";".join(parts)


# ---------- режим (б): фото-слайдшоу ----------

def plan_photos(photos, out_path, settings: ClipSettings, hook, badge, outro,
                subtitles: list[Caption] | None = None, voice=None,
                voice_duration: float | None = None) -> RenderPlan:
    """План слайдшоу. Длительность кадров подгоняется под озвучку: молчащий
    хвост и оборванная на полуслове фраза одинаково плохи."""
    photos = [str(p) for p in photos]
    subtitles = list(subtitles or [])
    per_photo, body = _slide_timing(len(photos), voice_duration, settings)
    total = body + settings.outro_seconds

    sources = [*[Source(p, MEDIA) for p in photos], Source(str(hook), PLATE),
               Source(str(badge), PLATE),
               *[Source(str(c.path), PLATE) for c in subtitles], Source(str(outro), PLATE)]
    audio = "voice" if voice else "none"
    if voice:
        sources.append(Source(str(voice), VOICE))

    argv = [settings.ffmpeg, "-y", "-v", "error", "-nostdin"]
    for photo in photos:
        # Кадр подаётся ОДИН раз, без -loop: длительность задаёт zoompan своим
        # `d`. Зациклённый вход дал бы фильтру сотни входных кадров, и на
        # каждый он выпустил бы d выходных — замер на живом фото: 12-секундное
        # слайдшоу собиралось больше двух минут вместо трёх секунд.
        argv += ["-i", photo]
    argv += ["-i", str(hook), "-i", str(badge)]
    for caption in subtitles:
        argv += ["-i", str(caption.path)]
    argv += ["-loop", "1", "-t", f"{settings.outro_seconds:.2f}", "-i", str(outro)]
    if voice:
        argv += ["-i", str(voice)]

    n = len(photos)
    hook_index, badge_index = n, n + 1
    outro_index = n + 2 + len(subtitles)
    graph = _photos_graph(settings, n, per_photo, subtitles, hook_index, badge_index,
                          outro_index, audio, body)
    argv += ["-filter_complex", graph, "-map", "[v]"]
    if audio != "none":
        argv += ["-map", "[a]"]
    argv += _encode_args(settings, total, with_audio=audio != "none")
    argv.append(str(out_path))

    return RenderPlan(mode="photos", out_path=out_path, duration=total, sources=sources,
                      argv=argv, audio=audio, subtitles=subtitles,
                      subtitles_source=_subs_source(subtitles, audio, "tts"))


def _photos_graph(settings, n, per_photo, subtitles, hook_index, badge_index,
                  outro_index, audio, body) -> str:
    W, H = settings.width, settings.height
    frames = max(int(per_photo * settings.fps), 1)
    parts: list[str] = []
    for i in range(n):
        # Ken Burns: медленный наезд. Масштабируем вдвое крупнее кадра, иначе
        # zoompan заметно мылит картинку на приближении.
        # Пре-скейл ×1.3, а не ×2: zoompan масштабирует КАЖДЫЙ выходной кадр,
        # и восьмимегапиксельный исходник — это чистая трата процессора,
        # которого на VPS два ядра.
        parts.append(
            f"[{i}:v]scale={int(W * 1.3)}:{int(H * 1.3)}:force_original_aspect_ratio=increase,"
            f"crop={int(W * 1.3)}:{int(H * 1.3)},"
            f"zoompan=z='min(zoom+0.0012,1.15)':d={frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={settings.fps},"
            f"setsar=1[s{i}]")
    parts.append("".join(f"[s{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[slides]")

    parts.append(f"[slides][{hook_index}:v]overlay=0:0:"
                 f"enable='between(t,0,{HOOK_SECONDS})'[p1]")
    parts.append(f"[p1][{badge_index}:v]overlay=0:0[p2]")
    last = "p2"
    for i, caption in enumerate(subtitles):
        tag = f"p{3 + i}"
        parts.append(f"[{last}][{n + 2 + i}:v]overlay=0:0:"
                     f"enable='between(t,{caption.start:.1f},{caption.end:.1f})'[{tag}]")
        last = tag
    parts.append(f"[{last}]format=yuv420p[main]")
    parts.append(f"[{outro_index}:v]fps={settings.fps},scale={W}:{H},setsar=1,"
                 f"format=yuv420p[outv]")

    if audio == "none":
        parts.append("[main][outv]concat=n=2:v=1:a=0[v]")
        return ";".join(parts)

    voice_index = outro_index + 1
    parts.append(f"[{voice_index}:a]aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:"
                 f"channel_layouts=stereo,apad,atrim=0:{body:.2f},asetpts=N/SR/TB[maina]")
    parts.append(f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE},"
                 f"atrim=0:{settings.outro_seconds:.2f}[outa]")
    parts.append("[main][maina][outv][outa]concat=n=2:v=1:a=1[v][a]")
    return ";".join(parts)


def _slide_timing(n: int, voice_duration: float | None, settings: ClipSettings):
    """Сколько держим каждый кадр и сколько идёт слайдшоу целиком."""
    n = max(n, 1)
    floor = settings.min_seconds - settings.outro_seconds
    ceil = settings.max_seconds - settings.outro_seconds
    if voice_duration:
        # +0.6 с хвоста: голос не должен обрываться вместе с последним кадром.
        # Длину слайдшоу мы задаём сами, поэтому держим её в полосе спеки:
        # клип короче 15 с в рекомендациях площадки живёт хуже.
        body = min(max(voice_duration + 0.6, floor), ceil)
    else:
        body = min(max(n * settings.photo_seconds, floor), ceil)
    return body / n, body


# ---------- запуск ----------

def run(plan: RenderPlan, settings: ClipSettings) -> RenderResult:
    """Собирает файл. Перед запуском — проверка на музыку (она обязательна:
    правило спеки не имеет права зависеть от того, кто собрал план)."""
    assert_no_music(plan)
    out = Path(plan.out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run([str(a) for a in plan.argv], capture_output=True, text=True,
                              timeout=settings.render_timeout_sec, check=False)
    except subprocess.TimeoutExpired:
        return RenderResult(False, error=f"ffmpeg не уложился в {settings.render_timeout_sec} с")
    except OSError as exc:
        return RenderResult(False, error=f"ffmpeg не запустился: {exc}")
    if done.returncode != 0:
        return RenderResult(False, error=_tail(done.stderr) or f"ffmpeg вернул {done.returncode}")
    if not out.exists() or not out.stat().st_size:
        return RenderResult(False, error="ffmpeg отработал, а файла нет")

    size = out.stat().st_size
    if size > settings.max_file_bytes:
        return RenderResult(False, path=out, size=size,
                            error=f"файл {size / 1048576:.1f} МБ больше лимита "
                                  f"{settings.max_file_mb} МБ")
    return RenderResult(True, path=out, size=size)


def _encode_args(settings: ClipSettings, duration: float, with_audio: bool) -> list[str]:
    args = ["-c:v", "libx264", "-preset", settings.preset, "-b:v", settings.video_bitrate,
            "-maxrate", settings.video_bitrate, "-bufsize", _bufsize(settings.video_bitrate),
            "-pix_fmt", "yuv420p", "-r", str(settings.fps),
            "-movflags", "+faststart", "-t", f"{duration:.2f}"]
    if settings.ffmpeg_threads:
        args += ["-threads", str(settings.ffmpeg_threads)]
    if with_audio:
        args += ["-c:a", "aac", "-b:a", settings.audio_bitrate, "-ar", str(SAMPLE_RATE)]
    return args


def _bufsize(bitrate: str) -> str:
    """Буфер вдвое от битрейта — обычная практика для VBV."""
    digits = "".join(ch for ch in bitrate if ch.isdigit())
    suffix = bitrate[len(digits):] or "M"
    return f"{int(digits or 5) * 2}{suffix}"


def _subs_source(subtitles, audio: str, default: str) -> str:
    if not subtitles:
        return "none"
    return "tts" if audio == "voice" else default


def _tail(text: str | None, limit: int = 400) -> str:
    return " ".join((text or "").split())[-limit:]
