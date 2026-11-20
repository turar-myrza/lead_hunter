"""Offline draft validation/export and local outcome bookkeeping.

No network, CRM client, sending API or automatic event generation is used here.
Validation proves provenance and structural constraints, not semantic truth of a
paraphrase: the operator must explicitly review the actual text against sources.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
import os
from pathlib import Path
import re
from typing import Mapping

from app.models.account_outreach import (
    AccountOpportunityBrief, AccountOutcomeEvent, AccountOutcomeLedger, normalized,
)
from app.models.outreach_copy import validate_customer_facing_text, validate_single_cta


class OutreachBlocked(ValueError):
    """A dossier cannot become a commercial draft under the current evidence."""


def _data(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='python')
    if isinstance(value, Mapping):
        return dict(value)
    raise OutreachBlocked('A dated routing decision is required')


def _datetime(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise OutreachBlocked('Invalid decision timestamp') from exc
    return value


def _fresh(value, now, seconds):
    value = _datetime(value)
    return (isinstance(value, datetime) and value.tzinfo is not None
            and 0 <= (now - value).total_seconds() <= seconds)


def _quote_present(quote, excerpt):
    return normalized(quote) in normalized(excerpt)


def _same_relationship_offer(fact, offer):
    service, key = fact.get('service_key'), fact.get('offer_key')
    return ((not service and not key) or service == offer.product_key or key == offer.offer_key)


def _private_request_basis(history, offer, now):
    """A reviewed, recent incoming CRM request may reopen an earlier refusal.

    Card modification dates cannot establish chronology.  This function neither
    exports the private request/contact nor treats an outgoing campaign as one.
    """
    declined = [fact for fact in history.get('declined_offers', [])
                if _same_relationship_offer(fact, offer)]
    decline_dates = []
    for fact in declined:
        reference = fact.get('reference') or {}
        date = _datetime(reference.get('date'))
        if (reference.get('date_kind') != 'event' or not isinstance(date, datetime)
                or date.tzinfo is None or date > now):
            return False
        decline_dates.append(date)
    if not decline_dates:
        return False
    for fact in history.get('new_requests', []):
        reference = fact.get('reference') or {}
        date = _datetime(reference.get('date'))
        if (fact.get('confidence') == 'explicit'
                and (fact.get('service_key') == offer.product_key or fact.get('offer_key') == offer.offer_key)
                and reference.get('date_kind') == 'event'
                and _fresh(date, now, 30 * 86400)
                and _fresh(reference.get('checked_at'), now, 86400)
                and date > max(decline_dates)):
            return True
    return False


def validate_brief(brief: AccountOpportunityBrief, candidate, decision, *, as_of=None,
                   allow_relationship_followup=False):
    """Fail closed before rendering/writing; never treats IMPORT_READY as consent."""
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise OutreachBlocked('as_of must be timezone-aware')
    # Revalidation also catches mutations made after Pydantic construction.
    brief = AccountOpportunityBrief.model_validate(brief.model_dump(mode='python'))
    route = _data(decision)
    if route.get('category') == 'D' or route.get('contact_permissibility') != 'allow_draft':
        raise OutreachBlocked('Routing does not allow preparation of a commercial draft')
    if (route.get('candidate_name') != candidate.name or brief.candidate_name != candidate.name
            or route.get('category') != brief.category):
        raise OutreachBlocked('The dossier and routing decision must describe the same company and category')
    if not _fresh(route.get('as_of'), now, 86400):
        raise OutreachBlocked('Routing must be reviewed within the last 24 hours')
    if not _fresh(brief.reviewed_at, now, 86400) or not brief.content_reviewed:
        raise OutreachBlocked('The actual draft content needs explicit, current human/operator review')
    source_map = {source.id: source for source in candidate.sources}
    for fact in [*brief.facts, *brief.new_basis_refs]:
        source = source_map.get(fact.source_id)
        if not source or not _quote_present(fact.quote, source.excerpt):
            raise OutreachBlocked('A dossier fact is not supported by its recorded source excerpt')
        if (source.access != 'public_page' or source.last_attempt_status != 'success'
                or not _fresh(source.checked_at, now, 30 * 86400)):
            raise OutreachBlocked('Draft facts require successfully read, recently checked public sources')
    # A named contact must already have public identity/role evidence.  Private
    # CRM phones and names are never substituted as cold outreach recipients.
    addressee = brief.addressee
    matching_contacts = [contact for contact in candidate.contacts
                         if contact.type == addressee.channel_type
                         and contact.value == addressee.channel_value
                         and contact.evidence.source_id == addressee.source_id
                         and contact.role != 'recruitment']
    if not matching_contacts:
        raise OutreachBlocked('The recipient channel must be a recorded public business contact')
    source = source_map.get(addressee.source_id)
    if (not source or source.access != 'public_page' or source.last_attempt_status != 'success'
            or not _fresh(source.checked_at, now, 30 * 86400)):
        raise OutreachBlocked('The recipient channel must have a current successful public verification')
    matching_contacts = [contact for contact in matching_contacts
                         if _quote_present(contact.evidence.quote, source.excerpt)
                         and ((contact.type in {'phone', 'email'}
                               and _quote_present(contact.value, contact.evidence.quote))
                              or (contact.type == 'website' and source.kind == 'company_site'
                                  and contact.value.rstrip('/') == source.url.rstrip('/'))
                              or contact.type == 'public_page')]
    if not matching_contacts:
        raise OutreachBlocked('The recipient channel is absent from its recorded public evidence')
    if brief.recommended_approach.method == 'whatsapp' and not any(
            re.search(r'whats\s*app|ватсап|вацап|wa\.me/', contact.evidence.quote, re.I)
            for contact in matching_contacts):
        raise OutreachBlocked('WhatsApp must be explicitly published as a business channel for the number')
    if addressee.name or addressee.role_confirmed:
        def current_role(contact):
            evidence = contact.role_evidence
            role_source = source_map.get(evidence.source_id) if evidence else None
            return (role_source is not None and role_source.access == 'public_page'
                    and role_source.last_attempt_status == 'success'
                    and _fresh(role_source.checked_at, now, 30 * 86400)
                    and _quote_present(evidence.quote, role_source.excerpt))
        matching_contacts = [contact for contact in matching_contacts if current_role(contact)]
    if addressee.name and not any(contact.person_name == addressee.name and contact.role_evidence
                                  for contact in matching_contacts):
        raise OutreachBlocked('Named recipients require recorded public role evidence')
    if addressee.role_confirmed and not any(contact.role_evidence for contact in matching_contacts):
        raise OutreachBlocked('Confirmed recipient roles need explicit public role evidence')
    proposal = getattr(candidate, 'commercial_proposal', None)
    if proposal is None:
        raise OutreachBlocked('Route must assess the actual, specific commercial proposal')
    proposal = _data(proposal)
    if (proposal.get('product_key') != brief.offer.product_key
            or proposal.get('offer_key') != brief.offer.offer_key
            or normalized(proposal.get('hypothesis', '')) != normalized(brief.hypothesis)
            or normalized(proposal.get('concrete_offer', '')) != normalized(brief.offer.concrete_offer)):
        raise OutreachBlocked('Changing the offer requires a new routing review')
    confirmed_fact = proposal.get('confirmed_fact') or {}
    if (confirmed_fact.get('source_id'), normalized(confirmed_fact.get('quote', ''))) not in {
            (fact.source_id, normalized(fact.quote)) for fact in brief.facts}:
        raise OutreachBlocked('The actual proposal fact must appear in the dossier')
    if not brief.urgency_unconfirmed:
        for source_id in brief.basis_source_ids:
            source = source_map[source_id]
            publication = source.published_on
            day = now.astimezone(source.checked_at.tzinfo).date()
            if (publication is None or not 0 <= (day - publication).days <= 30
                    or source.vacancy_status in {'archived', 'closed'}
                    or (source.valid_until is not None and source.valid_until < day)):
                raise OutreachBlocked('Confirmed timing needs current dated, non-expired evidence')
    if brief.category == 'C':
        history = getattr(candidate, 'relationship_review', None)
        history = _data(history) if history is not None else {}
        if (history.get('status') != 'complete' or history.get('identity') != 'confirmed'
                or not history.get('owner_name') or history.get('owner_name') != route.get('owner_name')
                or not _fresh(history.get('checked_at'), now, 86400)):
            raise OutreachBlocked('Existing relationships require confirmed identity, complete current history and an owner')
        if history.get('do_not_contact'):
            raise OutreachBlocked('Relationship history prohibits contact')
        if any(item.get('service_key') == brief.offer.product_key for item in history.get('performed_services', [])):
            raise OutreachBlocked('Do not offer an already performed service')
        declined = any(_same_relationship_offer(item, brief.offer) for item in history.get('declined_offers', []))
        if declined:
            reviewed_basis = proposal.get('new_basis') or []
            dossier_basis = {(fact.source_id, normalized(fact.quote)) for fact in brief.new_basis_refs}
            reviewed_keys = {(item.get('source_id'), normalized(item.get('quote', ''))) for item in reviewed_basis}
            if dossier_basis and not dossier_basis.issubset(reviewed_keys):
                raise OutreachBlocked('The recorded public new basis differs from the one assessed by routing')
            if not dossier_basis and not _private_request_basis(history, brief.offer, now):
                raise OutreachBlocked('A previously declined offer needs the same verified new basis assessed by routing')
        if allow_relationship_followup is not True:
            raise OutreachBlocked('Category C requires explicit relationship follow-up permission')
    try:
        if brief.category in {'A', 'B'} or brief.whatsapp_text is not None:
            brief.manager_copy()
        else:
            for text in (brief.email.subject, brief.email.body):
                validate_customer_facing_text(text)
            validate_single_cta(brief.email.body, brief.cta)
        validate_customer_facing_text(brief.call_script)
        validate_single_cta(brief.call_script, brief.cta)
    except ValueError:
        raise OutreachBlocked('Update the WhatsApp and email drafts to the current external copy rules') from None
    material = Path(brief.useful_material.path)
    if material.is_symlink() or not material.is_file():
        raise OutreachBlocked('The useful material must be a prepared local file, not a placeholder or symlink')
    if not material.read_text(encoding='utf-8').strip():
        raise OutreachBlocked('The useful material must not be empty')
    # A saved allow_draft decision is not durable authorization: the candidate
    # may since have gained a CRM match, a refusal or a contact prohibition.
    # Routing is a pure local evaluator and performs no CRM/network reads.
    from app.services.lead_hunter_routing import route as current_route
    current = current_route(candidate, now)
    if (current.contact_permissibility != 'allow_draft'
            or current.category != brief.category or current.owner_name != route.get('owner_name')):
        raise OutreachBlocked('Current local evidence no longer permits this commercial draft')
    return brief


def validate_portfolio(briefs):
    """Catch literal name-substitution campaigns; semantic review remains explicit."""
    for index, left in enumerate(briefs):
        for right in briefs[index + 1:]:
            for first, second in ((left.email.body, right.email.body), (left.whatsapp_text, right.whatsapp_text)):
                if first and second:
                    a = normalized(first).replace(normalized(left.candidate_name), '<company>')
                    b = normalized(second).replace(normalized(right.candidate_name), '<company>')
                    if SequenceMatcher(None, a, b, autojunk=False).ratio() >= .92:
                        raise OutreachBlocked('Drafts are too similar after company-name substitution')


def render_brief(brief, candidate, decision, *, as_of=None, allow_relationship_followup=False):
    brief = validate_brief(brief, candidate, decision, as_of=as_of,
                           allow_relationship_followup=allow_relationship_followup)
    sources = {source.id: source for source in candidate.sources}
    facts = '\n'.join(f'- {fact.fact} [{fact.source_id}]({sources[fact.source_id].url}); '
                      f'проверено {sources[fact.source_id].checked_at.isoformat()}' for fact in brief.facts)
    route = _data(decision)
    crm = _data(candidate.crm_check)
    history = _data(candidate.relationship_review) if candidate.relationship_review is not None else {}
    stamp = lambda value: value.isoformat() if isinstance(value, datetime) else str(value or 'не установлен')
    relation_labels = {'active_customer': 'действующий клиент по данным CRM', 'former_customer': 'бывший клиент',
                       'lost_prospect': 'предыдущие переговоры проиграны', 'history_only': 'есть история взаимодействия',
                       'no_history': 'доступной истории не найдено', 'unknown': 'требуется проверка'}
    relation = history.get('relationship_state', 'unknown')
    crm_notes = list(dict.fromkeys([*crm.get('limitations', []), *history.get('limitations', [])]))
    crm_notes_text = '\n'.join('- ' + note for note in crm_notes[:6]) or '- Только доступные владельцу READ ONLY вебхука записи; скрытые записи неизвестны.'
    crm_section = (f'## Проверка CRM Consulting\n\n'
                   f'Состояние сопоставления: {crm.get("state", "not_checked")}; '
                   f'проверено: {stamp(crm.get("checked_at"))}.\n\n'
                   f'История: {relation_labels.get(relation, relation)}; '
                   f'полнота смысловой проверки: {history.get("status", "not_reviewed")}. '
                   f'Ответственный: {history.get("owner_name") or route.get("owner_name") or "не установлен"}.\n\n'
                   f'Последний подтверждённый контакт: {stamp(history.get("last_interaction_at"))}. '
                   f'Последнее доступное событие CRM: {stamp(history.get("last_event_at"))}; '
                   f'это может быть изменение карточки или техническая активность.\n\n'
                   f'Ограничения:\n\n{crm_notes_text}\n\n')
    timing = brief.why_now or 'Актуальность коммерческой задачи не подтверждена; уточнить у компании.'
    whatsapp_section = f'## WhatsApp — черновик\n\n{brief.whatsapp_text}\n\n' if brief.whatsapp_text else ''
    return (f'# Account Opportunity Brief — {brief.candidate_name}\n\n'
            f'Категория: {brief.category}. Статус: локальный черновик. Отправка не выполнялась.\n\n'
            f'Потенциальная потребность: {route.get("need_score", "не оценена")}; '
            f'перспективность предложения: {route.get("offer_prospect_score", "не оценена")}; '
            f'допустимость: {route.get("contact_permissibility")}.\n\n'
            f'{crm_section}'
            f'## Подтверждённые факты для внутреннего использования\n\n{facts}\n\n'
            f'## Контекст\n\n{brief.business_context}\n\n'
            f'## Гипотеза для внутреннего использования\n\n{brief.hypothesis}\n\n'
            f'## Одно предложение\n\n{brief.offer.concrete_offer}\n\n'
            f'Почему сейчас: {timing}' + (' Срочность не подтверждена.' if brief.urgency_unconfirmed else '') + '\n\n'
            f'Адресат: {brief.addressee.role}; {brief.addressee.channel_type}: {brief.addressee.channel_value}. '
            f'{brief.addressee.limitation or ""}\n\n'
            f'Рекомендуемый подход: {brief.recommended_approach.method}. {brief.recommended_approach.reason}\n\n'
            f'{whatsapp_section}'
            f'## Персональное письмо — черновик\n\nТема: {brief.email.subject}\n\n{brief.email.body}\n\n'
            f'## Сценарий звонка — черновик\n\n{brief.call_script}\n\n'
            f'Полезный материал: [{brief.useful_material.title}](<{brief.useful_material.path}>).\n\n'
            f'Следующий шаг: {brief.next_step}\n\nЦелевое действие: {brief.cta}\n\n'
            f'Метрика: {brief.target_metric}\n\nРиски:\n\n' + '\n'.join('- ' + risk for risk in brief.risks) + '\n\n'
            f'Текст проверен: {brief.reviewed_by}, {brief.reviewed_at.isoformat()}. '
            'Проверка структуры и ссылок не заменяет содержательную сверку текста с источниками.\n')


def _private_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Refusing to overwrite a symlink')
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags, 0o600), 'w', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)
    path.chmod(0o600)


def export_brief(brief, candidate, decision, output_dir, *, as_of=None, stem='account-brief',
                 allow_relationship_followup=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', stem):
        raise ValueError('A simple local output stem is required')
    brief = validate_brief(brief, candidate, decision, as_of=as_of,
                           allow_relationship_followup=allow_relationship_followup)
    output_dir = Path(output_dir)
    markdown = output_dir / f'{stem}.md'
    data = output_dir / f'{stem}.json'
    _private_write(markdown, render_brief(brief, candidate, decision, as_of=as_of,
                                        allow_relationship_followup=allow_relationship_followup))
    _private_write(data, brief.model_dump_json(indent=2))
    return {'markdown': markdown, 'json': data}


def append_outcome(path, event: AccountOutcomeEvent):
    """Explicit local entry only. Retries are idempotent; no inferred funnel events."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Outcome ledger must not be a symlink')
    ledger = AccountOutcomeLedger.model_validate_json(path.read_text(encoding='utf-8')) if path.exists() else AccountOutcomeLedger()
    event = AccountOutcomeEvent.model_validate(event.model_dump(mode='python'))
    for recorded in ledger.events:
        if recorded.event_id == event.event_id:
            if recorded == event:
                return False
            raise ValueError('Conflicting outcome event with the same idempotency key')
    ledger.events.append(event)
    _private_write(path, ledger.model_dump_json(indent=2))
    return True


def outcome_summary(ledger: AccountOutcomeLedger):
    counts = Counter(event.event_type for event in ledger.events)
    totals = defaultdict(Decimal)
    for event in ledger.events:
        if event.event_type == 'revenue':
            totals[event.currency] += event.amount
    return {'explicit_events': dict(counts),
            'revenue_by_currency': {currency: str(amount) for currency, amount in totals.items()},
            'note': 'Только явно внесённые события; подготовка не считается отправкой, договором или выручкой.'}
