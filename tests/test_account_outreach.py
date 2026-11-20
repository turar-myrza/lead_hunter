"""Offline account drafts: routing gates, provenance and explicitly entered outcomes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket

from pydantic import ValidationError
import pytest

from app.models.account_outreach import (
    AccountOpportunityBrief, AccountOutcomeEvent, AccountOutcomeLedger,
)
from app.models.lead_hunter import Candidate, Contact, Source
from app.models.outreach_copy import ManagerOutreachCopy
from app.services.account_outreach import (
    OutreachBlocked, append_outcome, export_brief, outcome_summary,
    render_brief, validate_brief, validate_portfolio,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def account(tmp_path):
    material = tmp_path / 'order-process.md'
    material.write_text('# Схема\n\nВопросы проверки заказа и резерва.', encoding='utf-8')
    facts = [
        dict(id='f1', source_id='site', quote='индивидуальные заказы',
             fact='На сайте компании указаны индивидуальные заказы.'),
        dict(id='f2', source_id='site', quote='два адреса',
             fact='На официальном сайте опубликованы два адреса.'),
        dict(id='f3', source_id='site', quote='+7 700 123 45 67',
             fact='Компания публикует деловой телефон +7 700 123 45 67.'),
    ]
    hypothesis = 'Возможно, при индивидуальном заказе нужно согласовать резерв с несколькими точками.'
    offer = 'Предлагаем показать схему связи заказа, резерва и поступления товара на одном примере.'
    cta = 'Удобно согласовать 15-минутный разбор одного заказа?'
    body = ('Здравствуйте, Example Business!\n\n'
            'В индивидуальном заказе важны согласованная дата и резерв товара. '
            'Покажем, как связать их в карточке, чтобы сотрудник видел, что готово к выдаче.\n\n' + cta)
    whatsapp = ('Здравствуйте! Когда для заказа нужно подобрать товар в разных точках, '
                'удобно сразу видеть, какая позиция уже в резерве и что ещё ждёт подтверждения. '
                'Можем показать это на одном тестовом примере с двумя позициями и датой выдачи. ' + cta)
    raw = dict(candidate_name='Example Business', category='A', facts=facts,
               business_context='Производство и торговля с индивидуальными заказами и двумя точками.',
               hypothesis=hypothesis,
               offer=dict(product_key='crm_implementation', offer_key='order_reserve_review',
                          solution='Связь заказа и резерва', concrete_offer=offer),
               urgency_unconfirmed=True,
               addressee=dict(role='Ответственный за продажи', channel_type='phone',
                              channel_value='+7 700 123 45 67', source_id='site',
                              limitation='Ответственный не установлен; опубликован общий деловой телефон.'),
               recommended_approach=dict(method='whatsapp', reason='Доступен опубликованный деловой номер WhatsApp.'),
               email=dict(subject='Согласование индивидуального заказа и резерва', body=body,
                          fact_refs=['f1'], confirmed_fact_fragment=facts[0]['fact'],
                          hypothesis_fragment=hypothesis, offer_fragment=offer),
               whatsapp_text=whatsapp,
               call_script=body, useful_material=dict(title='Схема заказа и резерва', path=str(material)),
               next_step='Менеджеру проверить схему и вручную согласовать подход к первому контакту.',
               risks=['Отсутствие CRM и потери обращений не подтверждены.'],
               cta=cta, target_metric='Получить согласие на один 15-минутный разбор.',
               basis_source_ids=['site'], reviewed_at=NOW, reviewed_by='Исследователь', content_reviewed=True)
    brief = AccountOpportunityBrief.model_validate(raw)
    source = Source(id='site', url='https://business.kz/', title='Официальный сайт', checked_at=NOW,
                    kind='company_site', access='public_page', vacancy_status='not_applicable',
                    excerpt='Example Business Шымкент индивидуальные заказы два адреса WhatsApp +7 700 123 45 67')
    contact = Contact(type='phone', value='+7 700 123 45 67',
                      evidence=dict(source_id='site', quote='WhatsApp +7 700 123 45 67', fact='Деловой WhatsApp'))
    proposal = dict(product_key='crm_implementation', offer_key='order_reserve_review',
                    hypothesis=hypothesis, concrete_offer=offer,
                    confirmed_fact={key: value for key, value in facts[0].items() if key != 'id'}, new_basis=[])
    candidate = Candidate(name='Example Business', sector='Производство и торговля', city='Шымкент',
                          website='https://business.kz/', sources=[source], contacts=[contact],
                          identity=dict(source_id='site', quote='Example Business', fact='Имя'),
                          geography=dict(source_id='site', quote='Шымкент', fact='Город'),
                          evidence=[dict(source_id='site', quote='индивидуальные заказы',
                                         fact='Индивидуальные заказы', signal='order_complexity')],
                          products=['CRM'], hypothesis=hypothesis,
                          commercial_proposal=dict(proposal, rationale='Проверяемая гипотеза организации заказа.'),
                          crm_check=dict(state='no_match_accessible', checked_at=NOW,
                                         coverage={'company': True, 'contact': True, 'lead': True}))
    decision = dict(candidate_name='Example Business', category='A', contact_permissibility='allow_draft',
                    active_task=True, as_of=NOW, need_score=25, offer_prospect_score=35, owner_name=None)
    return brief, candidate, decision


def test_export_is_local_draft_not_send_or_import_readiness(account, tmp_path, monkeypatch):
    brief, candidate, decision = account
    def forbidden(*args, **kwargs):
        raise AssertionError('Account draft export must not use the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    files = export_brief(brief, candidate, decision, tmp_path / 'export', as_of=NOW)
    data = json.loads(files['json'].read_text())
    text = files['markdown'].read_text()
    assert data['status'] == 'draft'
    assert data['content_reviewed'] is True
    assert 'Отправка не выполнялась' in text
    assert '## Подтверждённые факты' in text and '## Гипотеза' in text
    assert '## Проверка CRM Consulting' in text
    assert 'Последний подтверждённый контакт: не установлен' in text
    assert 'no_match_accessible' in text
    assert 'Проверка CRM Consulting' not in data['email']['body']
    assert '## WhatsApp — черновик' in text
    assert data['whatsapp_text'] == brief.whatsapp_text
    assert brief.facts[0].fact not in data['email']['body']
    assert f'](<{brief.useful_material.path}>)' in text
    assert 'IMPORT_READY' not in text
    assert not (tmp_path / 'events.json').exists()  # Preparation does not invent outcome entries.
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files.values())


def test_category_d_has_no_commercial_letter(account):
    brief, _, _ = account
    raw = brief.model_dump()
    raw['category'] = 'D'
    with pytest.raises(ValidationError, match='EXCLUDED'):
        AccountOpportunityBrief.model_validate(raw)


@pytest.mark.parametrize('patch', [
    {'contact_permissibility': 'needs_review'}, {'contact_permissibility': 'prohibited'},
    {'category': 'D'}, {'category': 'B'},
    {'candidate_name': 'Different Company'}, {'as_of': NOW - timedelta(days=2)},
    {'as_of': NOW + timedelta(minutes=1)}, {'as_of': NOW.replace(tzinfo=None)}, {'as_of': None},
])
def test_routing_gate_fails_closed(account, patch):
    brief, candidate, decision = account
    decision.update(patch)
    with pytest.raises(OutreachBlocked):
        validate_brief(brief, candidate, decision, as_of=NOW)
    with pytest.raises(OutreachBlocked):
        render_brief(brief, candidate, decision, as_of=NOW)


def test_import_ready_is_not_permission_for_outreach(account):
    brief, candidate, decision = account
    decision.update(contact_permissibility='needs_review', IMPORT_READY=True)
    with pytest.raises(OutreachBlocked):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_local_draft_permission_is_independent_from_active_manager_work(account):
    brief, candidate, decision = account
    decision['active_task'] = False
    from app.services.lead_hunter_routing import route
    current = route(candidate, NOW)
    assert current.contact_permissibility == 'allow_draft'
    assert current.commercial_priority == 'low'
    assert current.active_task is False
    assert validate_brief(brief, candidate, decision, as_of=NOW).status == 'draft'
    assert 'локальный черновик' in render_brief(brief, candidate, decision, as_of=NOW).casefold()


@pytest.mark.parametrize('changed', ['new_crm_match', 'do_not_contact', 'partial_coverage'])
def test_saved_allow_draft_is_rechecked_against_current_candidate(account, changed):
    brief, candidate, decision = account
    if changed == 'new_crm_match':
        candidate.crm_check.state = 'possible_match'
    elif changed == 'do_not_contact':
        candidate.relationship_review.do_not_contact = True
    else:
        candidate.crm_check.coverage['contact'] = False
    with pytest.raises(OutreachBlocked, match='Current local evidence'):
        validate_brief(brief, candidate, decision, as_of=NOW)


@pytest.mark.parametrize('field', ['product_key', 'offer_key', 'hypothesis', 'concrete_offer'])
def test_actual_proposal_cannot_be_changed_after_routing(account, field):
    brief, candidate, decision = account
    setattr(candidate.commercial_proposal, field, 'different reviewed proposal')
    with pytest.raises(OutreachBlocked, match='Changing the offer'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_proposal_needs_actual_confirmed_fact(account):
    brief, candidate, decision = account
    candidate.commercial_proposal.confirmed_fact.quote = 'Unsupported change'
    with pytest.raises(OutreachBlocked, match='proposal fact'):
        validate_brief(brief, candidate, decision, as_of=NOW)


@pytest.mark.parametrize('patch', [
    {'quote': 'not present in the source'}, {'source_id': 'unknown'},
])
def test_unsupported_fact_is_blocked(account, patch):
    brief, candidate, decision = account
    brief.facts[0] = brief.facts[0].model_copy(update=patch)
    with pytest.raises(OutreachBlocked, match='source excerpt'):
        validate_brief(brief, candidate, decision, as_of=NOW)


@pytest.mark.parametrize('change', ['stale', 'unavailable', 'search_only'])
def test_unverified_facts_are_not_used_for_drafts(account, change):
    brief, candidate, decision = account
    if change == 'stale':
        candidate.sources[0].checked_at = NOW - timedelta(days=31)
    elif change == 'unavailable':
        candidate.sources[0].last_attempt_status = 'unavailable'
    else:
        candidate.sources[0].access = 'search_excerpt'
    with pytest.raises(OutreachBlocked, match='recently checked'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_only_recorded_public_business_recipient_allowed(account):
    brief, candidate, decision = account
    brief.addressee.channel_value = '+7 777 000 00 00'  # A private/CRM-only replacement is not acceptable.
    with pytest.raises(OutreachBlocked, match='public business contact'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_recruitment_contact_cannot_be_used_for_commercial_draft(account):
    brief, candidate, decision = account
    candidate.contacts[0].role = 'recruitment'
    with pytest.raises(OutreachBlocked, match='public business contact'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_phone_alone_is_not_a_verified_whatsapp_channel(account):
    brief, candidate, decision = account
    candidate.contacts[0].evidence.quote = '+7 700 123 45 67'
    with pytest.raises(OutreachBlocked, match='explicitly published'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_legacy_dossier_remains_readable_but_needs_new_copy_for_export(account):
    brief, candidate, decision = account
    raw = brief.model_dump(exclude={'whatsapp_text'})
    raw['email']['body'] = raw['email']['confirmed_fact_fragment'] + ' ' + raw['email']['body']
    legacy = AccountOpportunityBrief.model_validate(raw)
    with pytest.raises(OutreachBlocked, match='current external copy rules'):
        validate_brief(legacy, candidate, decision, as_of=NOW)


@pytest.mark.parametrize('patch', [{'content_reviewed': False}, {'reviewed_at': NOW-timedelta(days=2)}])
def test_explicit_current_content_review_is_required(account, patch):
    brief, candidate, decision = account
    brief = brief.model_copy(update=patch)
    with pytest.raises(OutreachBlocked, match='actual draft content'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_undated_or_closed_job_does_not_confirm_timing(account):
    brief, candidate, decision = account
    brief.urgency_unconfirmed = False
    brief.why_now = 'Проект опубликован сейчас; требуется сверка действующего статуса.'
    with pytest.raises(OutreachBlocked, match='Confirmed timing'):
        validate_brief(brief, candidate, decision, as_of=NOW)
    candidate.sources[0].published_on = NOW.date()
    candidate.sources[0].vacancy_status = 'closed'
    with pytest.raises(OutreachBlocked, match='Confirmed timing'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_missing_useful_material_is_not_full_brief(account):
    brief, candidate, decision = account
    Path(brief.useful_material.path).unlink()
    with pytest.raises(OutreachBlocked, match='prepared local file'):
        validate_brief(brief, candidate, decision, as_of=NOW)


@pytest.mark.parametrize('change', ['fact', 'hypothesis', 'offer', 'two_ctas', 'duplicate_facts'])
def test_generic_or_untraceable_letters_are_rejected(account, change):
    brief, _, _ = account
    raw = brief.model_dump()
    if change == 'fact':
        raw['email']['confirmed_fact_fragment'] = 'Ваша компания очень быстро развивается.'
    elif change == 'hypothesis':
        raw['email']['hypothesis_fragment'] = 'Общая гипотеза, не связанная с индивидуальным заказом.'
    elif change == 'offer':
        raw['email']['body'] = 'Мы изучили вашу компанию. ' + raw['email']['body']
    elif change == 'two_ctas':
        raw['email']['body'] += '\n' + raw['cta']
    else:
        raw['facts'][1] = dict(raw['facts'][0], id='f2')
    with pytest.raises(ValidationError):
        AccountOpportunityBrief.model_validate(raw)


def test_name_substitution_campaign_rejected_across_portfolio(account):
    brief, _, _ = account
    second = brief.model_copy(deep=True)
    second.candidate_name = 'Other Business'
    second.email.body = second.email.body.replace('Example Business', 'Other Business')
    with pytest.raises(OutreachBlocked, match='company-name substitution'):
        validate_portfolio([brief, second])


@pytest.mark.parametrize('leak', [
    'Мы изучили вашу компанию.',
    'Мы увидели вашу вакансию.',
    'Мы заметили, что вы используете CRM.',
    'На вашем сайте написано, что вы берёте индивидуальные заказы.',
    'Мы проанализировали ваши процессы.',
    'Вы теряете заявки.',
    'У вас постоянно пропадают заявки.',
    'Напишите нам.',
])
def test_external_whatsapp_blocks_research_unverified_losses_and_second_cta(account, leak):
    brief, _, _ = account
    data = brief.manager_copy().model_dump()
    data['whatsapp_text'] = f'{leak} {data["whatsapp_text"]}'
    with pytest.raises(ValidationError):
        ManagerOutreachCopy.model_validate(data)


def test_exact_long_research_excerpt_stays_in_the_dossier(account):
    brief, _, _ = account
    raw = brief.model_dump()
    quote = 'Сотрудник согласует получение заказов в нескольких точках выдачи'
    raw['facts'][0]['quote'] = quote
    raw['email']['body'] = f'{quote}. {raw["email"]["body"]}'
    with pytest.raises(ValidationError, match='reproduce research excerpts'):
        AccountOpportunityBrief.model_validate(raw)


def test_customer_requires_full_history_and_owner(account):
    brief, candidate, decision = account
    brief.category = decision['category'] = 'C'
    for history in [None, dict(status='partial'),
                    dict(status='complete', identity='confirmed', checked_at=NOW)]:
        candidate.relationship_review = history
        with pytest.raises(OutreachBlocked, match='complete current history'):
            validate_brief(brief, candidate, decision, as_of=NOW)


def customer_account(account):
    brief, candidate, decision = account
    brief.category = decision['category'] = 'C'
    decision['owner_name'] = 'Current Owner'
    candidate.crm_check.state = 'confirmed_duplicate'
    candidate.relationship_review = dict(status='complete', identity='confirmed', owner_name='Current Owner',
                                          relationship_state='former_customer', checked_at=NOW, performed_services=[],
                                          declined_offers=[], new_requests=[], do_not_contact=False)
    return brief, candidate, decision


def test_completed_services_are_never_reoffered_to_customer(account):
    brief, candidate, decision = customer_account(account)
    candidate.relationship_review['performed_services'] = [dict(service_key=brief.offer.product_key)]
    with pytest.raises(OutreachBlocked, match='already performed'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_previously_declined_offer_without_new_basis_is_blocked(account):
    brief, candidate, decision = customer_account(account)
    candidate.relationship_review['declined_offers'] = [dict(offer_key=brief.offer.offer_key)]
    with pytest.raises(OutreachBlocked, match='verified new basis'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def reopened_customer(account):
    brief, candidate, decision = customer_account(account)
    def fact(text, event_id, date):
        return dict(text=text, confidence='explicit', service_key=brief.offer.product_key,
                    offer_key=brief.offer.offer_key,
                    reference=dict(method='crm.timeline.comment.list', entity='comment', id=event_id,
                                   field='COMMENT', date=date, date_kind='event', checked_at=NOW, quote=text))
    candidate.relationship_review['declined_offers'] = [fact('Клиент отказался от предложенного разбора заказа.', 100, NOW-timedelta(days=60))]
    candidate.relationship_review['new_requests'] = [fact('Клиент сам запросил повторный разбор заказа и резерва.', 101, NOW-timedelta(days=1))]
    return brief, candidate, decision


def test_fresh_explicit_incoming_crm_request_can_reopen_refusal_without_public_event(account, tmp_path):
    brief, candidate, decision = reopened_customer(account)
    assert not brief.new_basis_refs and not candidate.commercial_proposal.new_basis
    with pytest.raises(OutreachBlocked, match='explicit relationship follow-up permission'):
        export_brief(brief, candidate, decision, tmp_path / 'reopened', as_of=NOW)
    files = export_brief(brief, candidate, decision, tmp_path / 'reopened', as_of=NOW,
                         allow_relationship_followup=True)
    exported = files['json'].read_text()
    assert 'Клиент сам запросил' not in exported  # Private history is not inserted into a cold letter.
    assert json.loads(exported)['addressee']['channel_value'] == '+7 700 123 45 67'


@pytest.mark.parametrize('change', ['card_modified', 'unknown_date_kind', 'stale_event', 'future_event',
                                  'before_decline', 'unreviewed', 'wrong_product', 'stale_reference',
                                  'unknown_decline_date', 'modified_decline_date'])
def test_private_request_must_prove_new_chronology_not_merely_card_update(account, change):
    brief, candidate, decision = reopened_customer(account)
    request = candidate.relationship_review['new_requests'][0]
    decline = candidate.relationship_review['declined_offers'][0]
    if change == 'card_modified':
        request['reference']['date_kind'] = 'card_modified'
    elif change == 'unknown_date_kind':
        request['reference'].pop('date_kind')
    elif change == 'stale_event':
        request['reference']['date'] = NOW-timedelta(days=31)
    elif change == 'future_event':
        request['reference']['date'] = NOW+timedelta(seconds=1)
    elif change == 'before_decline':
        request['reference']['date'] = NOW-timedelta(days=61)
    elif change == 'unreviewed':
        request['confidence'] = 'requires_review'
    elif change == 'wrong_product':
        request['service_key'] = 'other_service'
        request['offer_key'] = 'other_offer'
    elif change == 'stale_reference':
        request['reference']['checked_at'] = NOW-timedelta(days=2)
    elif change == 'unknown_decline_date':
        decline['reference']['date'] = None
    else:
        decline['reference']['date_kind'] = 'card_modified'
    with pytest.raises(OutreachBlocked, match='verified new basis'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_fresh_request_does_not_reopen_already_performed_work(account):
    brief, candidate, decision = reopened_customer(account)
    candidate.relationship_review['performed_services'] = [dict(service_key=brief.offer.product_key)]
    with pytest.raises(OutreachBlocked, match='already performed'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def test_private_crm_request_does_not_authorize_private_contact_as_recipient(account):
    brief, candidate, decision = reopened_customer(account)
    brief.addressee.channel_value = '+7 777 111 22 33'
    with pytest.raises(OutreachBlocked, match='public business contact'):
        validate_brief(brief, candidate, decision, as_of=NOW)


def event(event_type='prepared', **changes):
    return AccountOutcomeEvent.model_validate(dict(dict(event_id='event-0001', candidate_name='Example Business',
        event_type=event_type, occurred_at=NOW-timedelta(minutes=2), entered_at=NOW,
        entered_by='Менеджер', evidence=dict(kind='local_artifact', reference='/tmp/brief.md',
                                             description='Проверенный локальный черновик предложения.')), **changes))


def test_local_metrics_do_not_infer_sending_sales_or_revenue(tmp_path):
    path = tmp_path / 'outcomes.json'
    prepared = event()
    assert append_outcome(path, prepared)
    assert not append_outcome(path, prepared)  # Idempotent retries do not double count.
    ledger = AccountOutcomeLedger.model_validate_json(path.read_text())
    assert outcome_summary(ledger)['explicit_events'] == {'prepared': 1}
    assert outcome_summary(ledger)['revenue_by_currency'] == {}
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='Conflicting'):
        append_outcome(path, prepared.model_copy(update={'entered_by': 'Other manager'}))


@pytest.mark.parametrize('kind', ['sent_by_manager', 'replied', 'meeting', 'proposal', 'contract', 'revenue'])
def test_local_draft_is_not_evidence_of_a_business_outcome(kind):
    changes = dict(amount='1000.00', currency='KZT') if kind == 'revenue' else {}
    with pytest.raises(ValidationError):
        event(kind, **changes)


def test_revenue_requires_explicit_amount_currency_and_evidence():
    evidence = dict(kind='manager_record', reference='payment-123', description='Менеджер подтвердил поступление оплаты по заказу.')
    with pytest.raises(ValidationError, match='explicitly entered amount'):
        event('revenue', evidence=evidence)
    revenue = event('revenue', event_id='revenue-0001', evidence=evidence, amount='12500.50', currency='KZT')
    summary = outcome_summary(AccountOutcomeLedger(events=[event(), revenue]))
    assert summary['explicit_events'] == {'prepared': 1, 'revenue': 1}
    assert summary['revenue_by_currency'] == {'KZT': '12500.50'}


def test_mutated_unsafe_brief_cannot_bypass_pydantic_checks(account):
    brief, candidate, decision = account
    brief.email.body = 'Здравствуйте! Мы внедряем CRM, свяжитесь с нами.'
    with pytest.raises(ValidationError):
        validate_brief(brief, candidate, decision, as_of=NOW)
