# Шрифты

Бренд-пакет собран на **PT Sans** (ParaType): кириллица родная, а не
адаптированная; свободная лицензия; есть узкое начертание PT Sans Narrow —
на нём набраны «ЧП», названия категорий и заголовки обложек.

Разрешение шрифта — в `brand.py`, роли: `display` (PT Sans Narrow Bold),
`bold` (PT Sans Bold), `regular` (PT Sans). Порядок поиска:

1. эта папка — `PTSansNarrow-Bold.ttf`, `PTSans-Bold.ttf`, `PTSans-Regular.ttf`
2. macOS — `/System/Library/Fonts/Supplemental/PTSans.ttc` (есть из коробки)
3. Linux — `/usr/share/fonts/truetype/paratype-pt-sans/…`
4. запасной вариант — DejaVu Sans (кириллица есть, рисунок другой)

## На VPS

```bash
sudo apt-get install -y fonts-paratype
```

Если пакета в дистрибутиве нет — положите три TTF (Google Fonts: «PT Sans»,
«PT Sans Narrow») прямо в эту папку под именами из пункта 1; они найдутся
первыми и рендер будет побайтово одинаковым на всех машинах. Файлы шрифтов
в репозиторий не коммитятся.

Проверить, что нашлось:

```bash
python3 -c "import brand; print(brand.font_report())"
```
