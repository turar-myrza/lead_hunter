"""Category-separated, local v1.2 reports. No network access or CRM mutations."""
import csv
from collections import Counter
from datetime import datetime, timezone
from io import StringIO
import json

from app.services.lead_hunter import safe_cell
from app.services.lead_hunter_routing import route
from app.services.lead_hunter_scoring import usable_contact

CATEGORIES={'A':'NEW CRM','B':'EXISTING BITRIX24','C':'CRM CONSULTING CUSTOMERS','D':'EXCLUDED / HOLD'}


def current_contact(candidate, contact, now):
    source=next(s for s in candidate.sources if s.id==contact.evidence.source_id)
    return usable_contact(candidate,contact,now) and (source.valid_until is None or source.valid_until>=now.astimezone(source.checked_at.tzinfo).date())


def decisions(batch, as_of=None):
    now=as_of or datetime.now(timezone.utc)
    pairs=[(c,route(c,now)) for c in batch.candidates]
    rank={'high':0,'medium':1,'low':2,'hold':3}
    return sorted(pairs,key=lambda p:(p[1].category,rank[p[1].commercial_priority],-(p[1].offer_prospect_score or 0),-(p[1].need_score or 0),p[0].name))


def csv_v12(batch, category=None, as_of=None):
    now=as_of or datetime.now(timezone.utc)
    stream=StringIO(newline='')
    fields=['COMPANY','CATEGORY','CATEGORY_LABEL','SECTOR','CRM_STATUS','RELATIONSHIP_STATE',
      'NEED_PROFILE','NEED_SCORE','OFFER_PROSPECT_SCORE','CONTACT_SCORE','PURCHASE_INTEREST',
      'CONTACT_PERMISSIBILITY','COMMERCIAL_PRIORITY','ACTIVE_RECOMMENDATION','PUBLIC_CONTACTS',
      'CONFIRMED_SIGNALS','PRODUCT_HYPOTHESIS','REASON_TO_CONTACT','RECOMMENDED_CHANNEL',
      'NEXT_STEP','OWNER','LAST_INTERACTION','LAST_CRM_EVENT','HISTORY_REVIEW','DECISION_RATIONALE','LIMITATIONS','SOURCES_AND_FRESHNESS']
    writer=csv.DictWriter(stream,fields,delimiter=';');writer.writeheader()
    for c,d in decisions(batch,now):
        if category and d.category!=category: continue
        contacts=[x for x in c.contacts if current_contact(c,x,now)]
        channel=('email' if any(x.type=='email' for x in contacts) else 'звонок' if any(x.type=='phone' for x in contacts) else 'не установлен') if d.active_task else 'только внутренняя проверка'
        r=c.relationship_review
        proposal=c.commercial_proposal
        row=dict(COMPANY=c.name,CATEGORY=d.category,CATEGORY_LABEL=CATEGORIES[d.category],SECTOR=c.sector,
          CRM_STATUS=d.crm_status,RELATIONSHIP_STATE=r.relationship_state,NEED_PROFILE=d.need_profile,
          NEED_SCORE=d.need_score if d.need_score is not None else '',OFFER_PROSPECT_SCORE=d.offer_prospect_score if d.offer_prospect_score is not None else '',
          CONTACT_SCORE=d.contact_score,PURCHASE_INTEREST=d.purchase_interest,CONTACT_PERMISSIBILITY=d.contact_permissibility,
          COMMERCIAL_PRIORITY=d.commercial_priority,ACTIVE_RECOMMENDATION=str(d.active_task).lower(),
          PUBLIC_CONTACTS='; '.join(x.value for x in contacts),CONFIRMED_SIGNALS='; '.join(e.fact+f' [{e.source_id}]' for e in c.evidence),
          PRODUCT_HYPOTHESIS=proposal.concrete_offer if proposal and d.active_task else 'Не допущена к коммерческому обращению' if not d.active_task else 'Требует выбора конкретного предложения',
          REASON_TO_CONTACT=proposal.confirmed_fact.fact if proposal and d.active_task else 'Сначала выполнить внутреннюю проверку',
          RECOMMENDED_CHANNEL=channel,NEXT_STEP=d.next_step,OWNER=r.owner_name or '',LAST_INTERACTION=r.last_interaction_at.isoformat() if r.last_interaction_at else '',
          LAST_CRM_EVENT=r.last_event_at.isoformat() if r.last_event_at else '',
          HISTORY_REVIEW=json.dumps(r.model_dump(mode='json',exclude={'entities'}),ensure_ascii=False),
          DECISION_RATIONALE=' | '.join(d.reasons+d.need_rationale+d.offer_rationale),
          LIMITATIONS=' | '.join(c.limitations+r.limitations+d.limitations),
          SOURCES_AND_FRESHNESS='; '.join(f'{s.id}: {s.url}; publication={s.published_on or "unknown"}; checked={s.checked_at.isoformat()}; status={s.vacancy_status}; valid_until={s.valid_until or "unknown"}; last_attempt={s.last_attempt_status}' for s in c.sources))
        writer.writerow({k:safe_cell(v) for k,v in row.items()})
    return stream.getvalue()


def report_v12(batch, as_of=None):
    now=as_of or datetime.now(timezone.utc)
    pairs=decisions(batch,now);counts=Counter(d.category for _,d in pairs)
    esc=lambda x:str(x).replace('|','\\|').replace('\n',' ')
    lines=['# AI Lead Hunter v1.2 — классификация до коммерческой рекомендации','',
      f'Проверены прежние {len(pairs)} компаний. A: {counts["A"]}; B: {counts["B"]}; C: {counts["C"]}; D: {counts["D"]}.',
      f'Дата решения: {now.isoformat()}. Приоритеты и баллы разных профилей не сравниваются как одна шкала.',
      'NEED — потенциальная потребность в рамках категории; OFFER — обоснованность конкретного предложения, не вероятность покупки; CONTACT — доступность опубликованного канала. Допуск к подготовке черновика не означает согласия клиента или разрешения отправить сообщение.',
      'История имеет приоритет над оценкой. Неустановленная CRM не означает отсутствие CRM. Отсутствие видимых записей не исключает скрытые правами записи.','']
    if not counts['C']:
        lines += ['В этом проходе нет компаний с достаточно выясненной историей для категории C. Известные CRM-отношения не потеряны: они показаны в D/HOLD с отдельным статусом отношений и причинами блокировки. Это не означает, что существующих клиентов нет.','']
    for cat,label in CATEGORIES.items():
        lines += [f'## {cat}. {label}','',
          '| Компания / сфера | Сигналы / актуальность | CRM / отношения | Продукт / повод | NEED / OFFER / CONTACT | Приоритет / допуск | Публичный канал | Следующий шаг |',
          '|---|---|---|---|---|---|---|---|']
        entries=[(c,d) for c,d in pairs if d.category==cat]
        if not entries: lines += ['| Нет компаний в этой категории | — | — | — | — | — | — | — |']
        for c,d in entries:
            sm={s.id:s for s in c.sources}
            signals='<br>'.join(f'{esc(e.fact)} ([{e.source_id}]({sm[e.source_id].url}); публикация {sm[e.source_id].published_on or "неизвестна"}; проверено {sm[e.source_id].checked_at.date()}; статус {sm[e.source_id].vacancy_status}; срок {sm[e.source_id].valid_until or "не указан"})' for e in c.evidence)
            contacts='<br>'.join(esc(x.value) for x in c.contacts if current_contact(c,x,now)) or 'Не подтверждён'
            p=c.commercial_proposal
            offer=esc(p.concrete_offer) if p and d.active_task else 'Коммерческое предложение не допущено' if not d.active_task else 'Требуется выбрать предложение'
            vals=[c.name+' / '+c.sector,signals,d.crm_status+' / '+c.relationship_review.relationship_state,offer,
              f'{d.need_score if d.need_score is not None else "—"} / {d.offer_prospect_score if d.offer_prospect_score is not None else "—"} / {d.contact_score}',
              d.commercial_priority+' / '+d.contact_permissibility,contacts,d.next_step]
            lines.append('| '+' | '.join(esc(v) if i not in {1,6} else v for i,v in enumerate(vals))+' |')
        lines.append('')
    lines += ['## Обоснования и история','']
    for c,d in pairs:
        r=c.relationship_review
        lines += [f'### {c.name}','',f'Категория {d.category}; {d.contact_permissibility}; профиль {d.need_profile}; отношения {r.relationship_state}; полнота истории {r.status}; идентичность {r.identity}.',
          f'Ответственный: {r.owner_name or "не установлен"}; последний подтверждённый содержательный контакт: {r.last_interaction_at or "не установлен"}; последнее доступное CRM-событие: {r.last_event_at or "не установлено"}.']
        lines += ['- '+x for x in d.reasons+d.need_rationale+d.offer_rationale]
        for label,facts in [('Факты отношений',r.facts),('Доказанные выполненные работы',r.performed_services),('Отклонённые предложения',r.declined_offers),('Новые запросы',r.new_requests)]:
            lines.append(f'**{label}:**')
            lines.extend('- '+f.text+f' [{f.reference.entity} #{f.reference.id}, {f.reference.field}, {f.reference.date or "дата неизвестна"}, {f.confidence}]' for f in facts)
            if not facts: lines.append('- Не установлены по доступным доказательствам; отсутствие записей не доказывает отсутствие работ/запросов.')
        for entity in r.entities:
            lines += [f'- CRM {entity.entity} #{entity.id}: {entity.title}; связь {entity.identity}; стадия {entity.status_name or entity.status_code or "не установлена"}; ответственный {entity.owner_name or entity.owner_id or "не установлен"}.']
        lines += ['- Ограничение: '+x for x in r.limitations+d.limitations+c.limitations]
        lines.append('')
    lines += ['## READ ONLY','',json.dumps(batch.crm_read_audit,ensure_ascii=False),
       'Ни одного создания/изменения CRM, отправки сообщения или звонка. Пакет содержит частные CRM-сведения; только локальная работа руководителя.','']
    return '\n'.join(lines)
