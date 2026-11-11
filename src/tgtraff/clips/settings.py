"""Параметры завода клипов (config/settings.yaml → блок `clips`).

Всё, что спека называет числом, — параметр конфига, а не константа в коде:
лимит клипов на город, порог виральности, длина клипа, пресет ffmpeg, голос
озвучки. Мультигородность здесь та же, что у постов: город — параметр.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Категории-фавориты спеки: «dtp/fire/weather/city-резонанс».
FAVORITES = ("dtp", "fire", "weather", "city")


@dataclass(frozen=True)
class ClipSettings:
    enabled: bool = True

    # ---------- отбор ----------
    # Сколько клипов в сутки на ГОРОД (спека: старт 3).
    clips_per_day: int = 3
    # Ниже этого скоринга виральности клип не делаем (шкала 1-10).
    min_score: int = 6
    favorite_categories: tuple[str, ...] = FAVORITES
    # Категории, выключенные из клипов вручную (kill-switch по категории).
    blocked_categories: tuple[str, ...] = ()
    # Насколько свежей должна быть новость, чтобы стать клипом.
    max_age_hours: int = 24
    # Сколько кандидатов отдаём в скоринг за проход (предохранитель по деньгам).
    score_per_pass: int = 8

    # ---------- кандидаты ----------
    min_video_seconds: float = 8.0     # спека: живое видео ≥8 с
    min_photos: int = 2                # спека: ≥2 пригодных фото
    max_photos: int = 5                # слайдшоу 2-5 кадров
    # Сколько платных проверок на чужой знак имеем право потратить на клип.
    max_checks: int = 4
    # С какой секунды берём кадр видео на проверку знака (первый часто чёрный).
    watermark_frame_at: float = 1.5

    # ---------- сборка ----------
    width: int = 1080
    height: int = 1920
    fps: int = 30
    min_seconds: float = 15.0          # спека: обрезка до 15-40 с
    max_seconds: float = 40.0
    photo_seconds: float = 4.0         # сколько держим один кадр слайдшоу
    outro_seconds: float = 1.0         # аутро с CTA
    preset: str = "veryfast"
    video_bitrate: str = "5M"          # спека: ~4-6 Мбит
    audio_bitrate: str = "128k"
    max_file_mb: int = 40
    crf: int = 23                      # запасной путь, если битрейт не задан

    # ---------- речь ----------
    whisper_enabled: bool = True
    whisper_model: str = "base"        # спека: базовая модель, CPU
    whisper_compute: str = "int8"
    whisper_dir: str = "data/models"
    whisper_allow_download: bool = True
    whisper_language: str = "ru"
    # Порог «в видео есть речь»: короче — субтитров не делаем.
    speech_min_chars: int = 12
    tts_voice: str = "ru-RU-DmitryNeural"   # голос ОДИН на сеть (спека)
    tts_rate: str = "+8%"

    # ---------- инфраструктура ----------
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    out_dir: str = "data/clips"
    work_dir: str = "data/clips/work"
    render_timeout_sec: int = 900
    max_attempts: int = 3
    poll_seconds: int = 300
    # Рендер очередью, параллельность 1 (2 vCPU делим с дедупом).
    render_parallelism: int = 1
    # Сколько потоков разрешаем самому ffmpeg: 0 — на его усмотрение.
    ffmpeg_threads: int = 2
    retention_days: int = 14
    # Куда клип уходит руками в V1 (в карточке — подсказкой).
    platforms: tuple[str, ...] = ("VK Клипы", "OK", "Rutube")

    def __post_init__(self):
        # yaml отдаёт списки, а настройки заморожены: приводим к кортежам,
        # иначе конфиг нельзя сравнить и захешировать.
        for name in ("favorite_categories", "blocked_categories", "platforms"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value or ()))

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def aspect(self) -> str:
        return f"{self.width}x{self.height}"
