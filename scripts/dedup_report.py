#!/usr/bin/env python
"""Страница ручной проверки склеек — приёмка фазы 3 (спека §3).

Собирает две выборки за сутки: случайные 50 кластеров (как требует спека) и
все склейки этих суток (там и живут возможные ошибки), и рендерит их одной
самодостаточной HTML-страницей: кластер → источники → тексты, с отметками
«верная / ложная / не уверен» и счётчиком доли ложных склеек.

    .venv/bin/python scripts/dedup_report.py --day 2026-09-02 \
        --out docs/reports/dedup-review-2026-09-02.html
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgtraff.app import load  # noqa: E402
from tgtraff.db import Database  # noqa: E402
from tgtraff.dedup.sample import collect  # noqa: E402

TEMPLATE = """<title>Склейки ЧП-ленты</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet"
      href="https://fonts.googleapis.com/css2?family=PT+Sans:wght@400;700&family=PT+Sans+Narrow:wght@400;700&family=PT+Serif:ital,wght@0,400;0,700;1,400&display=swap">
<style>
:root {
  --ground: #f2f3f6;
  --panel: #ffffff;
  --panel-sunken: #e9ebf0;
  --ink: #171b22;
  --ink-soft: #4d5665;
  --ink-faint: #79839a;
  --rule: #d7dbe3;
  --rule-strong: #b9c0cd;
  --accent: #24418c;
  --accent-soft: #e5e9f6;
  --ok: #1f6b45;
  --ok-soft: #e0efe6;
  --bad: #a3231b;
  --bad-soft: #f7e4e2;
  --warn: #8a5a06;
  --warn-soft: #f6ecda;
  --violet: #5c4390;
  --violet-soft: #ebe6f6;
  --shadow: 0 1px 2px rgba(23, 27, 34, .06), 0 8px 24px -18px rgba(23, 27, 34, .5);
  --serif: "PT Serif", Georgia, "Times New Roman", serif;
  --sans: "PT Sans", "Helvetica Neue", Arial, sans-serif;
  --narrow: "PT Sans Narrow", "PT Sans", Arial, sans-serif;
}
:root:not([data-theme="light"]) { }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #14171d;
    --panel: #1b1f27;
    --panel-sunken: #232833;
    --ink: #e9ecf2;
    --ink-soft: #b0b8c6;
    --ink-faint: #838d9e;
    --rule: #2e3441;
    --rule-strong: #414958;
    --accent: #9db4ee;
    --accent-soft: #23304f;
    --ok: #86d3a7;
    --ok-soft: #1d3629;
    --bad: #f0a49c;
    --bad-soft: #3d2320;
    --warn: #e2b872;
    --warn-soft: #3a2f1a;
    --violet: #bdaae8;
    --violet-soft: #2b2540;
    --shadow: 0 1px 2px rgba(0, 0, 0, .5), 0 10px 28px -20px rgba(0, 0, 0, .9);
  }
}
:root[data-theme="dark"] {
  --ground: #14171d;
  --panel: #1b1f27;
  --panel-sunken: #232833;
  --ink: #e9ecf2;
  --ink-soft: #b0b8c6;
  --ink-faint: #838d9e;
  --rule: #2e3441;
  --rule-strong: #414958;
  --accent: #9db4ee;
  --accent-soft: #23304f;
  --ok: #86d3a7;
  --ok-soft: #1d3629;
  --bad: #f0a49c;
  --bad-soft: #3d2320;
  --warn: #e2b872;
  --warn-soft: #3a2f1a;
  --violet: #bdaae8;
  --violet-soft: #2b2540;
  --shadow: 0 1px 2px rgba(0, 0, 0, .5), 0 10px 28px -20px rgba(0, 0, 0, .9);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 16px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 62rem; margin: 0 auto; padding: 0 1.25rem 5rem; }

/* ---- шапка ---- */
header.masthead {
  border-bottom: 2px solid var(--ink);
  padding: 2.25rem 0 1.1rem;
  margin-bottom: 1.5rem;
}
.eyebrow {
  font-family: var(--narrow);
  text-transform: uppercase;
  letter-spacing: .16em;
  font-size: .78rem;
  color: var(--ink-faint);
}
h1 {
  font-family: var(--serif);
  font-size: clamp(1.7rem, 1.2rem + 2vw, 2.5rem);
  line-height: 1.12;
  margin: .35rem 0 .5rem;
  text-wrap: balance;
}
.standfirst {
  font-family: var(--serif);
  font-size: 1.05rem;
  color: var(--ink-soft);
  max-width: 44rem;
  margin: 0;
}
.standfirst b { color: var(--ink); }

/* ---- табло ---- */
.scoreboard {
  position: sticky; top: 0; z-index: 20;
  background: var(--panel);
  border: 1px solid var(--rule);
  border-radius: 3px;
  box-shadow: var(--shadow);
  margin-bottom: 1.75rem;
}
.score-row {
  display: flex; flex-wrap: wrap; align-items: stretch;
  gap: 0; border-bottom: 1px solid var(--rule);
}
.score-cell { padding: .7rem 1rem; flex: 1 1 8rem; border-right: 1px solid var(--rule); }
.score-cell:last-child { border-right: 0; }
.score-label {
  font-family: var(--narrow); text-transform: uppercase; letter-spacing: .11em;
  font-size: .72rem; color: var(--ink-faint); display: block;
}
.score-value {
  font-family: var(--serif); font-size: 1.5rem; font-weight: 700;
  font-variant-numeric: tabular-nums; line-height: 1.15;
}
.score-value small { font-size: .85rem; font-weight: 400; color: var(--ink-faint); }
.verdict { font-family: var(--narrow); text-transform: uppercase; letter-spacing: .1em; font-size: .8rem; }
.verdict.pass { color: var(--ok); }
.verdict.fail { color: var(--bad); }
.verdict.idle { color: var(--ink-faint); }
.score-actions { display: flex; gap: .5rem; padding: .6rem 1rem; flex-wrap: wrap; align-items: center; }
button {
  font-family: var(--narrow); font-size: .85rem; text-transform: uppercase; letter-spacing: .08em;
  background: var(--panel); color: var(--ink); border: 1px solid var(--rule-strong);
  border-radius: 2px; padding: .4rem .8rem; cursor: pointer;
}
button:hover { border-color: var(--ink-soft); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tab[aria-selected="true"] { background: var(--ink); color: var(--ground); border-color: var(--ink); }
.hint { color: var(--ink-faint); font-size: .85rem; margin-left: auto; }

/* ---- кластеры ---- */
.cluster {
  background: var(--panel); border: 1px solid var(--rule); border-radius: 3px;
  margin-bottom: 1.1rem; overflow: hidden;
}
.cluster.single { background: transparent; border-style: dashed; }
.cluster.marked-true { border-left: 3px solid var(--ok); }
.cluster.marked-false { border-left: 3px solid var(--bad); }
.cluster.marked-unsure { border-left: 3px solid var(--warn); }
.c-head {
  display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: baseline;
  padding: .75rem 1rem; border-bottom: 1px solid var(--rule); background: var(--panel-sunken);
}
.cluster.single .c-head { background: transparent; border-bottom-style: dashed; }
.c-id { font-family: var(--narrow); font-weight: 700; letter-spacing: .04em; }
.c-meta { font-family: var(--narrow); color: var(--ink-soft); font-size: .9rem; }
.c-meta b { color: var(--ink); font-weight: 700; }
.posts { list-style: none; margin: 0; padding: 0; }
.post { padding: .85rem 1rem .95rem; border-top: 1px solid var(--rule); }
.post:first-child { border-top: 0; }
.p-meta {
  display: flex; flex-wrap: wrap; gap: .45rem .7rem; align-items: center;
  font-family: var(--narrow); font-size: .86rem; color: var(--ink-soft); margin-bottom: .35rem;
}
.src { font-weight: 700; color: var(--ink); letter-spacing: .01em; }
.time { font-variant-numeric: tabular-nums; }
.chip {
  font-size: .74rem; text-transform: uppercase; letter-spacing: .07em;
  padding: .1rem .42rem; border-radius: 2px; white-space: nowrap;
}
.chip.exact { background: var(--accent-soft); color: var(--accent); }
.chip.phash { background: var(--violet-soft); color: var(--violet); }
.chip.semantic { background: var(--warn-soft); color: var(--warn); }
.chip.new { background: var(--panel-sunken); color: var(--ink-faint); }
.chip.plat { background: transparent; border: 1px solid var(--rule-strong); color: var(--ink-faint); }
.p-text {
  font-family: var(--serif); font-size: 1rem; line-height: 1.55; margin: 0;
  max-width: 40rem;
}
.p-text.empty { color: var(--ink-faint); font-style: italic; }
.rating { display: flex; flex-wrap: wrap; gap: .4rem; padding: .7rem 1rem; background: var(--panel-sunken); }
.rating button[aria-pressed="true"][data-mark="true"] { background: var(--ok-soft); border-color: var(--ok); color: var(--ok); }
.rating button[aria-pressed="true"][data-mark="false"] { background: var(--bad-soft); border-color: var(--bad); color: var(--bad); }
.rating button[aria-pressed="true"][data-mark="unsure"] { background: var(--warn-soft); border-color: var(--warn); color: var(--warn); }
.single-note {
  padding: .55rem 1rem; font-family: var(--narrow); font-size: .85rem; color: var(--ink-faint);
}
h2.set-title {
  font-family: var(--serif); font-size: 1.25rem; margin: 2rem 0 .35rem;
  padding-bottom: .4rem; border-bottom: 1px solid var(--rule-strong);
}
.set-note { font-family: var(--narrow); color: var(--ink-soft); margin: 0 0 1.1rem; font-size: .92rem; }
.legend { display: flex; flex-wrap: wrap; gap: .4rem .8rem; margin: 0 0 1.5rem; font-family: var(--narrow); font-size: .85rem; color: var(--ink-soft); }
[hidden] { display: none !important; }
@media (max-width: 640px) {
  .score-cell { flex: 1 1 45%; }
  .p-text { font-size: .97rem; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>

<div class="wrap">
  <header class="masthead">
    <div class="eyebrow">tgtraff · фаза 3 · дедуп ленты</div>
    <h1>Проверка склеек за __DAY__</h1>
    <p class="standfirst">
      Дедуп собирает одну новость из разных источников в кластер. Проверяем обратное:
      не склеилось ли лишнее. Кластер <b>верный</b>, если все посты в нём об одном
      и том же событии. Приёмка фазы — <b>меньше 5% ложных склеек</b>.
    </p>
  </header>

  <div class="scoreboard">
    <div class="score-row">
      <div class="score-cell">
        <span class="score-label">оценено склеек</span>
        <span class="score-value" id="stat-rated">0 <small>из __MERGED__</small></span>
      </div>
      <div class="score-cell">
        <span class="score-label">ложных</span>
        <span class="score-value" id="stat-false">0</span>
      </div>
      <div class="score-cell">
        <span class="score-label">доля ложных</span>
        <span class="score-value" id="stat-rate">—</span>
      </div>
      <div class="score-cell">
        <span class="score-label">приёмка &lt; 5%</span>
        <span class="score-value"><span class="verdict idle" id="stat-verdict">нет оценок</span></span>
      </div>
    </div>
    <div class="score-actions">
      <button class="tab" id="tab-sample" aria-selected="true">Случайные 50</button>
      <button class="tab" id="tab-merged" aria-selected="false">Все склейки суток (__MERGED_TOTAL__)</button>
      <button id="copy">Скопировать итог</button>
      <button id="reset">Сбросить отметки</button>
      <span class="hint" id="hint">отметки сохраняются в браузере</span>
    </div>
  </div>

  <div class="legend">
    <span><span class="chip new">первый пост</span> кластер начался с него</span>
    <span><span class="chip exact">точный хэш</span> совпал нормализованный текст</span>
    <span><span class="chip phash">картинка</span> совпало фото</span>
    <span><span class="chip semantic">смысл</span> близость векторов, показано cosine</span>
  </div>

  <section id="set-sample">
    <h2 class="set-title">Случайная выборка: 50 кластеров</h2>
    <p class="set-note" id="note-sample"></p>
    <div id="list-sample"></div>
  </section>

  <section id="set-merged" hidden>
    <h2 class="set-title">Все кластеры суток, где что-то склеилось</h2>
    <p class="set-note" id="note-merged"></p>
    <div id="list-merged"></div>
  </section>
</div>

<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const KEY = 'tgtraff-dedup-' + DATA.day;
const marks = load();

function load() {
  try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; }
}
function save() {
  try { localStorage.setItem(KEY, JSON.stringify(marks)); }
  catch (e) { document.getElementById('hint').textContent = 'браузер не сохраняет отметки'; }
}

const REASON = {
  new: ['new', 'первый пост'],
  exact_text: ['exact', 'точный хэш'],
  phash: ['phash', 'картинка'],
  semantic: ['semantic', 'смысл'],
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function renderCluster(c) {
  const box = el('article', 'cluster' + (c.size > 1 ? '' : ' single'));
  box.id = 'c' + c.id;
  box.dataset.cluster = c.id;

  const head = el('div', 'c-head');
  head.append(el('span', 'c-id', 'Кластер ' + c.id));
  const meta = el('span', 'c-meta');
  meta.innerHTML = '<b>' + c.size + '</b> ' + plural(c.size, 'пост', 'поста', 'постов')
    + ' · ' + c.city + ' · ' + c.sources.join(', ');
  head.append(meta);
  box.append(head);

  const list = el('ul', 'posts');
  for (const p of c.posts) {
    const li = el('li', 'post');
    const m = el('div', 'p-meta');
    m.append(el('span', 'src', p.source));
    m.append(el('span', 'chip plat', p.platform));
    m.append(el('span', 'time', p.published_at.slice(5, 16).replace('T', ' ')));
    const [cls, label] = REASON[p.reason] || ['new', p.reason || ''];
    const chip = el('span', 'chip ' + cls, label + (p.similarity !== null && p.reason !== 'new'
      ? ' ' + p.similarity.toFixed(2) : ''));
    m.append(chip);
    if (p.media) m.append(el('span', 'chip plat', p.media + ' фото'));
    li.append(m);
    const t = el('p', 'p-text' + (p.text ? '' : ' empty'), p.text || 'без текста, только медиа');
    li.append(t);
    list.append(li);
  }
  box.append(list);

  if (c.size > 1) {
    const rating = el('div', 'rating');
    for (const [mark, label] of [['true', 'Склейка верная'], ['false', 'Ложная склейка'],
                                 ['unsure', 'Не уверен']]) {
      const b = el('button', null, label);
      b.dataset.mark = mark;
      b.setAttribute('aria-pressed', marks[c.id] === mark ? 'true' : 'false');
      b.addEventListener('click', () => setMark(c.id, mark));
      rating.append(b);
    }
    box.append(rating);
  } else {
    box.append(el('div', 'single-note', 'Один пост — склейки нет, ошибиться не в чем.'));
  }
  applyMark(box, marks[c.id]);
  return box;
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}

function applyMark(box, mark) {
  box.classList.remove('marked-true', 'marked-false', 'marked-unsure');
  if (mark) box.classList.add('marked-' + mark);
  box.querySelectorAll('.rating button').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.mark === mark ? 'true' : 'false'));
}

function setMark(id, mark) {
  if (marks[id] === mark) delete marks[id]; else marks[id] = mark;
  save();
  document.querySelectorAll('[data-cluster="' + id + '"]').forEach(b => applyMark(b, marks[id]));
  refresh();
}

function refresh() {
  const ids = DATA.merged_ids;
  const rated = ids.filter(id => marks[id]);
  const bad = rated.filter(id => marks[id] === 'false');
  document.getElementById('stat-rated').innerHTML =
    rated.length + ' <small>из ' + ids.length + '</small>';
  document.getElementById('stat-false').textContent = bad.length;
  const v = document.getElementById('stat-verdict');
  if (!rated.length) {
    document.getElementById('stat-rate').textContent = '—';
    v.textContent = 'нет оценок'; v.className = 'verdict idle';
    return;
  }
  const rate = bad.length / rated.length * 100;
  document.getElementById('stat-rate').textContent = rate.toFixed(1) + '%';
  v.textContent = rate < 5 ? 'проходит' : 'не проходит';
  v.className = 'verdict ' + (rate < 5 ? 'pass' : 'fail');
}

function summary() {
  const ids = DATA.merged_ids;
  const rated = ids.filter(id => marks[id]);
  const bad = rated.filter(id => marks[id] === 'false');
  const unsure = rated.filter(id => marks[id] === 'unsure');
  const rate = rated.length ? (bad.length / rated.length * 100).toFixed(1) : '—';
  return [
    'Проверка склеек за ' + DATA.day + ' (дедуп, фаза 3)',
    'Оценено склеек: ' + rated.length + ' из ' + ids.length,
    'Ложных: ' + bad.length + ' (' + rate + '%), не уверен: ' + unsure.length,
    bad.length ? 'Ложные кластеры: ' + bad.join(', ') : 'Ложных склеек не отмечено',
    unsure.length ? 'Под вопросом: ' + unsure.join(', ') : '',
  ].filter(Boolean).join('\\n');
}

document.getElementById('copy').addEventListener('click', async (e) => {
  const text = summary();
  try {
    await navigator.clipboard.writeText(text);
    e.target.textContent = 'Скопировано';
    setTimeout(() => { e.target.textContent = 'Скопировать итог'; }, 1600);
  } catch (err) {
    window.prompt('Скопируйте итог:', text);
  }
});

document.getElementById('reset').addEventListener('click', () => {
  if (!window.confirm('Снять все отметки?')) return;
  for (const k of Object.keys(marks)) delete marks[k];
  save();
  document.querySelectorAll('.cluster').forEach(b => applyMark(b, undefined));
  refresh();
});

function showSet(which) {
  document.getElementById('set-sample').hidden = which !== 'sample';
  document.getElementById('set-merged').hidden = which !== 'merged';
  document.getElementById('tab-sample').setAttribute('aria-selected', which === 'sample');
  document.getElementById('tab-merged').setAttribute('aria-selected', which === 'merged');
}
document.getElementById('tab-sample').addEventListener('click', () => showSet('sample'));
document.getElementById('tab-merged').addEventListener('click', () => showSet('merged'));

for (const c of DATA.sample.clusters) document.getElementById('list-sample').append(renderCluster(c));
for (const c of DATA.merged.clusters) document.getElementById('list-merged').append(renderCluster(c));
document.getElementById('note-sample').textContent =
  'Случайные ' + DATA.sample.clusters.length + ' из ' + DATA.sample.total_clusters_in_day
  + ' кластеров суток. Со склейкой из них — ' + DATA.sample.sample_merged
  + '; остальные состоят из одного поста.';
document.getElementById('note-merged').textContent =
  'Все ' + DATA.merged.clusters.length + ' кластеров суток, куда попало больше одного поста. '
  + 'Именно здесь возможна ложная склейка.';
refresh();
</script>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="HTML-страница ручной проверки склеек")
    ap.add_argument("--day", help="сутки YYYY-MM-DD (по умолчанию — вчера)")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--city")
    ap.add_argument("--out", required=True)
    ap.add_argument("--text-limit", type=int, default=900,
                    help="сколько символов текста поста показывать")
    args = ap.parse_args()

    cfg = load()
    db = Database(cfg.paths.db)
    day = args.day or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    sample = collect(db, day=day, limit=args.limit, seed=args.seed,
                     only_merged=False, city=args.city)
    merged = collect(db, day=day, limit=10_000, seed=args.seed,
                     only_merged=True, city=args.city)

    for block in (sample, merged):
        for cluster in block["clusters"]:
            for post in cluster["posts"]:
                if len(post["text"]) > args.text_limit:
                    post["text"] = post["text"][:args.text_limit].rstrip() + "…"

    payload = {
        "day": day,
        "generated_at": sample["generated_at"],
        "sample": sample,
        "merged": merged,
        "merged_ids": sorted({c["id"] for c in merged["clusters"]}
                             | {c["id"] for c in sample["clusters"] if c["size"] > 1}),
    }

    html = (TEMPLATE
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__DAY__", day)
            .replace("__MERGED_TOTAL__", str(len(merged["clusters"])))
            .replace("__MERGED__", str(len(payload["merged_ids"]))))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"записано: {out} ({len(html) // 1024} КБ, склеек к проверке "
          f"{len(payload['merged_ids'])})")
    db.close()


if __name__ == "__main__":
    main()
