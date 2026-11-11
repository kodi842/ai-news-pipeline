"""Ступень 3 медиа-каскада: шаблонная обложка (спека §5, генератор R0)."""
from tgtraff.rewriter.cover import CATEGORY_MAP, render_cover
from tgtraff.rewriter.settings import CATEGORIES


def test_cover_is_rendered_for_a_category(tmp_path):
    path = render_cover("cherepovets", "fire", district="Зашекснинский",
                        title="Сгорел расселённый дом на Наседкина", out_dir=tmp_path)
    assert path is not None and path.exists()
    assert path.stat().st_size > 5000


def test_same_request_reuses_the_file(tmp_path):
    a = render_cover("magnitogorsk", "dtp", out_dir=tmp_path)
    b = render_cover("magnitogorsk", "dtp", out_dir=tmp_path)
    assert a == b
    assert len(list(tmp_path.iterdir())) == 1


def test_every_rewriter_category_has_its_own_cover(tmp_path):
    """Решение ревью §30: обложки дорисованы для всех семи категорий rewriter'а,
    подмены «криминал → город» больше нет."""
    for category in CATEGORIES:
        assert CATEGORY_MAP[category] == category, category
        assert render_cover("novokuznetsk", category, out_dir=tmp_path) is not None


def test_crime_and_other_are_drawn_as_their_own_cards(tmp_path):
    """Каждый четвёртый драфт живой ленты — crime или other: своя карточка,
    а не городская (§30)."""
    crime = render_cover("magnitogorsk", "crime", title="Из квартиры вынесли сейф",
                         out_dir=tmp_path)
    other = render_cover("magnitogorsk", "other", title="Из квартиры вынесли сейф",
                         out_dir=tmp_path)
    city = render_cover("magnitogorsk", "city", title="Из квартиры вынесли сейф",
                        out_dir=tmp_path)
    assert len({crime, other, city}) == 3
    assert crime.read_bytes() != city.read_bytes()
    assert other.read_bytes() != city.read_bytes()


def test_broken_generator_does_not_break_the_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr("tgtraff.rewriter.cover._render",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("нет шрифтов")))
    assert render_cover("cherepovets", "dtp", out_dir=tmp_path) is None
