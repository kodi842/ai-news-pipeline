"""Набор провокаций для приёмки safety-фильтра (спека, фаза 4).

Данные лежат в `config/safety_probe.yaml`: оператор может дописать туда свои
случаи, не трогая код. Тем же набором пользуются тест и скрипт приёмки.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "safety_probe.yaml"


@dataclass(frozen=True)
class Probe:
    id: str
    text: str
    expected: str
    note: str = ""


def load_probes(path: Path | None = None) -> list[Probe]:
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text("utf-8")) or {}
    return [
        Probe(id=item["id"], text=" ".join(item["text"].split()),
              expected=item["expected"], note=item.get("note", ""))
        for item in (raw.get("probes") or [])
    ]
