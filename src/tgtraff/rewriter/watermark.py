"""Детектор чужих вотермарков (спека §5): Haiku vision, «есть/нет/чей».

Вердикт кэшируется на строке медиа (`raw_media.watermark`) — одна картинка
стоит нам один платный запрос за всю свою жизнь, сколько бы кластеров её ни
переиспользовало.

Чего здесь принципиально нет и не будет: снятия, кропа и затирания чужих
знаков. Спека §5 запрещает это прямо (ст. 1300 ГК РФ плюс война с пабликами),
поэтому детектор умеет ровно одно — сказать, что картинка чужая.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from tgtraff.llm.client import LlmError, LlmUnavailable

log = logging.getLogger("tgtraff.rewriter.watermark")

NONE, FOREIGN, OWN, UNKNOWN = "none", "foreign", "own", "unknown"

# Картинку перед отправкой ужимаем: вотермарк виден и на 900 px, а платим мы
# за пиксели. 900×900 ≈ 1.1к токенов ≈ $0.001 у Haiku.
MAX_SIDE = 900
JPEG_QUALITY = 80

SYSTEM = """Ты проверяешь фотографию на наличие вотермарка (водяного знака,
логотипа, плашки с названием паблика/СМИ/канала, подписи автора).

Верни ТОЛЬКО JSON:
{"watermark": "none|foreign", "owner": "<чей знак, если виден, иначе null>",
 "note": "<где именно, коротко>"}

Правила:
  * "foreign" — на изображении есть логотип, название канала/СМИ/паблика,
    адрес сайта, ник автора, полупрозрачная плашка с именем источника,
    рамка-подложка с брендом;
  * "none" — чистое фото или кадр без опознавательных знаков;
  * номер машины, вывеска магазина, форма сотрудников, штамп даты
    видеорегистратора, логотип на технике В КАДРЕ — это не вотермарк;
  * если знак есть, но прочитать его нельзя — "foreign", owner: null."""


class WatermarkDetector:
    def __init__(self, client, model: str | None = None, max_side: int = MAX_SIDE):
        self.client = client
        self.model = model
        self.max_side = max_side

    def verdict(self, media) -> tuple[str, str | None]:
        """(вердикт, владелец). `unknown` — проверить не смогли, решает каскад."""
        if media.kind != "photo" or not media.file_path:
            return UNKNOWN, None
        path = Path(media.file_path)
        if not path.exists():
            return UNKNOWN, None
        try:
            payload = _encode(path, self.max_side)
        except Exception as exc:                      # битый файл, экзотический формат
            log.debug("медиа #%s не прочиталось: %s", media.media_id, exc)
            return UNKNOWN, None

        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": payload}},
            {"type": "text", "text": "Есть ли на этом фото чужой вотермарк?"},
        ]
        try:
            data, _ = self.client.ask_json(system=SYSTEM, user=content, max_tokens=150,
                                           purpose="watermark", model=self.model)
        except LlmUnavailable:
            raise                                 # модель недоступна всем — проход прекращается
        except LlmError as exc:
            log.warning("детектор вотермарков недоступен (%s)", exc)
            return UNKNOWN, None

        verdict = str(data.get("watermark", "")).strip().lower()
        owner = data.get("owner") or None
        if verdict not in (NONE, FOREIGN, OWN):
            return UNKNOWN, owner
        return verdict, owner


def _encode(path: Path, max_side: int) -> str:
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        if max(img.size) > max_side:
            ratio = max_side / max(img.size)
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")
