"""Эмбеддинги ЛОКАЛЬНОЙ моделью (спека, фаза 3: «без внешних API»).

Модель — sentence-transformers, мультиязычная; веса лежат в `data/models` рядом
с проектом и грузятся с диска. В рантайме сеть не нужна: с `allow_download: false`
модуль работает строго офлайн (режим VPS фазы 6).

Наружу — минимальный контракт `encode(list[str]) -> (n, dim) float32, L2-норма`,
чтобы тесты кластеризации подставляли свою модель и не тянули торч.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

from tgtraff.dedup.settings import DedupSettings

log = logging.getLogger("tgtraff.dedup.embed")


class LocalEmbedder:
    """Обёртка над SentenceTransformer с ленивой загрузкой весов.

    Модель весит сотни мегабайт и грузится ~10 секунд — держим одну на процесс
    и поднимаем только когда реально пришёл текст."""

    def __init__(self, settings: DedupSettings, root: Path | None = None):
        self.settings = settings
        self.name = settings.model
        self._root = root or Path.cwd()
        self._model = None
        self._dim: int | None = None

    # ---- загрузка ----

    @property
    def model_dir(self) -> Path:
        path = Path(self.settings.model_dir).expanduser()
        return path if path.is_absolute() else (self._root / path)

    def local_snapshot(self) -> Path | None:
        """Скачанные веса на диске. Есть — грузим прямо из каталога и в сеть
        не ходим вовсе, независимо от переменных окружения HF."""
        folder = "models--" + self.name.replace("/", "--")
        for base in (self.model_dir / "hub", self.model_dir):
            snapshots = base / folder / "snapshots"
            if not snapshots.is_dir():
                continue
            candidates = [d for d in snapshots.iterdir() if (d / "config.json").exists()]
            if candidates:
                return max(candidates, key=lambda d: d.stat().st_mtime)
        return None

    def _load(self):
        if self._model is not None:
            return self._model

        cache = str(self.model_dir / "hub")
        # Переменные ставим ДО импорта: huggingface_hub читает их один раз,
        # при импорте модуля.
        os.environ.setdefault("HF_HOME", str(self.model_dir))
        os.environ.setdefault("HF_HUB_CACHE", cache)
        os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", cache)

        from sentence_transformers import SentenceTransformer   # тяжёлый импорт — по требованию

        snapshot = self.local_snapshot()
        if snapshot is None and not self.settings.allow_download:
            raise RuntimeError(
                f"модель {self.name} не найдена в {self.model_dir} и скачивание выключено "
                f"(dedup.allow_download: false)"
            )
        source = str(snapshot) if snapshot else self.name
        log.info("загружаю модель эмбеддингов %s (%s) из %s", self.name, self.settings.device,
                 "локального каталога" if snapshot else "hugging face")
        self._model = SentenceTransformer(
            source, device=self.settings.device, cache_folder=cache,
            local_files_only=snapshot is not None,
        )
        # имя метода поменялось в sentence-transformers 6 — поддерживаем оба
        dim = getattr(self._model, "get_embedding_dimension", None) or \
            self._model.get_sentence_embedding_dimension
        self._dim = int(dim())
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._load()
        return int(self._dim)

    # ---- кодирование ----

    def encode(self, texts: list[str]) -> np.ndarray:
        """Векторы текстов. Пустой текст → нулевой вектор: он ни с чем не совпадёт,
        а модель на пустой строке выдаёт бессмысленный, но ненулевой вектор."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        model = self._load()
        idx = [i for i, t in enumerate(texts) if t and t.strip()]
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        if not idx:
            return out

        prefix = self.settings.text_prefix
        batch = [prefix + texts[i] for i in idx]
        vectors = model.encode(
            batch, batch_size=self.settings.batch_size,
            normalize_embeddings=True, show_progress_bar=False,
            convert_to_numpy=True,
        )
        out[idx] = np.asarray(vectors, dtype=np.float32)
        return out


def cosine_matrix(vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Косинусы вектора со всем окном разом. Векторы нормированы, поэтому
    косинус — обычное скалярное произведение: одно умножение матриц на пост."""
    if matrix.size == 0 or vec is None:
        return np.zeros((0,), dtype=np.float32)
    return matrix @ np.asarray(vec, dtype=np.float32)
