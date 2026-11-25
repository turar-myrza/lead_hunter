"""Behavioural tests for the bounded adaptive LeadHunter loop."""
from pathlib import Path

import pytest

from app.models.adaptive_intelligence import AdaptiveCampaignRequest, CampaignBudget
from app.models.universal_lead_hunter import Candidate, Evidence, ProductBrief, RunRequest, SourceCapability
from app.services.sales_store import SalesStore
from app.services.universal_lead_hunter import LeadHunterApplication
from app.services.universal_sources import Discovery, Verification, VerifiedCandidate


class EmptyPublicSource:
    capability = SourceCapability(id='empty_public', status='implemented', access='Fixture public source.',
                                  paths=['organization'], modes=['research'])

    async def discover(self, request):
        return Discovery([])

    async def verify(self, request, discovery):
        return Verification([])


class EvidencePublicSource:
    capability = SourceCapability(id='evidence_public', status='implemented', access='Fixture public source.',
                                  paths=['organization'], modes=['research'])

    async def discover(self, request):
        return Discovery(['fixture'])

    async def verify(self, request, discovery):
        candidate = Candidate(
            id='fixture-organization', label='Fixture bakery', path='organization', location='Warsaw',
            evidence=[
                Evidence(id='identity', kind='identity', excerpt='Fixture bakery in Warsaw.', basis='public_page'),
                Evidence(id='need', kind='need', excerpt='The bakery seeks safer packaging for delivery orders.', basis='public_page'),
            ],
        )
        return Verification([VerifiedCandidate(candidate)])


class BrokenSource:
    capability = SourceCapability(id='broken_public', status='implemented', access='Fixture public source.',
                                  paths=['organization'], modes=['research'])

    async def discover(self, request):
        raise RuntimeError('temporary test endpoint failure')

    async def verify(self, request, discovery):  # pragma: no cover - must not run
        raise AssertionError('verification must not run')


def request(*source_ids):
    return RunRequest(
        brief=ProductBrief(name='Cake trays', description='Rigid trays for safe cake transport.',
                           category='Packaging', market='Warsaw', buyer_paths={'organization'}),
        mode='research', source_ids=list(source_ids), limit=1,
        adaptive=AdaptiveCampaignRequest(
            current_objective='Find one evidence-backed bakery opportunity.',
            budget=CampaignBudget(max_attempts=3),
        ),
    )


@pytest.fixture
def service(tmp_path):
    return LeadHunterApplication(SalesStore(tmp_path / 'adaptive.sqlite3'), tmp_path / 'artifacts',
                                 adapters=(EmptyPublicSource, EvidencePublicSource, BrokenSource))


async def test_empty_hypothesis_revises_and_uses_a_distinct_available_strategy(service):
    run_id = service.submit(request('empty_public'))
    await service.process(run_id)

    result = service.result(run_id)
    adaptive = service.adaptive_status(run_id)
    assert result and [item.source_id for item in result.opportunities] == ['evidence_public']
    assert adaptive.attempts[0].capability_id == 'empty_public'
    assert adaptive.attempts[-1].capability_id == 'evidence_public'
    assert len({item.capability_id for item in adaptive.attempts}) > 1
    assert adaptive.attempts[0].failures[0].classification == 'insufficient_evidence'
    hypothesis, = adaptive.hypotheses
    assert hypothesis.state == 'supported' and hypothesis.revision == 2
    assert adaptive.stop_reason and 'Success criterion reached' in adaptive.stop_reason
    assert all(not cap.authorized and not cap.implemented for cap in result.action_capabilities)


async def test_failed_source_records_recovery_then_uses_next_strategy(service):
    run_id = service.submit(request('broken_public'))
    await service.process(run_id)

    adaptive = service.adaptive_status(run_id)
    assert [item.capability_id for item in adaptive.attempts][:2] == ['broken_public', 'empty_public']
    assert adaptive.attempts[0].failures[0].classification == 'temporary_technical'
    assert any(item.capability_id == 'evidence_public' for item in adaptive.attempts)
    assert service.result(run_id).opportunities


async def test_b2c_does_not_require_an_organization_and_stops_without_fabrication(tmp_path):
    from app.services.universal_sources import EvidenceIntake

    service = LeadHunterApplication(SalesStore(tmp_path / 'b2c.sqlite3'), tmp_path / 'artifacts', adapters=(EvidenceIntake,))
    request = RunRequest(
        brief=ProductBrief(name='Decompression belt', description='Consumer support product.',
                           category='Consumer product', market='Shymkent', buyer_paths={'individual'}),
        mode='research', limit=2,
        adaptive=AdaptiveCampaignRequest(current_objective='Research public demand without collecting people.'),
    )
    run_id = service.submit(request)
    await service.process(run_id)
    adaptive = service.adaptive_status(run_id)
    assert service.result(run_id).opportunities == []
    assert adaptive.status == 'stopped'
    assert 'All feasible implemented strategies' in adaptive.stop_reason
    assert not any('company' in item.goal.current_objective.casefold() for item in [adaptive])


async def test_resume_reuses_completed_public_evidence_without_repeating_adapter(service):
    calls = {'count': 0}

    class OnceSource(EvidencePublicSource):
        capability = EvidencePublicSource.capability.model_copy(update={'id': 'once_public'})

        async def discover(self, run_request):
            calls['count'] += 1
            return await super().discover(run_request)

    resumed = LeadHunterApplication(service.store, service.output_root, adapters=(OnceSource,))
    run_id = resumed.submit(request('once_public'))
    await resumed.process(run_id)
    assert calls['count'] == 1
    # Simulate a safe worker recovery. The universal step checkpoint is cleared,
    # but the adaptive public result stays in its separate append-only ledger.
    with resumed.store.connect() as db:
        db.execute("UPDATE run SET status='running' WHERE id=?", (run_id,))
    await resumed.process(run_id)
    assert calls['count'] == 1
    assert resumed.adaptive_status(run_id).attempts[0].reused_after_restart is True


async def test_reviewed_crm_experience_changes_strategy_ranking(tmp_path):
    class CRMFeed(EmptyPublicSource):
        capability = EmptyPublicSource.capability.model_copy(update={
            'id': 'shymkent_crm', 'context': 'crm_sales_operations', 'markets': ['Shymkent'],
        })

    class ProductPage(EvidencePublicSource):
        capability = EvidencePublicSource.capability.model_copy(update={'id': 'product_public'})

    service = LeadHunterApplication(SalesStore(tmp_path / 'experience.sqlite3'), tmp_path / 'artifacts',
                                    adapters=(CRMFeed, ProductPage))
    crm_request = RunRequest(
        brief=ProductBrief(name='CRM implementation', description='Automate sales handoffs and customer communications.',
                           category='CRM services', market='Shymkent', buyer_paths={'organization'}),
        mode='research', research_context='crm_sales_operations', limit=1,
        adaptive=AdaptiveCampaignRequest(current_objective='Find current CRM implementation evidence.'),
    )
    run_id = service.submit(crm_request)
    await service.process(run_id)
    adaptive = service.adaptive_status(run_id)
    assert 'rule-hyp-04' in {item.id for item in adaptive.experience}
    hypothesis, = adaptive.hypotheses
    assert any('awareness trigger' in item.casefold() for item in hypothesis.supporting_evidence_requirements)
    assert adaptive.attempts[0].capability_id == 'product_public'
