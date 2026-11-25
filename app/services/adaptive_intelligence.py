"""Bounded, evidence-led adaptive planning for the shared LeadHunter app.

This layer does not search arbitrary sites, contact people, or make CRM
changes. It chooses only adapters handed to it by the application, records the
outcome of each public research attempt, and switches to a materially different
available strategy when that is justified.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from app.models.adaptive_intelligence import (
    ActionAttempt, ActionEvaluation, AdaptiveCampaignRequest, AdaptiveRunView,
    AdaptiveStrategy, CampaignAuthorization, CampaignGoal, CampaignHypothesis,
    EvidenceLink, ExperienceItem, FailureRecord,
)
from app.models.universal_lead_hunter import Candidate, RunRequest, SourcePlan
from app.services.universal_sources import SourceAdapter, VerifiedCandidate

if TYPE_CHECKING:
    from app.services.sales_store import SalesStore


RULES_DIR = Path(__file__).resolve().parents[2] / 'data/commercial_rules/versions'


@dataclass
class AdaptiveExecution:
    records: dict[str, VerifiedCandidate]
    source_ids: dict[str, str]
    failures: list[FailureRecord]
    stop_reason: str


class ExperienceRepository:
    """Loads only versioned, reviewed local lessons and aggregate outcomes.

    It intentionally does not promote model-generated observations into a rule
    file. The stored lessons are provisional context for a human review.
    """
    def __init__(self, store: SalesStore, rules_dir: Path = RULES_DIR):
        self.store, self.rules_dir = store, rules_dir

    def relevant(self, request: RunRequest) -> list[ExperienceItem]:
        material = ' '.join((request.brief.name, request.brief.description, request.brief.category)).casefold()
        items: list[ExperienceItem] = []
        latest = sorted(self.rules_dir.glob('*.json'))[-1:] if self.rules_dir.is_dir() else []
        for path in latest:
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            for rule in data.get('rules', []):
                if rule.get('kind') not in {'commercial_hypothesis', 'offer_hypothesis', 'operational_hypothesis', 'evidence_discipline'}:
                    continue
                text = str(rule.get('rule', ''))
                # CRM-specific historical learning is not silently applied to
                # unrelated products. Evidence discipline remains universal.
                if rule.get('id') != 'POL-09' and 'crm' not in material and 'битрикс' not in material:
                    continue
                rule_id = str(rule.get('id', '')).casefold().replace('_', '-')
                if not rule_id or not rule_id[0].isalpha():
                    continue
                items.append(ExperienceItem(
                    id=f'rule-{rule_id}', category='internal_success',
                    provenance=f"Versioned commercial rule {data.get('version', 'unknown')} ({rule.get('id')}).",
                    applicability=str(data.get('scope', 'Reviewed CRM Consulting commercial research context.')),
                    lesson=text, confidence=0.25 if rule.get('status') == 'unvalidated' else 0.5,
                    supporting_outcomes=[str(rule.get('evidence', 'Reviewed rule; causal effect unvalidated.'))],
                    reviewed=True,
                ))
        counts = self.store.outcome_experience_summary()
        positive = sum(counts.get(key, 0) for key in ('sale', 'meeting_booked', 'interested_reply'))
        negative = sum(counts.get(key, 0) for key in ('declined', 'opt_out'))
        if positive:
            items.append(ExperienceItem(
                id='observed-positive-outcomes', category='internal_success',
                provenance='Aggregate local SalesStore observed outcome events.',
                applicability='Planning context only; outcome records are not a causal scoring model.',
                lesson='Observed positive outcomes can justify retaining evidence-backed review paths, not automatic outreach.',
                confidence=min(0.65, 0.15 + positive / 100),
                supporting_outcomes=[f'{positive} aggregate positive observed event(s).'], reviewed=True))
        if negative:
            items.append(ExperienceItem(
                id='observed-negative-outcomes', category='internal_unsuccessful',
                provenance='Aggregate local SalesStore observed outcome events.',
                applicability='Planning context only; contact preferences and opt-outs override commercial interest.',
                lesson='Negative outcomes require a permission and relevance check before any human follow-up.',
                confidence=min(0.65, 0.15 + negative / 100),
                supporting_outcomes=[f'{negative} aggregate negative observed event(s).'], reviewed=True))
        return items[:12]


class SourceCapabilityRegistry:
    """The planner's honest view of the factories available in this run."""
    def __init__(self, adapters: dict[str, type[SourceAdapter]], plans: list[SourcePlan]):
        self.adapters, self.plans = adapters, plans

    def executable(self) -> list[SourcePlan]:
        return [entry for entry in self.plans if entry.enabled and entry.capability.id in self.adapters]

    def capability_gaps(self) -> list[SourcePlan]:
        """Known compatible capabilities that cannot honestly be executed now."""
        return [entry for entry in self.plans
                if not entry.enabled and entry.capability.status in {'requires_access', 'unavailable'}]


class AdaptiveIntelligenceEngine:
    def __init__(self, store: SalesStore, *, experience: ExperienceRepository | None = None):
        self.store = store
        self.experience = experience or ExperienceRepository(store)

    @staticmethod
    def campaign_request(request: RunRequest) -> AdaptiveCampaignRequest:
        return request.adaptive or AdaptiveCampaignRequest(enabled=True)

    def _goal(self, request: RunRequest, available: list[str]) -> CampaignGoal:
        config = self.campaign_request(request)
        is_b2c = request.brief.buyer_paths == {'individual'}
        objective = (config.current_objective or
                     (f'Research public, non-identifying demand signals for {request.brief.name}.' if is_b2c
                      else f'Find evidence-backed organizations to review for {request.brief.name}.'))
        ultimate = (config.ultimate_objective or
                    ('Prepare a consent-based B2C market-entry opportunity.' if is_b2c
                     else 'Progress evidence-backed commercial opportunities to a manager review.'))
        return CampaignGoal(
            ultimate_objective=ultimate, current_objective=objective,
            success_criteria=config.success_criteria,
            resources=available or ['No implemented source adapter is available for this context.'],
            known_information=config.known_information + [f'Product: {request.brief.name}', f'Market: {request.brief.market}'],
            unknowns=config.unknowns + ['Whether current public evidence supports the buyer hypothesis.'],
            budget=config.budget,
            authorization=CampaignAuthorization(
                public_research=config.authorization.public_research,
                # `RunRequest.crm_read_only` is itself the explicit execution
                # boundary. The adaptive layer cannot widen it, only report it.
                crm_read_only=bool(request.crm_read_only),
                external_messages=False, outbound_calls=False, crm_writes=False,
            ),
            stop_conditions=config.stop_conditions,
        )

    def _hypotheses(self, request: RunRequest, experience: list[ExperienceItem]) -> list[CampaignHypothesis]:
        material = ' '.join((request.brief.name, request.brief.description, request.brief.category)).casefold()
        crm_context = 'crm' in material or 'битрикс' in material or 'автоматизац' in material
        results = []
        for path in sorted(request.brief.buyer_paths):
            individual = path == 'individual'
            support = (
                ['A current public page identifies a source/community and a non-identifying demand signal.',
                 'Any identifiable contact arrives through explicit inbound request or opt-in evidence.']
                if individual else
                ['A named organization and market are cited on a current public page.',
                 'The same public page contains a concrete operational, growth, procurement or automation signal.']
            )
            signals = (
                ['Current public interest in the product category or practical use case.',
                 'A lawful consent-based inbound route can be prepared.']
                if individual else
                ['Expansion, procurement, recruitment, sales-process or automation change is stated publicly.',
                 'A relevant buying role or business contact is cited where publicly available.']
            )
            if crm_context and not individual and any(item.id == 'rule-hyp-04' for item in experience):
                support.append('If stated publicly, preserve the exact external course, adviser, peer, event or community awareness trigger as evidence; never infer it.')
                signals.append('An explicitly stated awareness trigger connected to sales or process control.')
            results.append(CampaignHypothesis(
                id=f'hypothesis-{path}', buyer_path=path,
                segment=(f'Individual or family users in {request.brief.market}' if individual
                         else f'Organizations and their buying roles in {request.brief.market}'),
                expected_problem=(f'People may seek practical information or a consent-based way to evaluate {request.brief.name}.' if individual
                                  else f'Organizations may need a repeatable way to address the operational need described for {request.brief.name}.'),
                purchase_motivation=request.brief.description,
                observable_signals=signals,
                supporting_evidence_requirements=support,
                contradicting_evidence_requirements=[
                    'The source explicitly identifies a different market, product use case or audience.',
                    'The publication is stale, inaccessible, contradictory, or lacks a concrete signal.',
                ],
                alternative_verification=(['Use an approved inbound/opt-in provider if one is later configured.',
                                           'Use a current public market source with explicit location evidence.'] if individual else
                                          ['Check another implemented public source with first-party verification.',
                                           'Use a manager review before treating contact availability as interest.']),
                evidence=[EvidenceLink(relation='required', note=item) for item in support],
                next_action=('Research a public demand signal; do not collect people or initiate cold contact.' if individual
                             else 'Test the highest-value available public source and retain only exact cited evidence.'),
            ))
        return results

    def _strategies(self, request: RunRequest, registry: SourceCapabilityRegistry,
                    hypotheses: list[CampaignHypothesis], experience: list[ExperienceItem]) -> list[AdaptiveStrategy]:
        config = self.campaign_request(request)
        preferred = list(request.source_ids)
        experience_ids = {item.id for item in experience}
        crm_experience = 'rule-hyp-04' in experience_ids or 'rule-hyp-05' in experience_ids
        strategies: list[AdaptiveStrategy] = []
        for entry in registry.executable():
            cap = entry.capability
            hypothesis = next((h for h in hypotheses if h.buyer_path == entry.path), None)
            if hypothesis is None:
                continue
            rank = 0.55
            if cap.id in preferred:
                rank += 0.18 - 0.01 * preferred.index(cap.id)
            # Reviewed CRM experience changes the ranking toward sources that
            # can preserve exact current page evidence, rather than merely a
            # broad news signal. This is a bounded prioritisation, not scoring.
            if crm_experience and cap.id in {'product_public', 'grounded_public_web'}:
                rank += 0.12
            if cap.id == 'evidence_intake':
                rank += 0.10
            if not config.allow_alternative_sources and cap.id not in preferred:
                continue
            strategies.append(AdaptiveStrategy(
                id=f'strategy-{cap.id}', hypothesis_id=hypothesis.id, capability_id=cap.id,
                objective=entry.objective,
                expected_result=f'Verified public evidence relevant to {request.brief.name}.',
                expected_value=min(rank, 0.95),
                recovery_options=['Use the next compatible implemented source without repeating this unchanged attempt.',
                                  'Stop with a reviewable limitation when no alternative remains.'],
            ))
        # A capability gap is a visible, non-executing strategy. It tells the
        # operator what could be added next without pretending a restricted
        # site or connector was searched in this run.
        for entry in registry.capability_gaps():
            cap = entry.capability
            hypothesis = next((h for h in hypotheses if h.buyer_path == entry.path), None)
            if hypothesis is None:
                continue
            strategies.append(AdaptiveStrategy(
                id=f'strategy-gap-{cap.id}', hypothesis_id=hypothesis.id, capability_id=cap.id,
                objective=f'Assess the access path for {cap.id} after this run.',
                expected_result='A documented capability option, not a search result.', expected_value=0,
                status='blocked', reversibility='review_required',
                recovery_options=[cap.access, 'Do not execute until the adapter and access method are reviewed.'],
            ))
        return sorted(strategies, key=lambda item: (-item.expected_value, item.capability_id))

    def initialize(self, run_id: int, request: RunRequest, plans: list[SourcePlan], adapters: dict[str, type[SourceAdapter]]):
        prior = self.store.adaptive_snapshot(run_id)
        if prior:
            return self.view(run_id)
        registry = SourceCapabilityRegistry(adapters, plans)
        experience = self.experience.relevant(request)
        goal = self._goal(request, [entry.capability.id for entry in registry.executable()])
        hypotheses = self._hypotheses(request, experience)
        strategies = self._strategies(request, registry, hypotheses, experience)
        self.store.adaptive_start(run_id, goal.model_dump(mode='json'), [item.model_dump(mode='json') for item in experience])
        for hypothesis in hypotheses:
            self.store.adaptive_save_hypothesis(run_id, hypothesis.model_dump(mode='json'))
        for strategy in strategies:
            self.store.adaptive_save_strategy(run_id, strategy.model_dump(mode='json'))
        gaps = [entry.capability for entry in registry.capability_gaps()]
        self.store.adaptive_journal(run_id, 'campaign_initialized', {
            'goal': goal.current_objective,
            'available_capabilities': [entry.capability.id for entry in registry.executable()],
            'experience_applied': [item.id for item in experience],
            'external_actions_disabled': True,
        })
        if gaps:
            self.store.adaptive_journal(run_id, 'capability_gaps_identified', {
                'gaps': [{'id': item.id, 'status': item.status, 'access': item.access} for item in gaps],
                'next_action': 'Use only a reviewed adapter and authorized access; no unavailable source was executed.',
            })
        return self.view(run_id)

    def view(self, run_id: int) -> AdaptiveRunView | None:
        raw = self.store.adaptive_snapshot(run_id)
        if raw is None:
            return None
        lessons = [entry['payload'].get('lesson', '') for entry in raw['journal'] if entry['kind'] == 'lesson']
        return AdaptiveRunView(
            run_id=run_id, status=raw['status'], goal=CampaignGoal.model_validate(raw['goal']),
            hypotheses=[CampaignHypothesis.model_validate(item) for item in raw['hypotheses']],
            strategies=[AdaptiveStrategy.model_validate(item) for item in raw['strategies']],
            attempts=[ActionAttempt.model_validate(item) for item in raw['attempts']],
            experience=[ExperienceItem.model_validate(item) for item in raw['experience']],
            lessons=[item for item in lessons if item], journal=raw['journal'], stop_reason=raw['stop_reason'])

    def _save_hypothesis(self, run_id: int, hypothesis: CampaignHypothesis):
        self.store.adaptive_save_hypothesis(run_id, hypothesis.model_dump(mode='json'))

    def _save_strategy(self, run_id: int, strategy: AdaptiveStrategy):
        self.store.adaptive_save_strategy(run_id, strategy.model_dump(mode='json'))

    @staticmethod
    def _candidate_key(candidate: Candidate) -> str:
        return f'{candidate.path}:{candidate.label.casefold().strip()}'

    @staticmethod
    def _failure(source_id: str, exc: Exception | None = None) -> FailureRecord:
        name = type(exc).__name__ if exc else ''
        message = str(exc).casefold() if exc else ''
        if 'permission' in message or 'not enabled' in message or 'configuration' in message:
            classification, recovery = 'missing_permission', 'Keep the source blocked and use another implemented source.'
        elif name in {'IntelligenceError', 'PublicSourceError'} or 'unavailable' in message:
            classification, recovery = 'unavailable_source', 'Switch to a different compatible implemented source.'
        else:
            classification, recovery = 'temporary_technical', 'Switch to a different compatible source; retry only after a material condition changes.'
        return FailureRecord(id=f'failure-{source_id}', classification=classification, source_id=source_id,
                             summary='The configured public-source attempt did not complete.', recovery_action=recovery)

    async def execute(self, run_id: int, request: RunRequest, plans: list[SourcePlan],
                      adapters: dict[str, type[SourceAdapter]]) -> AdaptiveExecution:
        view = self.initialize(run_id, request, plans, adapters)
        assert view is not None
        self.store.adaptive_set_campaign(run_id, status='running')
        config = self.campaign_request(request)
        records: dict[str, VerifiedCandidate] = {}
        sources: dict[str, str] = {}
        failures: list[FailureRecord] = []
        attempted = {item.capability_id: item for item in view.attempts}

        # Rehydrate public verified results from a previous completed attempt;
        # do not issue the source request again after a worker restart.
        for item in view.attempts:
            if item.status in {'succeeded', 'partial'} and item.verified_candidates:
                if not item.reused_after_restart:
                    item = item.model_copy(update={'reused_after_restart': True})
                    self.store.adaptive_save_attempt(run_id, item.model_dump(mode='json'))
                for raw in item.verified_candidates:
                    candidate = Candidate.model_validate(raw)
                    key = self._candidate_key(candidate)
                    records.setdefault(key, VerifiedCandidate(candidate))
                    sources.setdefault(key, item.capability_id)

        strategies = sorted(view.strategies, key=lambda item: (-item.expected_value, item.capability_id))
        hypotheses = {item.id: item for item in view.hypotheses}
        stop_reason = ''
        sequence = max((attempt.sequence for attempt in view.attempts), default=0)
        campaign_started = monotonic()
        for strategy in strategies:
            if strategy.status == 'blocked':
                continue
            if len(records) >= request.limit:
                stop_reason = 'Success criterion reached: enough evidence-backed results were retained.'
                break
            if monotonic() - campaign_started >= config.budget.max_seconds:
                failure = FailureRecord(id='failure-time-budget', classification='budget_exhausted',
                                        summary='The configured adaptive research time budget was exhausted.',
                                        recovery_action='Review the completed evidence and approve a new bounded run if justified.')
                failures.append(failure)
                stop_reason = 'Research time budget exhausted before the candidate target was reached.'
                break
            if sequence >= config.budget.max_attempts:
                failure = FailureRecord(id='failure-budget', classification='budget_exhausted',
                                        summary='The bounded number of public research attempts was exhausted.',
                                        recovery_action='Review the evidence and approve a new budget or source capability.')
                failures.append(failure)
                stop_reason = 'Research attempt budget exhausted before the candidate target was reached.'
                break
            prior = attempted.get(strategy.capability_id)
            if prior and prior.status in {'succeeded', 'partial', 'failed', 'blocked'}:
                continue
            factory = adapters.get(strategy.capability_id)
            if factory is None:
                failure = FailureRecord(id=f'failure-{strategy.capability_id}', classification='unsupported_adapter',
                                        source_id=strategy.capability_id,
                                        summary='The capability is listed but no executable adapter factory is available for this run.',
                                        recovery_action='Use a different implemented capability or add a reviewed adapter.')
                failures.append(failure)
                continue
            sequence += 1
            selected = strategy.model_copy(update={'status': 'selected'})
            self._save_strategy(run_id, selected)
            started = ActionAttempt(sequence=sequence, strategy_id=strategy.id, capability_id=strategy.capability_id,
                                    status='running', expected_result=strategy.expected_result)
            self.store.adaptive_save_attempt(run_id, started.model_dump(mode='json'))
            self.store.adaptive_journal(run_id, 'strategy_selected', {
                'strategy_id': strategy.id, 'capability_id': strategy.capability_id,
                'expected_result': strategy.expected_result,
            })
            began = monotonic()
            try:
                adapter = factory()
                discovered = await adapter.discover(request)
                verified = await adapter.verify(request, discovered)
            except Exception as exc:
                failure = self._failure(strategy.capability_id, exc)
                failures.append(failure)
                outcome = ActionAttempt(sequence=sequence, strategy_id=strategy.id, capability_id=strategy.capability_id,
                                        status='failed', expected_result=strategy.expected_result,
                                        actual_result='No verified result: source attempt failed.', failures=[failure],
                                        elapsed_ms=int((monotonic() - began) * 1000),
                                        evaluation=ActionEvaluation(expected_result=strategy.expected_result,
                                            actual_result='The source failed before evidence could be verified.', evidence_quality='none',
                                            alternative_explanations=['Temporary endpoint failure, unavailable configuration, or access restriction.'],
                                            next_action='Try the next compatible implemented strategy.'))
                self.store.adaptive_save_attempt(run_id, outcome.model_dump(mode='json'))
                self.store.adaptive_journal(run_id, 'failure_recovery', failure.model_dump(mode='json'))
                continue
            retained = []
            for record in verified.records:
                candidate = record.candidate
                key = self._candidate_key(candidate)
                if key in records:
                    continue
                records[key] = record
                sources[key] = strategy.capability_id
                retained.append(record)
                if len(records) >= request.limit:
                    break
            hypothesis = hypotheses[strategy.hypothesis_id]
            if retained:
                links = list(hypothesis.evidence)
                for record in retained:
                    for evidence in record.candidate.evidence:
                        if evidence.kind in {'need', 'market_signal', 'inbound_request', 'opt_in'}:
                            links.append(EvidenceLink(candidate_id=record.candidate.id, evidence_id=evidence.id,
                                                      relation='supports', note='Exact public or supplied evidence supports the provisional hypothesis.'))
                updated = hypothesis.model_copy(update={
                    'state': 'supported', 'confidence': min(0.8, hypothesis.confidence + 0.2),
                    'evidence': links[:40], 'next_action': 'Manager review of evidence, CRM relationship and contact permission.',
                    'change_reason': 'A distinct source returned evidence-backed records.',
                })
                self._save_hypothesis(run_id, updated)
                hypotheses[updated.id] = updated
                actual = f'{len(retained)} new verified record(s) retained.'
                evaluation = ActionEvaluation(expected_result=strategy.expected_result, actual_result=actual,
                    evidence_quality='verifiable', alternative_explanations=[],
                    next_action='Assess the next available strategy only if the target remains unmet.')
                status = 'partial' if discovered.incomplete or verified.incomplete else 'succeeded'
            else:
                failure = FailureRecord(id=f'failure-empty-{strategy.capability_id}', classification='insufficient_evidence',
                                        source_id=strategy.capability_id,
                                        summary='The source completed but did not produce a record meeting the evidence requirements.',
                                        recovery_action='Revise the provisional hypothesis and test a different implemented source.')
                failures.append(failure)
                updated = hypothesis.model_copy(update={
                    'revision': hypothesis.revision + 1, 'state': 'insufficient_evidence',
                    'confidence': max(0.05, hypothesis.confidence - 0.15),
                    'evidence': [*hypothesis.evidence, EvidenceLink(relation='missing', note=f'No supporting record from {strategy.capability_id}.')][:40],
                    'next_action': 'Test an alternative verification path; do not treat lack of results as evidence of absence.',
                    'change_reason': f'{strategy.capability_id} produced no record satisfying the required evidence.',
                })
                self._save_hypothesis(run_id, updated)
                hypotheses[updated.id] = updated
                actual = 'No verified record met the stated evidence requirements.'
                evaluation = ActionEvaluation(expected_result=strategy.expected_result, actual_result=actual,
                    evidence_quality='none', alternative_explanations=['The source may not cover this market or the hypothesis may be weak.'],
                    next_action='Select a materially different compatible strategy.')
                status = 'partial'
            outcome = ActionAttempt(sequence=sequence, strategy_id=strategy.id, capability_id=strategy.capability_id,
                                    status=status, expected_result=strategy.expected_result, actual_result=actual,
                                    candidate_ids=[record.candidate.id for record in retained],
                                    failures=[failure] if not retained else [], evaluation=evaluation,
                                    elapsed_ms=int((monotonic() - began) * 1000),
                                    verified_candidates=[record.candidate.model_dump(mode='json') for record in retained])
            self.store.adaptive_save_attempt(run_id, outcome.model_dump(mode='json'))
            self.store.adaptive_journal(run_id, 'action_evaluated', outcome.model_dump(mode='json'))
            strategy = strategy.model_copy(update={'status': 'attempted'})
            self._save_strategy(run_id, strategy)

        if not stop_reason:
            stop_reason = ('All feasible implemented strategies were attempted; no additional evidence was fabricated.'
                           if len(records) < request.limit else
                           'Success criterion reached: enough evidence-backed results were retained.')
        final_status = 'completed' if len(records) >= request.limit else ('partial' if records else 'stopped')
        self.store.adaptive_set_campaign(run_id, status=final_status, stop_reason=stop_reason)
        lesson = ('Retain the evidence-backed result and require manager review.' if records else
                  'No verified result was produced; preserve the attempted strategies and use the stated next capability or evidence requirement.')
        self.store.adaptive_journal(run_id, 'lesson', {'lesson': lesson, 'stop_reason': stop_reason,
                                                       'records_retained': len(records), 'attempts': sequence})
        return AdaptiveExecution(records=records, source_ids=sources, failures=failures, stop_reason=stop_reason)
