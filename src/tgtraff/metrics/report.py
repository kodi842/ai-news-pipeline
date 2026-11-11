"""CPF-отчёт по инвайт-ссылкам (спека §9, критерий приёмки фазы 6).

CPF (cost per follower) — главная цифра гейта месяца 2: «побеждает лучшая
юнит-экономика». Считается по площадкам, поэтому и ссылка у каждой площадки
своя.

Два правила, из-за которых отчёт не сводится к делению:
  * у органики CPF не бывает — у неё нет рублей в числителе, и ноль вместо
    прочерка сделал бы органику «самой выгодной площадкой»;
  * площадка без вступлений — это не CPF = ∞, это «денег потрачено столько,
    подписчиков нет»: так и печатаем.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from tgtraff.metrics import store
from tgtraff.publisher.schedule import local_day

DEFAULT_TZ = "Europe/Moscow"


@dataclass(frozen=True)
class CpfRow:
    city: str
    name: str
    kind: str
    joins: int
    joins_window: int      # прирост за окно отчёта (по умолчанию неделя)
    cost: float
    url: str | None = None
    note: str | None = None

    @property
    def cpf(self) -> float | None:
        if self.kind != "paid" or not self.cost or not self.joins:
            return None
        return round(self.cost / self.joins, 2)


@dataclass(frozen=True)
class CpfReport:
    rows: list[CpfRow] = field(default_factory=list)
    days: tuple[str, ...] = ()

    @property
    def joins(self) -> int:
        return sum(r.joins for r in self.rows)

    @property
    def paid_joins(self) -> int:
        return sum(r.joins for r in self.rows if r.kind == "paid")

    @property
    def cost(self) -> float:
        return sum(r.cost for r in self.rows)

    @property
    def cpf(self) -> float | None:
        """Средний CPF закупа: рубли на платных площадках / приведённые ими люди."""
        return round(self.cost / self.paid_joins, 2) if self.cost and self.paid_joins else None

    def render(self) -> str:
        if not self.rows:
            return ("Инвайт-ссылок пока нет. Заведите их до старта закупа: "
                    "scripts/metrics.py --links")
        width = max(len(r.name) for r in self.rows)
        window = f"{len(self.days)} дн." if self.days else "окно"
        lines = [f"{'источник трафика'.ljust(width)}  вид      всего {window:>7}   рублей     CPF"]
        city = None
        for row in sorted(self.rows, key=lambda r: (r.city, r.kind, r.name)):
            if row.city != city:
                city = row.city
                lines.append(f"— {city} —")
            cpf = f"{row.cpf:>7.2f}" if row.cpf is not None else "      —"
            cost = f"{row.cost:>8.0f}" if row.cost else "       —"
            lines.append(f"{row.name.ljust(width)}  {row.kind:<8} {row.joins:>5}  "
                         f"{row.joins_window:>6}  {cost}  {cpf}")
        total_cpf = f"{self.cpf:.2f} ₽" if self.cpf is not None else "—"
        lines += ["",
                  f"всего подписчиков по ссылкам: {self.joins} "
                  f"(из них с закупа {self.paid_joins})",
                  f"потрачено: {self.cost:.0f} ₽, средний CPF закупа: {total_cpf}"]
        return "\n".join(lines)


def cpf_report(db, now: datetime | None = None, city: str | None = None,
               city_tz: dict[str, str] | None = None, days: int = 7) -> CpfReport:
    now = now or datetime.now(timezone.utc)
    city_tz = city_tz or {}
    rows = []
    window: tuple[str, ...] = ()
    for link in store.links(db, city):
        if link["kind"] == "service":             # вход нашего же юзербота — не трафик
            continue
        tz = city_tz.get(link["city"], DEFAULT_TZ)
        window = tuple(local_day(now - timedelta(days=i), tz) for i in range(days))
        rows.append(CpfRow(
            city=link["city"], name=link["name"], kind=link["kind"] or "other",
            joins=int(link["joins_total"] or 0),
            joins_window=store.joins_in_days(db, link["id"], window),
            cost=float(link["cost_rub"] or 0), url=link["url"], note=link["note"]))
    return CpfReport(rows=rows, days=window)
