"""Живая локальная модель эмбеддингов (спека, фаза 3: «без внешних API»).

Тесты кластеризации работают на подставной модели — здесь проверяется контракт
настоящей: векторы нормированы, батч не меняет результат, пустой текст ничему
не близок, а веса берутся с диска, без обращения в сеть.

Если модель ещё не скачана (`data/models`), файл пропускается: это тест
окружения, а не логики.
"""
from pathlib import Path

import numpy as np
import pytest

from tgtraff.dedup.embed import LocalEmbedder, cosine_matrix
from tgtraff.dedup.settings import DedupSettings

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "data" / "models"

pytestmark = pytest.mark.skipif(
    not MODEL_DIR.exists() or not any(MODEL_DIR.rglob("*.safetensors")),
    reason="локальная модель не скачана: .venv/bin/python scripts/dedup.py --backfill скачает",
)


@pytest.fixture(scope="module")
def embedder():
    # allow_download=False — доказательство, что в рантайме сеть не нужна.
    return LocalEmbedder(DedupSettings(allow_download=False), root=ROOT)


def test_vectors_are_normalized_float32(embedder):
    vectors = embedder.encode(["В Магнитогорске сгорел склад", "ДТП на проспекте Ленина"])
    assert vectors.shape == (2, embedder.dim) and vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)


def test_empty_text_gives_zero_vector(embedder):
    vectors = embedder.encode(["", "   ", "новость"])
    assert not vectors[0].any() and not vectors[1].any()
    assert vectors[2].any()


def test_paraphrase_is_close_and_other_news_is_far(embedder):
    a, b, c = embedder.encode([
        "В Магнитогорске на улице Ленина загорелся склад, пострадавших нет",
        "На улице Ленина в Магнитогорске произошёл пожар на складе, никто не пострадал",
        "Администрация Череповца утвердила график отключения горячей воды",
    ])
    assert float(a @ b) > 0.85
    assert float(a @ c) < 0.6


def test_batching_does_not_change_vectors(embedder):
    texts = ["Пожар на складе", "ДТП с автобусом", "Отключение воды"]
    batched = embedder.encode(texts)
    one_by_one = np.vstack([embedder.encode([t])[0] for t in texts])
    assert np.allclose(batched, one_by_one, atol=1e-4)


def test_cosine_matrix_matches_pairwise(embedder):
    vectors = embedder.encode(["пожар на складе", "горит склад", "ремонт дороги"])
    sims = cosine_matrix(vectors[0], vectors)
    assert sims.shape == (3,)
    assert sims[0] == pytest.approx(1.0, abs=1e-3)
    assert sims[1] == pytest.approx(float(vectors[1] @ vectors[0]), abs=1e-5)


def test_cosine_matrix_handles_empty_window(embedder):
    assert cosine_matrix(embedder.encode(["текст"])[0], np.zeros((0, embedder.dim))).shape == (0,)
