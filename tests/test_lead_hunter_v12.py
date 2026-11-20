"""v1.2 acceptance: identity/history precede scoring and local draft permission."""
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import ValidationError
import pytest

from app.models.lead_hunter import Candidate, CommercialSignal
from app.models.lead_hunter_crm import CRMCheck
from app.models.lead_hunter_relationships import CRMReference, RelationshipEntity, RelationshipFact, RelationshipReview
from app.models.lead_hunter_routing import CommercialProposal, CRMUsageAssessment, EvidenceCitation, RoutingDecision
from app.services.lead_hunter_routing import PROFILES, route


NOW = datetime.now(timezone.utc)


def citation(quote='индивидуальные заказы'):
    return EvidenceCitation(source_id='site', quote=quote, fact='Проверенный факт компании')


@pytest.fixture
def candidate_v12():
    return Candidate.model_validate({
        'name': 'Проверенная компания', 'city': 'Шымкент', 'sector': 'Производство',
        'website': 'https://business.kz/',
        'identity': {'source_id': 'site', 'quote': 'Проверенная компания', 'fact': 'Название'},
        'geography': {'source_id': 'site', 'quote': 'Шымкент', 'fact': 'Город'},
        'sources': [{'id': 'site', 'url': 'https://business.kz/', 'title': 'Официальная страница',
                     'checked_at': NOW, 'published_on': NOW.date(), 'vacancy_status': 'not_applicable',
                     'kind': 'company_site', 'access': 'public_page',
                     'excerpt': 'Проверенная компания Шымкент индивидуальные заказы +7 (700) 123-45-67 '
                                'работу с клиентской базой в Bitrix24. Опыт работы в Битрикс24 приветствуется. '
                                'Используем amoCRM. CRM отсутствует. Новый филиал. Проект автоматизации. '
                                'Потери обращений. Несколько менеджеров. Поток заявок.'}],
        'evidence': [{'source_id': 'site', 'quote': 'индивидуальные заказы', 'fact': 'Индивидуальные заказы', 'signal': 'order_complexity'}],
        'contacts': [{'type': 'phone', 'value': '+7 (700) 123-45-67', 'evidence': {
            'source_id': 'site', 'quote': '+7 (700) 123-45-67', 'fact': 'Публичный деловой телефон'}}],
        'products': ['Автоматизация заказов'], 'hypothesis': 'Как связываются заказ и производство?',
        'crm_check': {'state': 'no_match_accessible', 'checked_at': NOW,
                      'coverage': {'company': True, 'contact': True, 'lead': True}},
        'commercial_proposal': {'product_key': 'order_automation', 'offer_key': 'order_flow_demo',
                                'hypothesis': 'Связь заказов и производства может требовать ручной работы.',
                                'concrete_offer': 'Показать схему заказа с этапом согласования и резервом.',
                                'confirmed_fact': citation().model_dump(),
                                'rationale': 'Индивидуальные заказы требуют согласования параметров.'},
    })


def review(candidate, **changes):
    candidate.crm_check.state = 'confirmed_duplicate'
    candidate.relationship_review = RelationshipReview(**{
        'status': 'complete', 'identity': 'confirmed', 'relationship_state': 'active_customer',
        'checked_at': NOW, 'owner_name': 'Ответственный CRM Consulting', **changes})
    return candidate.relationship_review


def fact(days=60, service='order_automation', offer='order_flow_demo', **changes):
    return RelationshipFact(text='Подтверждённый факт истории', confidence='explicit',
                            service_key=service, offer_key=offer,
                            reference=CRMReference(method='crm.timeline.comment.list', entity='deal', id=7,
                                                   field='COMMENT', date=NOW-timedelta(days=days),
                                                   date_kind='event',
                                                   quote='Подтверждённый факт истории', checked_at=NOW), **changes)


def strong(candidate):
    candidate.evidence = [candidate.evidence[0].model_copy(update={'signal': signal}) for signal in CommercialSignal]


def assert_customer_hold(result, permission='needs_review'):
    assert result.category == 'C'
    assert result.need_score is None
    assert result.offer_prospect_score is None
    assert result.commercial_priority == 'hold'
    assert result.contact_permissibility == permission
    assert not result.active_task


def test_unknown_crm_is_first_implementation_hypothesis_not_absence(candidate_v12):
    result = route(candidate_v12, NOW)
    assert result.category == 'A'
    assert result.crm_status == 'unknown'
    assert 'отсутствие CRM не утверждается' in ' '.join(result.reasons)
    assert result.contact_permissibility == 'allow_draft'
    assert result.purchase_interest == 'unconfirmed'


def test_bitrix_requires_explicit_operational_evidence(candidate_v12):
    candidate_v12.crm_usage_evidence = CRMUsageAssessment(status='bitrix24', basis='explicit_usage',
        evidence=[citation('работу с клиентской базой в Bitrix24')])
    result = route(candidate_v12, NOW)
    assert result.category == 'B'
    assert result.crm_source_ids == ['site']
    assert result.need_profile == 'existing_bitrix24'


@pytest.mark.parametrize('quote', ['Bitrix24', 'Опыт работы в Битрикс24 приветствуется'])
def test_product_name_and_required_skill_do_not_prove_installed_crm(candidate_v12, quote):
    candidate_v12.crm_usage_evidence = CRMUsageAssessment(status='bitrix24', basis='explicit_usage', evidence=[citation(quote)])
    result = route(candidate_v12, NOW)
    assert result.category == 'D'
    assert result.crm_status == 'requires_verification'
    assert not result.active_task


@pytest.mark.parametrize('quote', ['Не используем Bitrix24', 'Планируем использовать Bitrix24'])
def test_negative_and_future_use_are_not_actual_bitrix_use(candidate_v12, quote):
    candidate_v12.sources[0].excerpt += ' ' + quote
    candidate_v12.crm_usage_evidence = CRMUsageAssessment(status='bitrix24', basis='explicit_usage', evidence=[citation(quote)])
    assert route(candidate_v12, NOW).category == 'D'


def test_expired_source_cannot_prove_current_crm_use(candidate_v12):
    candidate_v12.sources[0].valid_until = NOW.date()-timedelta(days=1)
    candidate_v12.crm_usage_evidence = CRMUsageAssessment(status='bitrix24', basis='explicit_usage',
        evidence=[citation('работу с клиентской базой в Bitrix24')])
    assert route(candidate_v12, NOW).category == 'D'


@pytest.mark.parametrize('status', ['closed', 'archived'])
def test_closed_bitrix_vacancy_cannot_establish_current_use(candidate_v12, status):
    source = candidate_v12.sources[0]
    source.kind, source.vacancy_status = 'vacancy', status
    candidate_v12.crm_usage_evidence = CRMUsageAssessment(status='bitrix24', basis='explicit_usage',
        evidence=[citation('работу с клиентской базой в Bitrix24')])
    assert route(candidate_v12, NOW).category == 'D'


def test_other_crm_cannot_be_routed_to_first_implementation(candidate_v12):
    candidate_v12.crm_usage_evidence = CRMUsageAssessment(status='other', system_name='amoCRM', basis='explicit_usage',
        evidence=[citation('Используем amoCRM')])
    result = route(candidate_v12, NOW)
    assert result.category == 'D'
    assert 'первое внедрение не предлагать' in result.next_step


def test_existing_unspecified_crm_does_not_mean_non_bitrix_or_first_install(candidate_v12):
    candidate_v12.sources[0].excerpt += ' Ведение клиентской базы в CRM'
    candidate_v12.crm_usage_evidence=CRMUsageAssessment(status='present_unspecified',basis='explicit_usage',
        evidence=[citation('Ведение клиентской базы в CRM')])
    result=route(candidate_v12,NOW)
    assert result.category=='D' and result.crm_status=='present_unspecified'
    assert 'это может быть и Битрикс24' in ' '.join(result.reasons)
    assert not result.active_task


def test_card_modified_date_cannot_make_repeating_declined_offer_permissible(candidate_v12):
    r=review(candidate_v12)
    refused=fact(days=60)
    refused.reference.date_kind='card_modified'
    r.declined_offers=[refused]
    r.new_requests=[fact(days=1)]
    d=route(candidate_v12,NOW)
    assert_customer_hold(d)


@pytest.mark.parametrize('state', ['not_checked', 'unavailable', 'incomplete', 'possible_match'])
def test_crm_identity_gate_precedes_even_maximum_need(candidate_v12, state):
    strong(candidate_v12)
    candidate_v12.crm_check.state = state
    result = route(candidate_v12, NOW)
    assert result.category == 'D'
    assert result.need_score is None
    assert result.commercial_priority == 'hold'
    assert not result.active_task


@pytest.mark.parametrize('mutation', ['stale', 'future', 'coverage'])
def test_crm_check_must_be_current_and_complete(candidate_v12, mutation):
    if mutation == 'stale':
        candidate_v12.crm_check.checked_at = NOW-timedelta(days=2)
    elif mutation == 'future':
        candidate_v12.crm_check.checked_at = NOW+timedelta(seconds=1)
    else:
        candidate_v12.crm_check.coverage['contact'] = False
    assert route(candidate_v12, NOW).category == 'D'


@pytest.mark.parametrize('changes', [{'status': 'partial'}, {'status': 'unavailable'}, {'identity': 'possible', 'relationship_state': 'unknown'},
                                     {'owner_name': None}, {'checked_at': NOW-timedelta(days=2)}, {'relationship_state': 'unknown'}])
def test_confirmed_record_is_not_a_contact_permission_without_history(candidate_v12, changes):
    review(candidate_v12, **changes)
    result = route(candidate_v12, NOW)
    assert result.category == ('D' if changes.get('identity') == 'possible' or changes.get('relationship_state') == 'unknown' else 'C')
    assert result.need_score is None and result.offer_prospect_score is None
    assert not result.active_task
    assert result.commercial_priority == 'hold'
    assert result.contact_permissibility == 'needs_review'


def test_customer_history_wins_over_bitrix_category_and_routes_through_owner(candidate_v12):
    candidate_v12.crm_usage_evidence = CRMUsageAssessment(status='bitrix24', basis='explicit_usage',
        evidence=[citation('работу с клиентской базой в Bitrix24')])
    review(candidate_v12)
    result = route(candidate_v12, NOW)
    assert result.category == 'C'
    assert result.need_profile == 'relationship'
    assert 'Только через ответственного' in result.next_step
    assert result.owner_name == 'Ответственный CRM Consulting'


def test_completed_product_is_not_resold_even_with_maximum_public_need(candidate_v12):
    strong(candidate_v12)
    review(candidate_v12, performed_services=[fact()])
    result = route(candidate_v12, NOW)
    assert_customer_hold(result)
    assert 'уже выполнена' in result.next_step


def test_different_product_can_be_considered_after_complete_history(candidate_v12):
    review(candidate_v12, performed_services=[fact(service='bitrix_setup', offer='initial_setup')])
    assert route(candidate_v12, NOW).category == 'C'


def test_unknown_completed_scope_requires_manual_review(candidate_v12):
    review(candidate_v12, performed_services=[fact(service=None, offer=None)])
    assert_customer_hold(route(candidate_v12, NOW))


def test_refusal_without_new_reason_blocks_repeat_offer(candidate_v12):
    review(candidate_v12, relationship_state='lost_prospect', declined_offers=[fact()])
    assert_customer_hold(route(candidate_v12, NOW))


def test_lost_deal_without_known_reason_is_not_a_new_prospect(candidate_v12):
    review(candidate_v12, relationship_state='lost_prospect')
    result = route(candidate_v12, NOW)
    assert_customer_hold(result)
    assert 'Причина предыдущего отказа' in result.next_step


def test_phone_or_new_website_is_not_a_reason_to_repeat_refused_offer(candidate_v12):
    review(candidate_v12, declined_offers=[fact()])
    candidate_v12.commercial_proposal.new_basis = [citation()]
    assert_customer_hold(route(candidate_v12, NOW))


def test_generic_dated_fact_does_not_raise_offer_urgency(candidate_v12):
    baseline = route(candidate_v12, NOW).offer_prospect_score
    candidate_v12.commercial_proposal.new_basis = [citation()]
    assert route(candidate_v12, NOW).offer_prospect_score == baseline


def test_expired_event_does_not_earn_need_points(candidate_v12):
    candidate_v12.commercial_proposal = None
    candidate_v12.sources.append(candidate_v12.sources[0].model_copy(update={'id': 'contacts'}))
    candidate_v12.contacts[0].evidence.source_id = 'contacts'
    candidate_v12.sources[0].valid_until = NOW.date()-timedelta(days=1)
    assert route(candidate_v12, NOW).need_score == 0


def test_explicitly_expired_contact_does_not_open_draft_permission(candidate_v12):
    candidate_v12.sources[0].valid_until = NOW.date()-timedelta(days=1)
    result = route(candidate_v12, NOW)
    assert result.category == 'D'
    assert result.contact_score == 0


def test_fresh_dated_project_can_justify_owner_review_of_refused_offer(candidate_v12):
    review(candidate_v12, declined_offers=[fact()])
    project = candidate_v12.evidence[0].model_copy(update={'quote': 'Проект автоматизации', 'signal': CommercialSignal.AUTOMATION_PROJECT})
    candidate_v12.evidence.append(project)
    candidate_v12.commercial_proposal.new_basis = [citation('Проект автоматизации')]
    result = route(candidate_v12, NOW)
    assert result.category == 'C'
    assert result.contact_permissibility == 'allow_draft'
    assert 'ответственного' in result.next_step


def test_new_customer_request_must_follow_refusal_and_match_scope(candidate_v12):
    review(candidate_v12, declined_offers=[fact()], new_requests=[fact(days=5)])
    assert route(candidate_v12, NOW).category == 'C'
    candidate_v12.relationship_review.new_requests = [fact(days=5, service='different', offer='different')]
    assert_customer_hold(route(candidate_v12, NOW))


def test_unknown_refusal_date_cannot_be_overruled_by_assumed_newness(candidate_v12):
    declined = fact()
    declined.reference.date = None
    review(candidate_v12, declined_offers=[declined], new_requests=[fact(days=5)])
    assert_customer_hold(route(candidate_v12, NOW))


def test_explicit_do_not_contact_is_prohibited_regardless_of_scores(candidate_v12):
    strong(candidate_v12)
    review(candidate_v12, do_not_contact=True)
    result = route(candidate_v12, NOW)
    assert_customer_hold(result, permission='prohibited')
    assert result.contact_permissibility == 'prohibited'
    assert not result.active_task


def test_private_crm_phone_does_not_replace_missing_public_contact(candidate_v12):
    candidate_v12.contacts = []
    review(candidate_v12)
    result = route(candidate_v12, NOW)
    assert_customer_hold(result)
    assert result.contact_score == 0


def test_recruitment_contact_is_not_a_cold_sales_channel(candidate_v12):
    candidate_v12.contacts[0].role = 'recruitment'
    assert route(candidate_v12, NOW).category == 'D'


def test_profiles_have_distinct_business_weights(candidate_v12):
    assert all(sum(weights.values()) == 100 for weights in PROFILES.values())
    a = route(candidate_v12, NOW)
    candidate_v12.crm_usage_evidence = CRMUsageAssessment(status='bitrix24', basis='explicit_usage',
        evidence=[citation('работу с клиентской базой в Bitrix24')])
    b = route(candidate_v12, NOW)
    review(candidate_v12)
    c = route(candidate_v12, NOW)
    assert (a.need_score, b.need_score, c.need_score) == (20, 10, 5)
    assert len({a.need_profile, b.need_profile, c.need_profile}) == 3
    assert all(d.purchase_interest == 'unconfirmed' for d in [a,b,c])


def test_recent_confirmed_request_is_distinct_from_generic_customer_history(candidate_v12):
    review(candidate_v12, new_requests=[fact(days=3)])
    assert route(candidate_v12, NOW).need_breakdown['new_customer_request'].points == 50
    candidate_v12.relationship_review.new_requests = [fact(days=91)]
    assert route(candidate_v12, NOW).need_breakdown['new_customer_request'].points == 0


def test_no_subject_specific_proposal_means_no_offer_score_or_outreach_task(candidate_v12):
    candidate_v12.commercial_proposal = None
    result = route(candidate_v12, NOW)
    assert result.category == 'A'
    assert result.need_score == 20
    assert result.offer_prospect_score is None
    assert result.contact_permissibility == 'needs_review'
    assert not result.active_task


def test_routing_has_no_network_or_write_side_effects(candidate_v12, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Routing must remain local')
    monkeypatch.setattr(httpx.Client, 'request', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)
    assert route(candidate_v12, NOW).category == 'A'


def test_usage_model_rejects_unsupported_claims():
    with pytest.raises(ValidationError):
        CRMUsageAssessment(status='bitrix24', basis='explicit_usage')
    with pytest.raises(ValidationError):
        CRMUsageAssessment(status='none_confirmed', basis='explicit_usage', evidence=[citation()])


def test_no_active_task_can_be_smuggled_into_hold_record(candidate_v12):
    candidate_v12.crm_check.state = 'possible_match'
    result = route(candidate_v12, NOW).model_dump()
    result['active_task'] = True
    with pytest.raises(ValidationError):
        RoutingDecision.model_validate(result)


def test_sanbel_control_unresolved_identity_overrides_strong_public_signals(candidate_v12):
    """Synthetic control; no actual customer history, owner, IDs or phones."""
    c = candidate_v12
    c.name = 'SANBEL — синтетический контроль маршрутизации'
    c.sources[0].excerpt += ' ' + c.name
    c.identity.quote = c.name
    # Deliberately strong *synthetic* public signals must not turn an unresolved
    # relationship into a new prospect. The public stock/order and phone proofs
    # already present in this fixture are retained.
    strong(c)
    c.crm_check.state = 'possible_match'
    earlier = fact(days=300, service='crm_setup', offer='initial_setup')
    earlier.text = 'Синтетическая успешная стадия; факт исполнения услуги не установлен.'
    earlier.confidence = 'requires_review'
    later = fact(days=70, service='order_automation', offer='order_flow_demo')
    later.text = 'Синтетический последующий отказ от автоматизации; принадлежность записи не установлена.'
    later.confidence = 'requires_review'
    c.relationship_review = RelationshipReview(
        status='partial', identity='possible', relationship_state='unknown', checked_at=NOW,
        facts=[earlier, later], declined_offers=[later], new_requests=[],
        entities=[
            RelationshipEntity(entity='deal', id=1, title='Вымышленная ранняя сделка',
                               identity='possible', status_semantics='success'),
            RelationshipEntity(entity='deal', id=2, title='Вымышленный последующий запрос',
                               identity='possible', status_semantics='failure'),
        ], limitations=['Синтетические одноимённые записи не доказывают идентичность.'])
    result = route(c, NOW)
    assert result.category == 'D'
    assert result.offer_prospect_score is None
    assert result.need_score is None
    assert result.active_task is False
    assert result.contact_permissibility == 'needs_review'
    assert 'Совпадение CRM не разрешено' in result.next_step


def test_sanbel_control_confirmed_customer_declined_automation_needs_new_basis(candidate_v12):
    """Even confirmed identity and complete history cannot erase a refusal."""
    c = candidate_v12
    c.name = 'SANBEL — синтетический подтверждённый клиент'
    strong(c)
    review(c, owner_name='Синтетический ответственный',
           performed_services=[fact(days=300, service='crm_setup', offer='initial_setup')],
           declined_offers=[fact(days=70)], new_requests=[])
    assert c.commercial_proposal.new_basis == []
    result = route(c, NOW)
    assert_customer_hold(result)
    assert result.offer_prospect_score is None
    assert result.active_task is False
    assert result.commercial_priority == 'hold'
    assert 'ранее отклонено' in result.next_step


def test_public_brand_phone_match_to_other_legal_entity_does_not_prove_customer_identity(candidate_v12):
    """A phone match needs review when brand/legal-entity ownership conflicts."""
    c = candidate_v12
    # Duplicate detection may report an exact identifier; the relationship
    # review can still discover that the published cafe number belongs to an
    # unrelated sole proprietor. No actual cafe or CRM contact is used here.
    c.crm_check.state = 'confirmed_duplicate'
    c.relationship_review = RelationshipReview(
        status='partial', identity='possible', relationship_state='unknown', checked_at=NOW,
        entities=[RelationshipEntity(entity='contact', id=3,
                    title='Вымышленный контакт другого ИП', identity='possible')],
        limitations=['Публичный номер кафе найден у другого ИП; связь с исследуемым брендом не доказана.'])
    result = route(c, NOW)
    assert result.category == 'D'
    assert result.contact_permissibility == 'needs_review'
    assert result.offer_prospect_score is None
    assert not result.active_task


def test_confirmed_customer_keeps_category_when_crm_snapshot_needs_refresh(candidate_v12):
    review(candidate_v12)
    candidate_v12.crm_check.checked_at = NOW-timedelta(days=2)
    assert_customer_hold(route(candidate_v12, NOW))


@pytest.mark.parametrize('state', ['possible_match', 'no_match_accessible', 'not_checked', 'unavailable', 'incomplete'])
def test_confirmed_history_cannot_override_contradictory_identity_check(candidate_v12, state):
    review(candidate_v12)
    candidate_v12.crm_check.state = state
    result = route(candidate_v12, NOW)
    assert result.category == 'D'
    assert result.need_score is None and result.offer_prospect_score is None
    assert result.contact_permissibility == 'needs_review'
    assert result.commercial_priority == 'hold'
    assert not result.active_task


def test_weak_need_and_email_allow_only_local_draft_not_active_outreach(candidate_v12):
    c = candidate_v12
    c.sources[0].excerpt += ' info@business.kz'
    c.contacts.append(c.contacts[0].model_copy(update={'type': 'email', 'value': 'info@business.kz',
        'evidence': c.contacts[0].evidence.model_copy(update={'quote': 'info@business.kz'})}))
    c.evidence.append(c.evidence[0].model_copy(update={'signal': CommercialSignal.CHANNELS}))
    c.commercial_proposal.new_basis = [citation()]  # Ordinary order details are not a current event.
    result = route(c, NOW)
    assert (result.need_score, result.offer_prospect_score) == (30, 30)
    assert result.contact_score == 50  # An email improves reachability, never the commercial case.
    assert result.contact_permissibility == 'allow_draft'
    assert result.commercial_priority == 'low'
    assert result.active_task is False
    assert 'Только исследование' in result.next_step


def test_low_bitrix_need_stays_inactive_even_with_fresh_growth_basis(candidate_v12):
    c = candidate_v12
    c.crm_usage_evidence = CRMUsageAssessment(status='bitrix24', basis='explicit_usage',
        evidence=[citation('работу с клиентской базой в Bitrix24')])
    c.evidence = [c.evidence[0].model_copy(update={'quote': quote, 'signal': signal}) for quote, signal in [
        ('Новый филиал', CommercialSignal.GROWTH), ('Несколько менеджеров', CommercialSignal.TEAM),
        ('+7 (700) 123-45-67', CommercialSignal.CHANNELS)]]
    c.commercial_proposal.new_basis = [citation('Новый филиал')]
    result = route(c, NOW)
    assert (result.need_score, result.offer_prospect_score) == (20, 42)
    assert result.category == 'B'
    assert result.commercial_priority == 'low'
    assert result.contact_permissibility == 'allow_draft'
    assert not result.active_task


def test_low_new_crm_need_does_not_become_active_from_a_prepared_offer(candidate_v12):
    c = candidate_v12
    c.evidence = [c.evidence[0].model_copy(update={'quote': 'Несколько менеджеров', 'signal': CommercialSignal.TEAM})]
    result = route(c, NOW)
    assert (result.need_score, result.offer_prospect_score) == (10, 23)
    assert result.contact_permissibility == 'allow_draft'
    assert result.commercial_priority == 'low'
    assert not result.active_task


def test_strong_general_score_needs_a_current_proposal_linked_reason_to_act(candidate_v12):
    strong(candidate_v12)
    result = route(candidate_v12, NOW)
    assert (result.need_score, result.offer_prospect_score) == (100, 55)
    assert result.contact_permissibility == 'allow_draft'
    assert result.commercial_priority == 'low'
    assert not result.active_task
    candidate_v12.evidence.append(candidate_v12.evidence[0].model_copy(update={
        'quote': 'Проект автоматизации', 'signal': CommercialSignal.AUTOMATION_PROJECT}))
    candidate_v12.commercial_proposal.new_basis = [citation('Проект автоматизации')]
    result = route(candidate_v12, NOW)
    assert result.commercial_priority == 'high'
    assert result.active_task


def test_old_why_now_does_not_activate_even_a_high_score(candidate_v12):
    strong(candidate_v12)
    candidate_v12.commercial_proposal.new_basis = [citation()]
    candidate_v12.sources[0].published_on = NOW.date()-timedelta(days=31)
    result = route(candidate_v12, NOW)
    assert result.need_score == 100  # The accepted score formula is unchanged.
    assert result.commercial_priority == 'low'
    assert not result.active_task


def test_matching_recent_crm_request_can_activate_customer_work_through_owner(candidate_v12):
    review(candidate_v12, new_requests=[fact(days=2)])
    result = route(candidate_v12, NOW)
    assert result.category == 'C'
    assert result.commercial_priority == 'medium'
    assert result.active_task
    assert 'Только через ответственного' in result.next_step
    candidate_v12.relationship_review.new_requests[0].reference.date_kind = 'card_modified'
    result = route(candidate_v12, NOW)
    assert result.category == 'C'
    assert result.commercial_priority == 'low'
    assert not result.active_task


def test_future_request_timestamp_cannot_create_active_customer_work(candidate_v12):
    request = fact(days=0)
    request.reference.date = NOW+timedelta(hours=1)
    review(candidate_v12, new_requests=[request])
    result = route(candidate_v12, NOW)
    assert result.category == 'C'
    assert result.commercial_priority == 'low'
    assert not result.active_task
