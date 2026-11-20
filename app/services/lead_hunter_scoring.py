"""v1.1: independent need, reachability and public purchase-interest assessments."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import re

from app.models.lead_hunter import CommercialSignal

WEIGHTS_V11 = {
    CommercialSignal.PROCESSING_PROBLEM: 30,
    CommercialSignal.AUTOMATION_PROJECT: 25,
    CommercialSignal.GROWTH: 15,
    CommercialSignal.INBOUND: 10,
    CommercialSignal.TEAM: 5,
    CommercialSignal.CHANNELS: 5,
    CommercialSignal.ORDER_COMPLEXITY: 5,
    CommercialSignal.EXISTING_CRM: 5,
}


def freshness(source, as_of: datetime) -> tuple[Decimal, str]:
    """Conservative, deterministic age/status policy. No guessed publication date."""
    local_day = as_of.astimezone(source.checked_at.tzinfo).date()
    age_check = (local_day - source.checked_at.date()).days
    if as_of < source.checked_at:
        return Decimal('0'), 'Проверка позже даты расчёта'
    if source.last_attempt_at and as_of < source.last_attempt_at:
        return Decimal('0'), 'Последняя попытка позже даты расчёта'
    if source.valid_until and source.valid_until < local_day:
        return Decimal('0'), 'Срок действия источника истёк'
    if source.access != 'public_page' or source.last_attempt_status != 'success':
        return Decimal('0'), 'Источник не прочитан при последней попытке'
    if age_check > 30:
        return Decimal('0'), 'Последняя успешная проверка старше 30 дней'
    if source.kind == 'vacancy':
        if source.vacancy_status in {'archived', 'closed'}:
            return Decimal('0'), 'Вакансия закрыта/архивная'
        if source.published_on is None:
            return Decimal('0'), 'У вакансии неизвестна дата публикации'
        age = (local_day - source.published_on).days
        if age < 0 or age > 90:
            return Decimal('0'), 'Вакансия старше 90 дней либо дата некорректна'
        age_factor = Decimal('1') if age <= 30 else Decimal('.5')
        status_factor = Decimal('1') if source.vacancy_status == 'active' else Decimal('.5')
        factor = age_factor * status_factor
        return factor, f'Вакансия: возраст {age} дн.; статус {source.vacancy_status}; коэффициент {factor}'
    # A newly checked company contact page may legitimately be undated.
    return Decimal('1'), f'Открытая страница проверена {age_check} дн. назад; дата публикации ' + (str(source.published_on) if source.published_on else 'не указана')


def usable_contact(candidate, contact, as_of):
    from app.services.lead_hunter_duplicates import normalize_phone
    sources = {s.id: s for s in candidate.sources}
    source = sources[contact.evidence.source_id]
    factor, _ = freshness(source, as_of)
    if not factor or contact.role == 'recruitment':
        return False
    if contact.type == 'phone' and normalize_phone(contact.value) is None:
        return False
    if contact.type == 'email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', contact.value):
        return False
    if contact.role_evidence:
        factor, _ = freshness(sources[contact.role_evidence.source_id], as_of)
    return bool(factor)


def assess(candidate, as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError('as_of must be timezone-aware')
    sources = {s.id: s for s in candidate.sources}
    breakdown = {}
    rationale = []
    for signal, weight in WEIGHTS_V11.items():
        options = []
        for e in candidate.evidence:
            if e.signal != signal:
                continue
            factor, reason = freshness(sources[e.source_id], as_of)
            if e.scope == 'network':
                factor *= Decimal('.5')
                reason += '; общесетевые сведения ×0.5'
            if sources[e.source_id].kind != 'vacancy' and not sources[e.source_id].published_on and signal in {
                    CommercialSignal.GROWTH, CommercialSignal.PROCESSING_PROBLEM, CommercialSignal.AUTOMATION_PROJECT}:
                factor = Decimal('0')
                reason += '; событие/проект без даты не доказывает текущий спрос'
            points = int((Decimal(weight) * factor).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            options.append((points, e.source_id, e.fact, reason))
        best = max(options, default=(0, None, 'Нет подтверждения', 'Баллы не начислены'), key=lambda x: x[0])
        breakdown[signal.value] = {'points': best[0], 'max': weight, 'source_id': best[1], 'fact': best[2], 'freshness': best[3]}
        if options:
            rationale.append(f'{signal.value}: {best[0]}/{weight}. {best[2]}. {best[3]}')
    score = sum(x['points'] for x in breakdown.values())
    strong = bool(breakdown['processing_problem']['points'] or breakdown['automation_project']['points'])
    priority = 'A' if score >= 65 and strong else 'B' if score >= 35 else 'C'
    contacts = [c for c in candidate.contacts if usable_contact(candidate, c, as_of)]
    website = any(c.type == 'website' for c in contacts)
    phone = any(c.type == 'phone' for c in contacts)
    email = any(c.type == 'email' for c in contacts)
    responsible = any(c.type in {'phone', 'email'} and c.role in {'sales', 'decision_maker'} for c in contacts)
    contact_score = 15 * website + 30 * phone + 20 * email + 35 * responsible
    contact_level = 'responsible_person' if responsible else 'company_channel' if phone or email else 'website_only' if website else 'not_found'
    purchase = []
    for e in candidate.purchase_evidence:
        source = sources[e.source_id]
        day = as_of.astimezone(source.checked_at.tzinfo).date()
        if (source.kind != 'vacancy' and source.published_on is not None
                and 0 <= (day - source.published_on).days <= 30
                and (source.valid_until is None or source.valid_until >= day)
                and freshness(source, as_of)[0] == 1):
            purchase.append(e)
    purchase_state = 'public_request' if purchase else 'unconfirmed'
    crm = candidate.crm_check
    crm_fresh = (crm.checked_at is not None and crm.checked_at.tzinfo is not None
                 and 0 <= (as_of - crm.checked_at).total_seconds() <= 86400)
    ready = (crm.state == 'no_match_accessible' and all(crm.coverage.get(k, False) for k in ['company', 'contact', 'lead'])
             and crm_fresh and (phone or email))
    if crm.state == 'confirmed_duplicate':
        action = 'Не создавать новый лид. Передать гипотезу текущему ответственному и сверить доступную историю: '
    elif crm.state == 'possible_match':
        action = 'Сначала вручную разрешить возможное совпадение CRM; до этого не импортировать: '
    elif not ready and (crm.state != 'no_match_accessible' or not crm_fresh or not all(crm.coverage.get(k, False) for k in ['company', 'contact', 'lead'])):
        action = 'Сначала завершить/обновить READ ONLY проверку дублей; до этого не импортировать: '
    elif not (phone or email):
        action = 'Сначала найти и проверить публичный деловой телефон/email; не импортировать без контакта: '
    else:
        action = 'Менеджеру вручную обратиться по опубликованному деловому каналу, уточнив ответственного: '
    next_step = action + (candidate.next_step or candidate.hypothesis)
    return {'score': score, 'priority': priority, 'breakdown': breakdown, 'rationale': rationale,
            'contact_score': contact_score, 'contact_level': contact_level,
            'purchase_interest': purchase_state, 'purchase_source_ids': [e.source_id for e in purchase],
            'import_ready': bool(ready), 'next_step': next_step, 'as_of': as_of.isoformat()}
