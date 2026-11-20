"""History-first v1.2 classification. Pure local evaluation; never opens a network."""
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import re

from app.models.lead_hunter_routing import RoutingDecision, ScoreItem
from app.services.lead_hunter_scoring import assess, freshness


# Different business questions have different scoring profiles. CRM adoption itself
# never earns points, and none of these profiles measures willingness to buy.
PROFILES = {
    'new_crm': {'inbound': 20, 'order_complexity': 20, 'team': 10, 'growth': 20,
                'channels': 10, 'processing_problem': 10, 'automation_project': 10},
    'existing_bitrix24': {'processing_problem': 35, 'automation_project': 35,
                         'order_complexity': 10, 'channels': 10, 'growth': 5, 'team': 5},
    'relationship': {'processing_problem': 25, 'automation_project': 20,
                     'order_complexity': 5, 'new_customer_request': 50},
}
LABELS = {'A': 'NEW CRM', 'B': 'EXISTING BITRIX24',
          'C': 'CRM CONSULTING CUSTOMERS', 'D': 'EXCLUDED / HOLD'}
BITRIX = re.compile(r'(?:bitrix\s*24|битрикс\s*24)', re.I)
OPERATING = re.compile(r'(?:использ\w*|работ\w*\s+(?:в|с\s+клиентск)|вед\w*|вести|вед[её]ни\w*|фиксир\w*|заполн\w*|обрабат\w*|уч[её]т\w*|наша\s+crm|crm\s*[:—-])', re.I)
REQUIREMENT = re.compile(r'(?:знани\w*|навык\w*|опыт\w*|приветству\w*|желатель\w*)', re.I)
NOT_ACTUAL_USE = re.compile(r'(?:не\s+(?:использ\w*|работ\w*|вед\w*)|планиру\w*|предстоит|хотим\s+(?:использ\w*|внедр\w*))', re.I)


def _get(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _fresh_check(checked_at, as_of, seconds=86400):
    return (checked_at is not None and checked_at.tzinfo is not None
            and 0 <= (as_of - checked_at).total_seconds() <= seconds)


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            pass
    return None


def _citation_valid(candidate, citation, as_of, *, current_event=False):
    source = next((s for s in candidate.sources if s.id == citation.source_id), None)
    normalize = lambda value: ' '.join(value.casefold().split())
    if not source or normalize(citation.quote) not in normalize(source.excerpt):
        return False
    if not freshness(source, as_of)[0]:
        return False
    local_day = as_of.astimezone(source.checked_at.tzinfo).date()
    if source.valid_until is not None and source.valid_until < local_day:
        return False
    if current_event:
        day = as_of.astimezone(source.checked_at.tzinfo).date()
        if not source.published_on or not 0 <= (day - source.published_on).days <= 30:
            return False
    return True


def _usage(candidate, as_of):
    usage = getattr(candidate, 'crm_usage_evidence', None)
    if usage is None or usage.status == 'unknown':
        return 'unknown', [], ['Использование CRM не установлено; отсутствие CRM не утверждается.']
    valid = []
    for citation in usage.evidence:
        if not _citation_valid(candidate, citation, as_of):
            continue
        if usage.status == 'bitrix24':
            # A requested skill or a bare product name is not evidence of an
            # installed system. The reviewed quote must describe operational use.
            quote = citation.quote
            if (not BITRIX.search(quote) or not OPERATING.search(quote)
                    or REQUIREMENT.search(quote) or NOT_ACTUAL_USE.search(quote)):
                continue
        valid.append(citation.source_id)
    if not valid:
        return 'requires_verification', [], ['Заявленный статус CRM не подтверждён актуальным операционным источником.']
    labels = {'bitrix24': 'Подтверждено использование Битрикс24.',
              'other': f'Подтверждена другая CRM: {usage.system_name or "название в источнике"}.',
              'present_unspecified': 'Подтверждена работа с CRM, но продукт не установлен; это может быть и Битрикс24.',
              'none_confirmed': 'Отсутствие CRM подтверждено отдельным источником.'}
    return usage.status, sorted(set(valid)), [labels[usage.status]]


def _signal_score(candidate, profile, as_of, review):
    sources = {s.id: s for s in candidate.sources}
    result = {}
    for signal, weight in PROFILES[profile].items():
        options = []
        if signal == 'new_customer_request':
            for fact in _get(review, 'new_requests', []):
                ref = _get(fact, 'reference')
                event_day = _day(_get(ref, 'date'))
                if _get(fact, 'confidence') != 'explicit' or not event_day or _get(ref,'date_kind')!='event':
                    continue
                age = (as_of.date() - event_day).days
                factor = Decimal('1') if 0 <= age <= 30 else Decimal('.5') if 31 <= age <= 90 else Decimal('0')
                options.append((int(Decimal(weight) * factor), None, _get(fact, 'text'),
                                f'Подтверждённый запрос клиента, возраст {age} дн.; коэффициент {factor}'))
        else:
            for evidence in candidate.evidence:
                if _get(evidence.signal, 'value', evidence.signal) != signal:
                    continue
                source = sources[evidence.source_id]
                factor, reason = freshness(source, as_of)
                if source.valid_until is not None and source.valid_until < as_of.astimezone(source.checked_at.tzinfo).date():
                    factor = Decimal('0')
                    reason += '; срок действия источника истёк'
                if evidence.scope == 'network':
                    factor *= Decimal('.5')
                    reason += '; общесетевые сведения ×0.5'
                if source.kind != 'vacancy' and source.published_on is None and signal in {
                        'growth', 'processing_problem', 'automation_project'}:
                    factor = Decimal('0')
                    reason += '; текущий проект/проблема не датированы'
                points = int((Decimal(weight) * factor).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                options.append((points, evidence.source_id, evidence.fact, reason))
        best = max(options, key=lambda item: item[0], default=(0, None, 'Нет подтверждения', 'Баллы не начислены'))
        result[signal] = ScoreItem(points=best[0], max=weight, source_id=best[1], fact=best[2], freshness=best[3])
    return sum(item.points for item in result.values()), result


def _same_offer(fact, proposal):
    service, offer = _get(fact, 'service_key'), _get(fact, 'offer_key')
    return (not service and not offer) or service == proposal.product_key or offer == proposal.offer_key


def _current_event_basis(candidate, citation, as_of):
    if not _citation_valid(candidate, citation, as_of, current_event=True):
        return False
    return any(e.source_id == citation.source_id
               and ' '.join(e.quote.casefold().split()) == ' '.join(citation.quote.casefold().split())
               and _get(e.signal, 'value', e.signal) in {'processing_problem', 'automation_project', 'growth'}
               for e in candidate.evidence)


def _current_request_basis(review, proposal, as_of):
    """A dated, scope-matched request can justify work through the current owner."""
    for fact in _get(review, 'new_requests', []):
        reference = _get(fact, 'reference')
        event_day = _day(_get(reference, 'date'))
        if (_get(fact, 'confidence') == 'explicit'
                and _get(reference, 'date_kind') == 'event'
                and _fresh_check(_get(reference, 'checked_at'), as_of)
                and _fresh_check(_get(reference, 'date'), as_of, 30 * 86400)
                and (_get(fact, 'service_key') == proposal.product_key
                     or _get(fact, 'offer_key') == proposal.offer_key)
                and event_day and 0 <= (as_of.date() - event_day).days <= 30):
            return True
    return False


def _new_basis(candidate, proposal, declined, review, as_of):
    """A fresh channel/phone is never a reason to repeat a declined offer."""
    decline_days = [_day(_get(_get(f, 'reference'), 'date')) if _get(_get(f,'reference'),'date_kind')=='event' else None for f in declined]
    if any(day is None for day in decline_days):
        return False
    latest = max(decline_days, default=date.min)
    for fact in _get(review, 'new_requests', []):
        event_day = _day(_get(_get(fact, 'reference'), 'date'))
        if (_get(fact, 'confidence') == 'explicit'
                and _get(_get(fact,'reference'),'date_kind')=='event'
                and (_get(fact, 'service_key') == proposal.product_key or _get(fact, 'offer_key') == proposal.offer_key)
                and event_day and latest < event_day <= as_of.date()
                and (as_of.date() - event_day).days <= 30):
            return True
    for citation in proposal.new_basis:
        if not _current_event_basis(candidate, citation, as_of):
            continue
        source = next(s for s in candidate.sources if s.id == citation.source_id)
        if source.published_on > latest:
            return True
    return False


def _offer_assessment(candidate, proposal, profile, score, review, as_of):
    if proposal is None:
        return None, ['Предметное предложение ещё не сформировано; его перспективность не оценена.']
    if not _citation_valid(candidate, proposal.confirmed_fact, as_of):
        return None, ['Подтверждающий факт предложения требует обновления/проверки.']
    # A concrete, evidence-linked proposal may be prepared without treating a
    # prospect as a ready buyer. Need contributes at most 35 points here.
    points = 20 + min(35, score * 35 // 100)
    reasons = ['20 баллов: конкретное предложение связано с проверенным фактом.',
               f'{min(35, score * 35 // 100)} баллов: сила потребности по профилю {profile}.']
    if any(_current_event_basis(candidate, c, as_of) for c in proposal.new_basis):
        points += 15
        reasons.append('15 баллов: отдельное актуальное основание «почему сейчас».')
    if _get(review, 'identity') == 'confirmed' and _get(review, 'status') == 'complete':
        points += 10
        reasons.append('10 баллов: подтверждена и изучена история отношений.')
    # A genuine request for a vendor is independent of a vacancy or inferred need.
    if assess(candidate, as_of)['purchase_interest'] == 'public_request':
        points += 20
        reasons.append('20 баллов: отдельный актуальный публичный запрос на предложение.')
    return min(100, points), reasons


def route(candidate, as_of: datetime | None = None) -> RoutingDecision:
    """Classify first, then score an eligible business category; fail closed."""
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError('as_of must be timezone-aware')
    sources = {s.id: s for s in candidate.sources}
    contacts = [c for c in candidate.contacts
                if sources[c.evidence.source_id].valid_until is None
                or sources[c.evidence.source_id].valid_until >= as_of.astimezone(sources[c.evidence.source_id].checked_at.tzinfo).date()]
    # Keep the audited v1.1 contact/purchase rules, with an additional explicit
    # expiry guard for v1.2; never substitute private CRM contacts.
    independent = assess(candidate.model_copy(update={'contacts': contacts}), as_of)
    review = getattr(candidate, 'relationship_review', None)
    crm = candidate.crm_check
    proposal = getattr(candidate, 'commercial_proposal', None)
    usage, usage_sources, usage_reasons = _usage(candidate, as_of)
    reasons, limitations = list(usage_reasons), list(candidate.limitations)
    limitations += list(_get(review, 'limitations', []))
    owner = _get(review, 'owner_name')
    relation = _get(review, 'relationship_state', 'unknown')
    forbidden = bool(_get(review, 'do_not_contact', False))
    holds = []
    # Customer identity describes a relationship, not permission to contact.
    # Keep a confirmed relationship in C even when its history or current offer
    # is on hold. Unresolved identities remain excluded from customer claims.
    known_relationship = (crm.state == 'confirmed_duplicate'
                          and _get(review, 'identity') == 'confirmed'
                          and relation in {'active_customer', 'former_customer', 'lost_prospect', 'history_only'})
    category, profile = ('C', 'relationship') if known_relationship else ('D', 'not_scored')
    if forbidden:
        holds.append('В истории установлен запрет обращения; контакт запрещён.')
    elif crm.state == 'possible_match' or _get(review, 'identity') == 'possible':
        holds.append('Совпадение CRM не разрешено: сначала подтвердить идентичность и изучить историю.')
    elif not _fresh_check(crm.checked_at, as_of) or not all(crm.coverage.get(k, False) for k in ['company', 'contact', 'lead']):
        holds.append('READ ONLY проверка CRM неполна либо старше 24 часов.')
    elif known_relationship:
        if (crm.state != 'confirmed_duplicate' or _get(review, 'status') != 'complete'
                or not _fresh_check(_get(review, 'checked_at'), as_of)):
            holds.append('Релевантная история отношений не подтверждена/не изучена полностью; требуется проверка.')
        elif not owner:
            holds.append('Не установлен ответственный CRM Consulting; прямое обращение не разрешено.')
    elif crm.state == 'confirmed_duplicate':
        holds.append('Релевантная история отношений не подтверждена/не изучена полностью; требуется проверка.')
    elif crm.state != 'no_match_accessible':
        holds.append('Проверка CRM не установила отсутствие доступных совпадений.')
    elif _get(review, 'identity') == 'confirmed' or relation not in {'unknown', 'no_history'}:
        holds.append('Результат идентификации противоречит истории отношений; требуется проверка.')
    elif usage == 'bitrix24':
        category, profile = 'B', 'existing_bitrix24'
    elif usage == 'other':
        holds.append('Подтверждена другая CRM: первое внедрение не предлагать; требуется отдельное решение о миграции/интеграции.')
    elif usage == 'present_unspecified':
        holds.append('Подтверждена работа с CRM, но продукт не установлен: первое внедрение не предлагать; сначала уточнить систему.')
    elif usage == 'requires_verification':
        holds.append('Статус существующей CRM требует проверки; рекомендация первого внедрения заблокирована.')
    else:
        category, profile = 'A', 'new_crm'

    if not forbidden and independent['contact_level'] not in {'company_channel', 'responsible_person'}:
        holds.append('Нет проверенного публичного делового телефона/email; сначала найти контакт.')
    if category == 'C' and relation == 'lost_prospect' and not _get(review, 'declined_offers', []):
        holds.append('Причина предыдущего отказа не установлена; сначала изучить её и допустимость повторного контакта.')
    if category == 'C' and proposal is not None:
        performed = [f for f in _get(review, 'performed_services', []) if _same_offer(f, proposal)]
        declined = [f for f in _get(review, 'declined_offers', []) if _same_offer(f, proposal)]
        if performed:
            holds.append('Предлагаемая работа уже выполнена либо её объём не уточнён; повторное предложение заблокировано.')
        if declined and not _new_basis(candidate, proposal, declined, review, as_of):
            holds.append('Предложение ранее отклонено; нового датированного основания для повторения не установлено.')
    elif category == 'C' and _get(review, 'declined_offers', []) and not _get(review, 'new_requests', []):
        holds.append('В истории есть отказ; без предметного нового основания контакт требует отдельного решения.')
    if holds:
        if category != 'C':
            category, profile = 'D', 'not_scored'
        reasons += holds

    blocked = bool(holds) or category == 'D'
    score, breakdown = (None, {}) if blocked else _signal_score(candidate, profile, as_of, review)
    offer_score, offer_reasons = ((None, ['Коммерческое предложение заблокировано до разрешения ограничений.'])
                                  if blocked else _offer_assessment(candidate, proposal, profile, score, review, as_of))
    ready = not blocked and proposal is not None and offer_score is not None
    permission = 'prohibited' if forbidden else 'allow_draft' if ready else 'needs_review'
    why_now = bool(ready and (any(_current_event_basis(candidate, citation, as_of) for citation in proposal.new_basis)
                             or _current_request_basis(review, proposal, as_of)))
    priority = ('hold' if not ready else 'low' if not why_now else 'high' if score >= 65 and offer_score >= 55
                else 'medium' if score >= 30 and offer_score >= 30 else 'low')
    active = ready and why_now and priority in {'medium', 'high'}
    if ready and not why_now:
        reasons.append('Предметного текущего основания «почему сейчас» не установлено; только исследование, без активного обращения.')
    elif ready and priority == 'low':
        reasons.append('Слабая потенциальная потребность: низкий приоритет не создаёт активное задание менеджеру.')
    if category == 'D':
        next_step = 'Не включать в активные задания менеджеру. ' + ' '.join(holds)
    elif category == 'C':
        if blocked:
            next_step = 'Категория клиента сохранена. Не включать в активные задания менеджеру. ' + ' '.join(holds)
        else:
            next_step = f'Только через ответственного {owner}: ' + (proposal.concrete_offer if active else
                'внутреннее исследование нового запроса и допустимого объёма предложения; активное обращение не рекомендовано.')
        reasons.append('История отношений имеет приоритет над публичным скорингом; обращение только через ответственного.')
    else:
        next_step = ('Подготовить локальный черновик для ручного решения менеджера: ' + proposal.concrete_offer
                     if active else 'Только исследование: уточнить актуальную задачу и предметный повод для обращения; активное обращение не рекомендовано.'
                     if ready else 'Сформировать предметную гипотезу и проверяемое предложение; автоматическое обращение не разрешено.')
    return RoutingDecision(candidate_name=candidate.name, category=category, category_label=LABELS[category],
                           need_profile=profile, need_score=score, need_breakdown=breakdown,
                           need_rationale=[f'{key}: {item.points}/{item.max}. {item.fact}. {item.freshness}' for key, item in breakdown.items()],
                           offer_prospect_score=offer_score, offer_rationale=offer_reasons,
                           contact_permissibility=permission, commercial_priority=priority,
                           contact_score=independent['contact_score'], contact_level=independent['contact_level'],
                           purchase_interest=independent['purchase_interest'], crm_status=usage, crm_source_ids=usage_sources,
                           relationship_state=relation, owner_name=owner, reasons=reasons, limitations=limitations,
                           active_task=active, next_step=next_step, as_of=as_of)
