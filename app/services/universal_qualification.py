"""Review decisions shared by the organization and individual workflows."""

import re

from app.models.universal_lead_hunter import (
    BuyerPath, BuyerRole, EngagementPlan, InteractionEvent, Opportunity, Qualification, RunRequest,
)

ROLES = {
    'organization': (
        ('user', 'Team using the product'), ('decision_maker', 'Budget or purchase approver'),
        ('payer', 'Budget owner'), ('influencer', 'Cross-functional stakeholder'),
        ('procurement', 'Purchasing or supplier manager'), ('technical_specialist', 'Technical evaluator'),
        ('department_head', 'Department or process lead'), ('owner', 'Business owner or executive'),
    ),
    'individual': (
        ('user', 'Person using the product'), ('decision_maker', 'User or authorized family member'),
        ('payer', 'User or family member funding the purchase'),
        ('influencer', 'Trusted adviser chosen by the user'), ('caregiver', 'Caregiver if one is involved'),
    ),
}
QUESTIONS = {
    'organization': ['Who owns the process and specifies requirements?', 'Who approves a supplier and controls the budget?'],
    'individual': ['Are the user, decision-maker, and payer the same person?', 'Who may participate with the user’s permission?'],
}


def understand(request: RunRequest) -> tuple[str, list[BuyerPath]]:
    brief = request.brief
    purpose = re.search(r'\b(?:helps?|designed to|used to|allows?|позволяет|помогает|чтобы)\s+([^.!?]+)',
                        brief.description, flags=re.IGNORECASE)
    if brief.problem_hint:
        problem = f'Validate the stated problem: {brief.problem_hint}'
    elif purpose:
        problem = f'Investigate what prevents the buyer from achieving this outcome: {purpose[1].strip()}'
    else:
        problem = f'Clarify the buyer’s task, current alternative and constraint. Seller context: {brief.description}'
    paths = [BuyerPath(kind=kind, roles=[BuyerRole(role=role, actor=actor) for role, actor in ROLES[kind]],
                       unresolved_questions=QUESTIONS[kind]) for kind in sorted(brief.buyer_paths)]
    return problem, paths


def qualify(request, candidate, legacy_state=None):
    kinds = {item.kind for item in candidate.evidence}
    inbound = any(item.kind == 'inbound_request' and item.channel for item in candidate.evidence)
    if candidate.subject_kind == 'public_demand_signal':
        state, reason = 'needs_review', ('Public demand evidence is not an identifiable buyer, '
                                         'an opt-in, or permission to contact anyone.')
    elif 'opt_out' in kinds:
        state, reason = 'excluded', 'An opt-out is recorded for this candidate.'
    elif legacy_state:
        state, reason = legacy_state
    elif request.mode == 'fixture' and (
        (candidate.path == 'organization' and {'identity', 'need', 'role'} <= kinds)
        or (candidate.path == 'individual' and inbound and 'need' in kinds)
    ):
        state, reason = 'qualified', 'Synthetic evidence meets the fixture review criteria.'
    else:
        state, reason = 'needs_review', 'Supplied evidence needs independent review before qualification.'
    # An inquiry shows interest in the topic. It does not establish a purchase or a meeting.
    return Qualification(state=state, reasons=[reason], purchase_interest='expressed' if inbound else 'unknown')


def build_opportunity(request, candidate, qualification, *, source_id, crm_check='not_checked', relationship='unknown', crm_owner=None):
    evidence = candidate.evidence
    history = [InteractionEvent(kind=item.kind, evidence_id=item.id, at=item.observed_at,
                                channel=item.channel, basis=item.basis)
               for item in evidence if item.kind in {'inbound_request', 'opt_in', 'opt_out'}]
    inbound = next((event for event in history if event.kind == 'inbound_request' and event.channel), None)
    contactability = 'unknown'
    if candidate.path == 'organization' and any(
        item.kind == 'business_contact' and item.basis in {'public_page', 'synthetic'} for item in evidence
    ):
        contactability = 'public_business_channel' if request.mode == 'research' else 'unknown'
    if inbound:
        contactability = 'synthetic_inbound_channel' if request.mode == 'fixture' else 'reported_inbound_channel'

    questions = list(QUESTIONS[candidate.path])
    if candidate.subject_kind == 'public_demand_signal':
        questions = ['Which consent-based inbound channel can this demand signal inform?',
                     'What product claim, price, safety information or landing-page evidence still needs review?']
        first = ('Use the public signal only to prepare a consent-based offer, public content, '
                 'advertising hypothesis or inbound form. Do not identify or contact individuals from the source.')
        follow = 'Measure only opted-in requests and their permitted channel before any personal follow-up.'
        channel = 'manager_review'
    elif candidate.path == 'organization':
        first = 'Ask the verified process owner for requirements and routing to procurement after approval.'
        follow = 'If invited, share a sample or demonstration and confirm the purchasing decision path.'
        channel = 'manager_review'
        if qualification.state == 'needs_review' or relationship not in {'unknown', 'no_history'}:
            first = 'Confirm the organization, buyer roles, prior relationship and any contact restrictions internally.'
    else:
        first = 'Review the inquiry and draft a response to the stated request on the original channel.' if inbound else (
            'Wait for an inbound request or a separately verified permission record.')
        follow = 'Clarify the request with the user. Include a family decision-maker or payer only with permission.'
        channel = 'inbound_reply' if inbound else 'manager_review'
    observed_opt_in = inbound and any(event.kind == 'opt_in' and event.channel == inbound.channel and event.at
                                      for event in history)
    permission = 'blocked' if candidate.subject_kind == 'public_demand_signal' else 'review_only'
    if candidate.subject_kind == 'public_demand_signal':
        pass
    elif qualification.state == 'excluded' or (candidate.path == 'individual' and not inbound):
        permission = 'blocked'
        channel = 'manager_review'
        first = 'Record the restriction and retain the case for internal review.' if qualification.state == 'excluded' else first
        follow = 'Keep the restriction in force until an authorized reviewer resolves it.'
    elif request.mode == 'fixture' and observed_opt_in:
        permission = 'synthetic_opt_in'

    reference = next((item.id for item in evidence if item.kind == 'need'), None)
    inferred_need = f'Check whether {request.brief.name} addresses the candidate’s requirements.'
    if candidate.subject_kind == 'public_demand_signal':
        inferred_need = ('Validate whether this public market-context signal justifies a consent-based '
                         'inbound experiment; it does not establish an individual need or purchase intent.')
    if reference:
        inferred_need += f' Review evidence {reference} and confirm fit, timing, and budget.'
    else:
        questions.append('What evidence ties this candidate to the product’s use case?')
    return Opportunity(candidate=candidate, source_id=source_id, product_category=request.brief.category, crm_relationship=relationship,
        crm_check=crm_check, crm_owner=crm_owner, inferred_need=inferred_need, unresolved_questions=questions, qualification=qualification,
        contactability=contactability, engagement=EngagementPlan(channel=channel, permission=permission,
        reply_channel=inbound.channel if channel == 'inbound_reply' else None, first_action=first, follow_up=follow),
        interaction_history=history, owner=request.owner, next_action=first)
