"""Deterministic qualification, duplicate rejection, executive report and CSV."""

import csv
from collections import Counter
from io import StringIO
import re
from urllib.parse import urlsplit

from app.models.lead_hunter import Candidate, ResearchBatch, WEIGHTS


def qualify(candidate: Candidate) -> dict:
    confirmed = {e.criterion for e in candidate.evidence if e.criterion is not None}
    breakdown = {c.value: w if c in confirmed else 0 for c, w in WEIGHTS.items()}
    score = sum(breakdown.values())
    return {'score': score, 'priority': 'A' if score >= 70 else 'B' if score >= 40 else 'C',
            'breakdown': breakdown}


def validate_batch_identity(batch: ResearchBatch) -> None:
    """Reject repeated candidate identities without calculating any score."""
    names, urls, contacts = set(), set(), set()
    for c in batch.candidates:
        name = re.sub(r'[^\w]', '', c.name.casefold())
        parsed = urlsplit(c.website)
        url = (parsed.netloc.lower().removeprefix('www.'), parsed.path.rstrip('/'))
        keys = {(x.type, re.sub(r'\D', '', x.value) if x.type == 'phone' else x.value.casefold())
                for x in c.contacts if x.type != 'public_page'}
        if name in names or url in urls or keys & contacts:
            raise ValueError(f'Possible duplicate company: {c.name}; resolve identity before export')
        names.add(name)
        urls.add(url)
        contacts.update(keys)


def ranked(batch: ResearchBatch) -> list[Candidate]:
    validate_batch_identity(batch)
    return sorted(batch.candidates, key=lambda c: (-qualify(c)['score'], c.name.casefold()))


def safe_cell(value: object) -> str:
    text = str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text


def csv_export(batch: ResearchBatch) -> str:
    stream = StringIO(newline='')
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(['TITLE', 'COMPANY_TITLE', 'ADDRESS_CITY', 'WEB_WORK', 'PHONE_WORK',
                     'EMAIL_WORK', 'COMMENTS', 'PRIORITY', 'SCORE', 'SECTOR', 'PRODUCTS',
                     'HYPOTHESIS', 'EVIDENCE_URLS', 'CHECKED_AT', 'SCORE_BREAKDOWN'])
    for c in ranked(batch):
        q = qualify(c)
        facts = '; '.join(f'{e.fact} [{e.source_id}]' for e in c.evidence)
        sources = '; '.join(f'{s.id}: {s.url}' for s in c.sources)
        notes = ' | '.join(c.limitations + [s.limitation for s in c.sources if s.limitation])
        comments = (f'AI Lead Hunter: {q["priority"]}, {q["score"]}/100. ФАКТЫ: {facts}. '
                    f'ПРОДУКТЫ (гипотеза): {", ".join(c.products)}. ГИПОТЕЗА: {c.hypothesis}. '
                    f'ИСТОЧНИКИ: {sources}. ОГРАНИЧЕНИЯ: {notes or "Не указаны"}')
        values = [f'Lead Hunter — {c.name}', c.name, c.city, c.website,
                  ', '.join(re.sub(r'\D', '', x.value) for x in c.contacts if x.type == 'phone'),
                  ', '.join(x.value for x in c.contacts if x.type == 'email'), comments,
                  q['priority'], q['score'], c.sector, ', '.join(c.products), c.hypothesis,
                  sources, max(s.checked_at for s in c.sources).isoformat(),
                  ', '.join(f'{k}={v}' for k, v in q['breakdown'].items())]
        writer.writerow([safe_cell(v) for v in values])
    return stream.getvalue()


def markdown_report(batch: ResearchBatch) -> str:
    candidates = ranked(batch)
    counts = Counter(qualify(c)['priority'] for c in candidates)
    lines = ['# AI Lead Hunter v1.0 — Шымкент', '',
             f'Найдено: {len(candidates)}. A: {counts["A"]}; B: {counts["B"]}; C: {counts["C"]}.', '',
             '## Рекомендуемый порядок первых разговоров', '']
    for c in candidates[:5]:
        q = qualify(c)
        lines.append(f'- **{c.name} — {q["priority"]}, {q["score"]}/100.** ' + '; '.join(e.fact for e in c.evidence if e.criterion))
    lines += ['', 'Продукты и гипотезы ниже требуют проверки в разговоре. Балл не доказывает готовность купить.', '',
              '| Компания / сфера | Источники и факты | Продукты (гипотеза) | Приоритет / баллы | Деловой контакт | Гипотеза первого разговора |',
              '|---|---|---|---|---|---|']
    escape = lambda x: str(x).replace('|', '\\|').replace('\n', ' ')
    for c in candidates:
        q = qualify(c)
        sources = {s.id: s for s in c.sources}
        facts = '<br>'.join(f'{escape(e.fact)} ([источник]({sources[e.source_id].url}))' for e in c.evidence)
        contact = '<br>'.join(escape(x.value) + f' ([источник]({sources[x.evidence.source_id].url}))' for x in c.contacts) or 'Не найден'
        lines.append('| ' + ' | '.join([escape(c.name + ' / ' + c.sector), facts,
                     escape(', '.join(c.products)), f'{q["priority"]} / {q["score"]}', contact, escape(c.hypothesis)]) + ' |')
    lines += ['', '## Расчёт и ограничения', '',
              'Критерии: поток — 25; несколько менеджеров — 20; каналы — 20; рост/проблемы — 20; CRM — 15. A ≥70; B ≥40; C <40.', '',
              'Вакансия сама по себе не доказывает рост или несколько менеджеров; филиалы не дают дополнительных баллов. '
              'Наличие CRM не доказывает неисправность и не означает запрос на замену. '
              'Даты проверки означают чтение публичного источника, а не независимое подтверждение заявлений компании.', '']
    for c in candidates:
        q = qualify(c)
        lines.append(f'- **{c.name}:** ' + ', '.join(f'{k}: {v}' for k, v in q['breakdown'].items()))
        lines.extend(f'  - {note}' for note in c.limitations)
        for s in c.sources:
            lines.append(f'  - [{s.title}]({s.url}); проверено {s.checked_at.isoformat()}; доступ: {s.access}; публикация: {s.published_on or "не указана"}.' + (f' {s.limitation}' if s.limitation else ''))
    lines += ['', '## Журнал поиска', ''] + ['- ' + x for x in batch.search_log]
    lines += ['', '## Пропуски и ограничения доступа', ''] + ['- ' + x for x in batch.skipped_sources]
    return '\n'.join(lines) + '\n'
