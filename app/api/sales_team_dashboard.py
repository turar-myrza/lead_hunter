"""Local, read-only dashboard for the sales team's queue and saved artifacts."""
from collections import Counter
from html import escape
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse


REPORTS = {
    'leads.xlsx': ('Excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    'leads.csv': ('CSV', 'text/csv; charset=utf-8'),
    'index.html': ('Материалы', 'text/html; charset=utf-8'),
    'summary.json': ('Сводка JSON', 'application/json'),
    'simulation.json': ('Учебный диалог JSON', 'application/json'),
}
PRODUCTS = {'A': 'Внедрение Битрикс24', 'B': 'Доработка Битрикс24', 'C': 'WhatsApp-звонки'}
RUN_STATE = {
    'queued': 'В очереди', 'running': 'В работе', 'completed': 'Завершён',
    'failed': 'Ошибка', 'stopped': 'Остановлен', 'sources_unavailable': 'Источники недоступны',
    'partial_crm': 'CRM проверена частично',
}
ORIGIN = {
    'automatic': 'Автоматический поиск', 'simulation': 'Учебный запуск',
    'manual': 'Ручной запуск', 'public_batch': 'Пакет открытых источников',
}
GROUPS = {
    'qualified': 'Готов черновик', 'hold': 'Требуется решение', 'account': 'Клиент CRM Consulting',
    'excluded': 'Исключена', 'research': 'Требуется проверка', 'simulation': 'Учебная карточка',
}
EVENT_NAMES = {
    'email_sent': 'Отправки', 'email_delivered': 'Доставки',
    'call_connected': 'Соединения', 'interested_reply': 'Ответы с интересом',
    'meeting_agreed': 'Согласованные встречи', 'meeting_booked': 'Назначенные встречи',
    'declined': 'Отказы', 'opt_out': 'Отписки', 'sale': 'Продажи',
}
HEADERS = {
    'Cache-Control': 'no-store',
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}
DEMO_FILE = re.compile(r'demo-[0-9a-f]{12}\.html')

STYLE = """
:root{color-scheme:light;--ink:#172b2e;--muted:#526667;--line:#cedad4;--dark:#153c38;
--green:#256052;--paper:#f3f5f1;--surface:#fff;--warning:#90462d}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 Inter,"Arial",sans-serif}
a{color:#205d4c;text-underline-offset:.19em}
:focus-visible{outline:3px solid #a74524;outline-offset:3px;border-radius:2px}
header{background:var(--dark);color:#fff}
.wrap{width:min(1180px,100% - 44px);margin:auto}
.bar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 0}
.wordmark{font-weight:750;letter-spacing:.01em}
.read-only{font-size:.82rem;color:#d6e8df}
main{padding:27px 0 55px}
.heading{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;padding-bottom:18px;border-bottom:1px solid var(--line)}
h1,h2,h3,p{margin:0}
h1{font:700 clamp(2rem,5vw,3.3rem)/1.1 "Georgia",serif;letter-spacing:-.03em}
h2{font-size:1.15rem;line-height:1.25;margin-bottom:11px}
h3{font-size:.93rem}
nav{display:flex;align-items:center;flex-wrap:wrap;gap:4px 17px;margin-top:10px}
nav a,.refresh{display:inline-flex;align-items:center;min-height:36px;font-size:.9rem}
.refresh{border:1px solid #a7bdb5;border-radius:6px;padding:4px 13px;background:#fff;font-weight:700;text-decoration:none}
.state{font-size:.85rem;font-weight:700;color:var(--green);margin-top:8px}
.state.pause{color:var(--warning)}
.blocks{display:grid;grid-template-columns:1.05fr 1fr;gap:20px;margin:21px 0}
.panel{border:1px solid var(--line);border-radius:9px;background:var(--surface);padding:15px 18px}
.panel.experiment{background:#ecf2ed;border-color:#c4d6c9}
.kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:8px}
.kpi{min-width:0;border-top:1px solid var(--line);padding:8px 0 4px}
.kpi strong{display:block;font-size:1.75rem;line-height:1.24;font-variant-numeric:tabular-nums}
.kpi span,.subtle{font-size:.83rem;color:var(--muted);overflow-wrap:anywhere}
.split{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.section{margin-top:24px;scroll-margin-top:12px}
.table-scroll{overflow:auto;border:1px solid var(--line);border-radius:9px;background:#fff}
table{width:100%;border-collapse:collapse;text-align:left}
th,td{padding:9px 12px;vertical-align:top;border-bottom:1px solid #e0e8e3}
thead th{background:#e7eee9;color:#263b36;font-size:.83rem;font-weight:740}
tbody tr:last-child>*{border-bottom:0}
tbody tr:hover{background:#f7f9f6}
tbody th{text-align:left}
.runid{white-space:nowrap}
.jump{display:flex;flex-wrap:wrap;gap:0 12px;align-items:center}
.jump a{display:inline-flex;align-items:center;min-height:30px}
.empty{padding:15px;border:1px dashed #b0c5b9;border-radius:7px;color:var(--muted);background:#fafcf9}
.product{color:#38574b}
details{margin-top:10px}
summary{cursor:pointer;padding:4px 0;font-size:.85rem;color:var(--green)}
@media(max-width:690px){
  .wrap{width:calc(100% - 26px)}.bar{padding:10px 0}main{padding-top:19px}
  .blocks{grid-template-columns:1fr;gap:12px}.panel{padding:12px 13px}
  .heading{align-items:flex-start;gap:10px}.refresh{padding:4px 8px;margin-top:4px;font-size:.82rem}
  h2{font-size:1.04rem}.kpis{gap:7px}.kpi strong{font-size:1.45rem}
  th,td{padding:8px 10px}td{overflow-wrap:anywhere}table{min-width:480px}
}
"""


def _text(value):
    return escape(str(value or ''), quote=True)


def _run_folder(root, run_id):
    folder = root / str(run_id)
    return folder if folder.is_dir() and not folder.is_symlink() else None


def _artifact(folder, relative):
    if folder is None:
        return None
    if relative in REPORTS:
        label, mime = REPORTS[relative]
    elif DEMO_FILE.fullmatch(relative):
        label, mime = 'Демонстрация', 'text/html; charset=utf-8'
    else:
        return None
    path = folder / relative
    if path.is_symlink() or not path.is_file() or path.resolve().parent != folder:
        return None
    return path, label, mime


def _links(root, run_id):
    folder = _run_folder(root, run_id)
    if folder is None:
        return '<span class="subtle">Материалы ещё недоступны</span>'
    paths = list(REPORTS)
    if not _artifact(folder, 'index.html'):
        paths += [file.name for file in sorted(folder.glob('demo-*.html'))]
    links = []
    previews = []
    for relative in paths:
        resolved = _artifact(folder, relative)
        if resolved is None:
            continue
        _, label, mime = resolved
        url = f'/runs/{run_id}/files/{quote(relative)}'
        link = f'<a href="{url}">{_text(label)}</a>'
        if DEMO_FILE.fullmatch(relative):
            previews.append(link)
        else:
            links.append(link)
    if previews:
        links.append(f'<details><summary>Демонстрации · {len(previews)}</summary>'
                     f'<div class="jump">{"".join(previews)}</div></details>')
    return ('<div class="jump">' + ''.join(links) + '</div>'
            if links else '<span class="subtle">Материалы ещё недоступны</span>')


def _table(headers, rows, empty):
    if not rows:
        return f'<p class="empty">{_text(empty)}</p>'
    columns = ''.join(f'<th scope="col">{_text(label)}</th>' for label in headers)
    return (f'<div class="table-scroll" tabindex="0"><table><thead><tr>{columns}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _counters(events, pairs):
    return '<div class="kpis">' + ''.join(
        f'<div class="kpi"><strong>{int(events.get(event, 0))}</strong><span>{_text(label)}</span></div>'
        for event, label in pairs
    ) + '</div>'


def _page(title, content):
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_text(title)} · CRM Consulting</title><style>{STYLE}</style></head>
<body><header><div class="wrap bar"><span class="wordmark">CRM Consulting</span>
<span class="read-only">Локальная панель · Только чтение</span></div></header>
<main class="wrap">{content}</main></body></html>"""


def _render(stats, root):
    events = stats['events']
    stopped = stats['stopped']
    states = Counter(run['status'] for run in stats['runs'])
    voice_simulated = stats.get('simulated_dialogues', 0)
    stage_counts = _counters(stats.get('stage_events', events), (
        ('commercial_researcher', 'Исследовано'), ('qualification_analyst', 'Квалификация'),
        ('direct_response_marketer', 'Черновики'), ('visual_creative', 'Визуальные материалы'),
        ('communication_specialist', 'Сценарии'), ('sales_learning_analyst', 'Анализ результатов'),
    ))
    observed = _counters(events, tuple(EVENT_NAMES.items())[:6])
    state = 'Кампания остановлена' if stopped else 'Обработка доступна'
    state_class = 'state pause' if stopped else 'state'
    runs = _table(
        ('Запуск', 'Тип', 'Состояние', 'Создан UTC', 'Материалы'),
        [f'<tr><th class="runid" scope="row">№{int(r["id"])}</th>'
         f'<td>{_text(ORIGIN.get(r["origin"], r["origin"]))}</td>'
         f'<td>{_text(RUN_STATE.get(r["status"], r["status"]))}</td>'
         f'<td>{_text(r["created"][:16].replace("T", " "))}</td>'
         f'<td>{_links(root, int(r["id"]))}</td></tr>' for r in stats['runs']],
        'Очередь пуста. Запуски появятся после исследования или учебного прогона.',
    )
    portfolio = _table(
        ('Компания', 'Группа', 'Продукт', 'Запуск'),
        [f'<tr><th scope="row">{_text(r["company"])}</th>'
         f'<td>{_text(GROUPS.get(str(r["group"]).lower(), r["group"]))}</td>'
         f'<td class="product">{_text(PRODUCTS.get(r.get("product"), "—"))}</td>'
         f'<td class="runid">№{int(r["run"])}</td></tr>' for r in stats['portfolio']],
        'После завершения квалификации здесь появятся компании.',
    )
    metrics = _table(
        ('Вариант', 'Язык', 'Сегмент', 'Событие', 'Компании'),
        [f'<tr><td>{_text(m["variant"] or "—")}</td><td>{_text(m["language"] or "—")}</td>'
         f'<td>{_text(m["segment"] or "—")}</td><td>{_text(EVENT_NAMES.get(m["kind"], m["kind"]))}</td>'
         f'<td>{int(m["n"])}</td></tr>' for m in stats.get('metrics', ())],
        'Зафиксированных событий кампании пока нет.',
    )
    body = f"""
<div class="heading"><div><h1>Работа с компаниями</h1>
<p class="{state_class}">{state} · {states['queued']} в очереди · {states['running']} в работе</p></div>
<a class="refresh" href="/">Обновить</a></div>
<nav aria-label="Разделы"><a href="#runs">Запуски</a><a href="#portfolio">Компании</a>
<a href="#events">События по группам</a></nav>
<div class="blocks">
<section class="panel"><h2>Подготовка</h2>{stage_counts}
<p class="subtle">Тестовые голосовые диалоги · {int(voice_simulated)}</p></section>
<section class="panel experiment"><h2>Подтверждённые события</h2>{observed}</section>
</div>
<section class="section" id="runs"><h2>Последние запуски</h2>{runs}</section>
<section class="section" id="portfolio"><h2>Компании и решения</h2>{portfolio}</section>
<section class="section" id="events"><h2>События по группам</h2>{metrics}</section>"""
    return _page('Панель продаж', body)


def create_dashboard(store, output_root):
    root = Path(output_root).resolve()
    app = FastAPI(title='CRM Consulting — панель продаж', openapi_url=None,
                  docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'],
                       www_redirect=False)

    @app.get('/', response_class=HTMLResponse)
    def dashboard():
        try:
            stats = store.stats()
        except sqlite3.Error:
            content = ('<h1>Панель временно недоступна</h1>'
                       '<p>Не удалось прочитать журнал. Обновите страницу.</p>'
                       '<a class="refresh" href="/">Повторить</a>')
            return HTMLResponse(_page('Журнал недоступен', content), status_code=503, headers=HEADERS)
        return HTMLResponse(_render(stats, root), headers=HEADERS)

    @app.get('/runs/{run_id}/files/{relative:path}')
    def artifact(run_id: int, relative: str, download: bool = False):
        if run_id < 1:
            raise HTTPException(404, 'Материал отсутствует')
        resolved = _artifact(_run_folder(root, run_id), relative)
        if resolved is None:
            raise HTTPException(404, 'Материал отсутствует')
        path, _, mime = resolved
        headers = dict(HEADERS)
        preview = mime.startswith('text/html') and not download
        if preview:
            headers['Content-Security-Policy'] += '; sandbox'
        return FileResponse(path, media_type=mime, filename=path.name, headers=headers,
                            content_disposition_type='inline' if preview else 'attachment')

    return app
