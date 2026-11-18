"""v1.1 managerial report and separate review/import CSVs."""
import csv
from collections import Counter
from datetime import datetime, timezone
from io import StringIO
import json

from app.services.lead_hunter import safe_cell
from app.services.lead_hunter_scoring import assess, freshness, usable_contact
from app.services.lead_hunter_duplicates import normalize_phone

CONTACT_LABELS = {'responsible_person': 'Опубликован ответственный', 'company_channel': 'Канал компании',
                  'website_only': 'Только сайт', 'not_found': 'Не найден'}
CRM_LABELS = {'not_checked': 'Не проверено', 'unavailable': 'Проверка недоступна', 'incomplete': 'Проверка неполная',
              'confirmed_duplicate': 'Подтверждённый дубль', 'possible_match': 'Возможное совпадение',
              'no_match_accessible': 'В доступных записях совпадений нет'}


def assessments(batch, as_of=None):
    now = as_of or datetime.now(timezone.utc)
    assessed = [(c, assess(c, now)) for c in batch.candidates]
    return sorted(assessed, key=lambda pair: (-pair[1]['score'], -pair[1]['contact_score'], pair[0].name.casefold()))


def match_summary(c):
    parts = []
    for m in c.crm_check.matches:
        responsible = m.responsible_name or (f'ID {m.responsible_id}' if m.responsible_id else 'Не указан/недоступен')
        parts.append(f'{m.entity} #{m.id} {m.name}; {m.confidence}; ответственный: {responsible}; статус/тип: {m.status or "Не указан"}; ' + '; '.join(m.reasons))
    return ' | '.join(parts)


def csv_v11(batch, *, import_only=False, as_of=None):
    now = as_of or datetime.now(timezone.utc)
    stream = StringIO(newline='')
    columns = ['TITLE', 'COMPANY_TITLE', 'ADDRESS_CITY', 'WEB_WORK', 'PHONE_WORK', 'EMAIL_WORK', 'COMMENTS',
               'NEED_PRIORITY', 'NEED_SCORE', 'CONTACT_SCORE', 'CONTACT_AVAILABILITY', 'PURCHASE_INTEREST',
               'CRM_CHECK', 'IMPORT_READY', 'CRM_MATCHES', 'CRM_HISTORY', 'NEXT_STEP', 'PUBLIC_RESPONSIBLES', 'PRODUCTS', 'SECTOR',
               'HYPOTHESIS', 'SCORE_RATIONALE', 'EVIDENCE_URLS', 'SOURCE_FRESHNESS', 'CRM_CHECKED_AT', 'ASSESSED_AT']
    writer = csv.DictWriter(stream, fieldnames=columns, delimiter=';')
    writer.writeheader()
    for c, q in assessments(batch, now):
        if import_only and not q['import_ready']:
            continue
        contacts = [x for x in c.contacts if usable_contact(c, x, now)]
        sites = [x.value for x in contacts if x.type == 'website']
        phones = [normalize_phone(x.value) for x in contacts if x.type == 'phone']
        sources = '; '.join(f'{s.id}: {s.url}' for s in c.sources)
        source_status = '; '.join(f'{s.id}: публикация={s.published_on or "неизвестна"}, проверка={s.checked_at.isoformat()}, '
                                 f'статус={s.vacancy_status}, доступ={s.last_attempt_status}, {freshness(s, now)[1]}' for s in c.sources)
        history = json.dumps([{'entity': m.entity, 'id': m.id, 'history': m.history, 'related_deals': m.related_deals,
                              'limitations': m.limitations} for m in c.crm_check.matches], ensure_ascii=False)
        facts = '; '.join(f'{e.fact} [{e.source_id}]' for e in c.evidence)
        row = dict(TITLE=f'Lead Hunter — {c.name}', COMPANY_TITLE=c.name, ADDRESS_CITY=c.city,
                   WEB_WORK=', '.join(sites), PHONE_WORK=', '.join(p for p in phones if p),
                   EMAIL_WORK=', '.join(x.value for x in contacts if x.type == 'email'),
                   NEED_PRIORITY=q['priority'], NEED_SCORE=q['score'], CONTACT_SCORE=q['contact_score'],
                   CONTACT_AVAILABILITY=CONTACT_LABELS[q['contact_level']], PURCHASE_INTEREST='Публичный запрос' if q['purchase_interest'] == 'public_request' else 'Не подтверждён',
                   CRM_CHECK=CRM_LABELS[c.crm_check.state], IMPORT_READY=str(q['import_ready']).lower(),
                   CRM_MATCHES=match_summary(c), CRM_HISTORY=history, NEXT_STEP=q['next_step'],
                   PUBLIC_RESPONSIBLES='; '.join(f'{p.name} — {p.role} ({p.scope}); нет подтверждённого прямого канала [{p.evidence.source_id}]' for p in c.public_responsibles),
                   PRODUCTS='; '.join(c.products), SECTOR=c.sector, HYPOTHESIS=c.hypothesis,
                   SCORE_RATIONALE=' | '.join(q['rationale']), EVIDENCE_URLS=sources, SOURCE_FRESHNESS=source_status,
                   CRM_CHECKED_AT=c.crm_check.checked_at.isoformat() if c.crm_check.checked_at else '', ASSESSED_AT=q['as_of'])
        notes = ' | '.join(c.limitations + [s.limitation for s in c.sources if s.limitation] + c.crm_check.limitations)
        row['COMMENTS'] = (f'AI Lead Hunter 1.1. Потенциальная потребность: {q["score"]}/100 ({q["priority"]}); '
            f'контакт: {q["contact_score"]}/100; интерес к покупке: {row["PURCHASE_INTEREST"]}; '
            f'CRM: {row["CRM_CHECK"]}; ответственный/статус: {row["CRM_MATCHES"]}; '
            f'ФАКТЫ: {facts}; ОБОСНОВАНИЕ: {row["SCORE_RATIONALE"]}; '
            f'ПРОДУКТЫ (гипотеза): {row["PRODUCTS"]}; СЛЕДУЮЩИЙ ШАГ: {q["next_step"]}; '
            f'ИСТОЧНИКИ: {sources}; ОГРАНИЧЕНИЯ: {notes}')
        writer.writerow({k: safe_cell(v) for k, v in row.items()})
    return stream.getvalue()


def report_v11(batch, as_of=None):
    now = as_of or datetime.now(timezone.utc)
    entries = assessments(batch, now)
    counts = Counter(q['priority'] for _, q in entries)
    duplicates = sum(c.crm_check.state == 'confirmed_duplicate' for c, _ in entries)
    ready = sum(q['import_ready'] for _, q in entries)
    esc = lambda v: str(v).replace('|', '\\|').replace('\n', ' ')
    title = 'расширенный поиск компаний Шымкента' if batch.mode == 'expanded' else 'повторная проверка пяти компаний Шымкента'
    lines = [f'# AI Lead Hunter v1.1 — {title}', '',
             f'Дата расчёта: {now.isoformat()}. Компаний: {len(entries)}. A: {counts["A"]}; B: {counts["B"]}; C: {counts["C"]}.', '',
             f'Подтверждённых дублей: {duplicates}; готово к возможному ручному импорту: {ready}. '
             'Отсутствие совпадений относится только к доступным владельцу вебхука записям.', '',
             'Приоритет отражает потенциальную потребность. Доступность контакта и публичный интерес к покупке оцениваются отдельно. '
             'Никаких сообщений, звонков и изменений CRM агент не выполняет.', '',
             '| Компания / сфера | Факты и источники | Потребность / контакт | Интерес к покупке | Контакты | Проверка CRM | Следующий шаг менеджера |',
             '|---|---|---|---|---|---|---|']
    for c, q in entries:
        src = {s.id: s for s in c.sources}
        facts = '<br>'.join(f'{esc(e.fact)} ([источник]({src[e.source_id].url}))' for e in c.evidence)
        contacts = '<br>'.join(f'{esc(x.value)} — {x.role}' + (f' ({esc(x.person_name)})' if x.person_name else '') + f' ([источник]({src[x.evidence.source_id].url}))' for x in c.contacts if usable_contact(c, x, now)) or 'Не подтверждены'
        crm = CRM_LABELS[c.crm_check.state] + ('<br>' + esc(match_summary(c)) if c.crm_check.matches else '')
        lines.append('| ' + ' | '.join([esc(c.name + ' / ' + c.sector), facts,
                     f'{q["priority"]}: {q["score"]}/100; контакт {q["contact_score"]}/100',
                     'Публичный запрос' if q['purchase_interest'] == 'public_request' else 'Не подтверждён', contacts, crm, esc(q['next_step'])]) + ' |')
    lines += ['', '## Почему изменились оценки', '',
              'Весовые максимумы: конкретная проблема обработки — 30; явно описанный проект автоматизации — 25; '
              'расширение — 15; постоянный/измеренный поток — 10; команда — 5; каналы — 5; сложность товарного заказа — 5; действующая CRM — 5. '
              'Каждый сигнал начисляется однократно по сильнейшему актуальному доказательству. '
              'A: от 65 и есть конкретная проблема/проект; B: от 35; C: ниже 35. '
              'Вакансия РОП или менеджера не является запросом купить CRM.', '',
              'Вакансия: до 30 дней ×1, 31–90 ×0.5, старше 90 ×0. Неизвестный статус ×0.5, архив/закрытие ×0, неизвестная дата ×0. '
              'Последняя проверка старше 30 дней или неуспешная последняя попытка дают ×0. Общесетевой сигнал ×0.5. '
              'Недатированная контактная страница может подтверждать контакт, но не текущий проект/рост/проблему.', '',
              'Контакт: официальный сайт 15; телефон компании 30; деловой email 20; явно опубликованный контакт ответственного 35. '
              'Email на Gmail/Mail.ru допустим как опубликованный деловой, но не называется корпоративным доменом.', '',
              f'review.csv содержит все {len(entries)} записей и результаты CRM. companies.csv содержит только записи с полной доступной проверкой '
              'не старше 24 часов, без совпадений и с доступным телефоном/email. Это не разрешение на импорт и не проверка всех скрытых CRM-записей.', '']
    for c, q in entries:
        lines += [f'## {c.name}', '', '**Обоснование:**'] + ['- ' + r for r in q['rationale']]
        lines += ['', '**Продукты (гипотеза):** ' + '; '.join(c.products), '', '**Актуальность:**']
        for p in c.public_responsibles:
            lines.append(f'- Опубликован ответственный: {p.name} — {p.role} ({p.scope}), [{p.evidence.source_id}]({next(s.url for s in c.sources if s.id == p.evidence.source_id)}). Прямой телефон/email не установлен; имя не увеличивает оценку доступности контакта.')
        for s in c.sources:
            lines.append(f'- [{s.id}]({s.url}): публикация {s.published_on or "неизвестна"}; успешно проверен {s.checked_at.isoformat()}; '
                         f'статус вакансии {s.vacancy_status}; последняя попытка {s.last_attempt_at or s.checked_at} ({s.last_attempt_status}); {freshness(s, now)[1]}.' + (f' {s.limitation}' if s.limitation else ''))
        lines += ['', '**CRM:** ' + CRM_LABELS[c.crm_check.state],
                  f'- Проверено: {c.crm_check.checked_at or "не проверено"}; полнота: {c.crm_check.coverage}; просмотрено: {c.crm_check.scanned}.']
        for m in c.crm_check.matches:
            lines += [f'- {m.entity} #{m.id}: {m.name}; {m.confidence}; ответственный {m.responsible_name or m.responsible_id or "недоступен"}; статус/тип {m.status or "недоступен"}.']
            lines += ['  - ' + reason for reason in m.reasons]
            lines += ['  - Дело: ' + json.dumps(h, ensure_ascii=False) for h in m.history]
            lines += ['  - Сделка: ' + json.dumps(d, ensure_ascii=False) for d in m.related_deals]
            lines += ['  - ' + note for note in m.limitations]
        lines += ['- ' + note for note in c.crm_check.limitations + c.limitations]
    lines += ['', '**Аудит READ ONLY:** ' + json.dumps(batch.crm_read_audit, ensure_ascii=False), '', '## Журнал исследования', ''] + ['- ' + x for x in batch.search_log]
    lines += ['', '## Недоступные и отклонённые источники', ''] + ['- ' + x for x in batch.skipped_sources]
    return '\n'.join(lines) + '\n'
