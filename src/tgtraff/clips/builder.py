"""Сборщик клипа (VIDEO-SPEC, фаза V1): плашки + речь + ffmpeg.

Здесь сходятся оба режима спеки:

  (а) живое видео — звук ОРИГИНАЛЬНЫЙ, субтитры от Whisper и только если в
      дорожке есть речь. Если звуковой дорожки нет вовсе, читает TTS: спека
      разрешает ровно два источника звука, и молчащий клип из этого списка
      выпадает, а музыка в нём не появляется никогда;
  (б) фото — слайдшоу с Ken Burns, TTS-озвучка сценария, субтитры = озвучка.
      Строки озвучиваются по одной, поэтому субтитр стоит ровно столько,
      сколько звучит его фраза (критерий приёмки V1).

Плашки кэшируются по содержимому в общем каталоге, служебный звук клипа живёт
в его собственном и убирается после сборки: рендер идёт минутами, а диск на
VPS — самый дефицитный ресурс после процессора.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from tgtraff.clips import overlay, render, speech as speech_mod, store
from tgtraff.clips.settings import ClipSettings

log = logging.getLogger("tgtraff.clips.builder")

# Ниже этого субтитр мелькает и не читается.
MIN_CAPTION_SEC = 0.9
# Больше плашек субтитров на клип не делаем: каждая — вход ffmpeg.
MAX_CAPTIONS = 12


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    path: Path | None = None
    size: int = 0
    duration: float = 0.0
    subtitles: str = "none"          # whisper | tts | lines | none
    error: str = ""


class ClipBuilder:
    def __init__(self, settings: ClipSettings | None = None, transcriber=None, voice=None,
                 runner=None, probe=None):
        self.settings = settings or ClipSettings()
        self.transcriber = transcriber
        self.voice = voice
        self._run = runner or render.run
        self._probe = probe or self._default_probe

    def build(self, clip, draft, files, city_name: str | None = None,
              username: str | None = None) -> BuildResult:
        if not files:
            return BuildResult(False, error="исходных файлов нет")
        city = clip["city"]
        hook = clip["hook"] or (draft["title"] if draft is not None else "")
        lines = store.decode_lines(clip)
        cta = clip["cta"] or "Подробности — в нашем канале"

        plates = Path(self.settings.work_dir) / "plates"
        work = Path(self.settings.work_dir) / f"clip{clip['id']}"
        out_path = Path(self.settings.out_dir) / f"clip{clip['id']}_{city}.mp4"

        hook_png = overlay.hook_plate(city, hook, out_dir=plates, settings=self.settings,
                                      city_name=city_name)
        badge_png = overlay.badge_plate(city, out_dir=plates, settings=self.settings,
                                        city_name=city_name)
        outro_png = overlay.outro_frame(city, cta, out_dir=plates, settings=self.settings,
                                        city_name=city_name)

        try:
            if (clip["mode"] or "video") == "video":
                plan, subs_kind = self._video_plan(clip, files[0], hook, lines, city,
                                                   hook_png, badge_png, outro_png,
                                                   plates, work)
            else:
                plan, subs_kind = self._photos_plan(clip, files, hook, lines, city,
                                                    hook_png, badge_png, outro_png,
                                                    plates, work)
        except BuildFailed as exc:
            return BuildResult(False, error=str(exc))

        plan = _with_out_path(plan, out_path)
        result = self._run(plan, self.settings)
        if not result.ok:
            return BuildResult(False, error=result.error)
        _cleanup(work)
        return BuildResult(True, path=result.path, size=result.size,
                           duration=plan.duration, subtitles=subs_kind)

    # ---------- режим (а) ----------

    def _video_plan(self, clip, source, hook, lines, city, hook_png, badge_png,
                    outro_png, plates, work):
        info = self._probe(source)
        if info is None:
            return _fail(f"видео {source} не прочиталось ffprobe")
        body = min(info.duration, self.settings.max_seconds - self.settings.outro_seconds)

        captions: list[render.Caption] = []
        voice_path = None
        voice_duration = None
        subs_kind = "none"

        if info.has_audio:
            # Звук оригинальный. Субтитры — только если в нём есть речь.
            segments = self.transcriber.transcribe(source) if self.transcriber else []
            segments = speech_mod.within(segments, 0.0, body)
            captions = self._caption_plates(city, segments, plates)
            subs_kind = "whisper" if captions else "none"
        else:
            # Дорожки нет: читает TTS, и субтитры равны озвучке.
            parts = self.voice.say_lines([hook, *lines], out_dir=work) if self.voice else None
            if parts:
                voice_path = self.voice.track(parts, work / "voice.m4a")
                voice_duration = speech_mod.total_duration(parts)
                captions = self._voice_captions(city, parts, plates, skip_first=True)
                subs_kind = "tts" if captions else "none"
            if voice_path is None:
                captions = self._even_captions(city, lines, plates, body)
                subs_kind = "lines" if captions else "none"

        plan = render.plan_video(
            source=source, out_path=Path(self.settings.out_dir) / "clip.mp4",
            settings=self.settings, source_duration=info.duration,
            source_size=(info.width, info.height), has_audio=info.has_audio,
            hook=hook_png, badge=badge_png, outro=outro_png, subtitles=captions,
            voice=voice_path, voice_duration=voice_duration)
        return plan, subs_kind

    # ---------- режим (б) ----------

    def _photos_plan(self, clip, files, hook, lines, city, hook_png, badge_png,
                     outro_png, plates, work):
        photos = files[:self.settings.max_photos]
        parts = self.voice.say_lines([hook, *lines], out_dir=work) if self.voice else None
        voice_path = None
        voice_duration = None
        if parts:
            voice_path = self.voice.track(parts, work / "voice.m4a")
            voice_duration = speech_mod.total_duration(parts)
        if voice_path is not None:
            captions = self._voice_captions(city, parts, plates, skip_first=True)
            subs_kind = "tts" if captions else "none"
        else:
            # Без озвучки клип выходит молча — но факты на экране остаются.
            body = max(len(photos) * self.settings.photo_seconds,
                       self.settings.min_seconds - self.settings.outro_seconds)
            captions = self._even_captions(city, lines, plates, body)
            subs_kind = "lines" if captions else "none"

        plan = render.plan_photos(
            photos=photos, out_path=Path(self.settings.out_dir) / "clip.mp4",
            settings=self.settings, hook=hook_png, badge=badge_png, outro=outro_png,
            subtitles=captions, voice=voice_path, voice_duration=voice_duration)
        return plan, subs_kind

    # ---------- субтитры ----------

    def _caption_plates(self, city, segments, plates) -> list[render.Caption]:
        out: list[render.Caption] = []
        for seg in segments[:MAX_CAPTIONS]:
            end = max(seg.end, seg.start + MIN_CAPTION_SEC)
            path = overlay.subtitle_plate(city, seg.text, out_dir=plates,
                                          settings=self.settings)
            if path is None:
                continue
            out.append(render.Caption(text=seg.text, path=path, start=seg.start, end=end))
        return out

    def _voice_captions(self, city, parts, plates, skip_first: bool) -> list[render.Caption]:
        """Тайминги субтитров = тайминги озвучки. Первая часть — хук: он уже
        стоит плашкой, второй раз его подписывать незачем."""
        spans = speech_mod.spans(parts)
        if skip_first:
            spans = spans[1:]
        out: list[render.Caption] = []
        for text, start, end in spans[:MAX_CAPTIONS]:
            path = overlay.subtitle_plate(city, text, out_dir=plates, settings=self.settings)
            if path is None:
                continue
            out.append(render.Caption(text=text, path=path, start=start,
                                      end=max(end, start + MIN_CAPTION_SEC)))
        return out

    def _even_captions(self, city, lines, plates, body: float) -> list[render.Caption]:
        """Озвучки нет — раскладываем строки ровно по длине клипа."""
        lines = [l for l in lines if l][:MAX_CAPTIONS]
        if not lines:
            return []
        step = body / len(lines)
        out: list[render.Caption] = []
        for i, line in enumerate(lines):
            path = overlay.subtitle_plate(city, line, out_dir=plates, settings=self.settings)
            if path is None:
                continue
            out.append(render.Caption(text=line, path=path, start=i * step,
                                      end=min((i + 1) * step, body)))
        return out

    def _default_probe(self, path):
        from tgtraff.clips.probe import probe_video

        return probe_video(path, ffprobe=self.settings.ffprobe)


class BuildFailed(RuntimeError):
    pass


def _fail(message: str):
    raise BuildFailed(message)


def _with_out_path(plan: render.RenderPlan, out_path: Path) -> render.RenderPlan:
    """Итоговый путь известен только сборщику: план строится раньше, поэтому
    подменяем последний аргумент команды (это и есть выходной файл)."""
    argv = list(plan.argv)
    argv[-1] = str(out_path)
    return render.RenderPlan(mode=plan.mode, out_path=out_path, duration=plan.duration,
                             sources=plan.sources, argv=argv, audio=plan.audio,
                             subtitles=plan.subtitles,
                             subtitles_source=plan.subtitles_source)


def _cleanup(work: Path) -> None:
    """Служебный звук клипа после сборки не нужен: в mp4 он уже внутри."""
    try:
        shutil.rmtree(work, ignore_errors=True)
    except OSError as exc:                         # noqa: BLE001
        log.debug("рабочий каталог %s не убрался: %s", work, exc)
