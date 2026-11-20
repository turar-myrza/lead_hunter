from datetime import datetime, timezone
import json
from pathlib import Path
from zipfile import is_zipfile

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.api.universal_lead_hunter import get_application
from app.core.config import Settings, get_settings
from app.integrations.lead_hunter_crm import CRMReadError, LeadHunterCRMClient
from app.main import app
from app.models.universal_lead_hunter import ProductBrief, RunRequest
from app.services import universal_sources
from app.services.lead_hunter_discovery import DiscoveryFinding, DiscoveryResult, ALMATY
from app.services.sales_store import SalesStore
from app.services.universal_lead_hunter import LeadHunterApplication


FIXTURES = Path(__file__).parent / 'fixtures/universal'


def payload(name):
    return json.loads((FIXTURES / f'{name}.json').read_text())


@pytest.fixture
def service(tmp_path):
    return LeadHunterApplication(SalesStore(tmp_path / 'universal.sqlite3'), tmp_path / 'artifacts')


@pytest.mark.parametrize(('name', 'role', 'channel'), [
    ('cake_trays', 'procurement', 'manager_review'),
    ('decompression_belt', 'payer', 'inbound_reply'),
])
async def test_shared_fixture_journey(service, name, role, channel, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail('Fixture accessed an external service')

    monkeypatch.setattr(universal_sources.PublicFetcher, 'fetch', unexpected)
    service.crm_reader = unexpected
    run_id = service.submit(RunRequest.model_validate(payload(name)))
    assert service.result(run_id) is None
    assert [step.status for step in service.status(run_id).stages] == ['pending'] * 8
    await service.process(run_id)
    reloaded = LeadHunterApplication(SalesStore(service.store.path), service.output_root)
    status = reloaded.status(run_id)
    result = reloaded.result(run_id)
    opportunity, = result.opportunities
    assert status.status == 'completed' and all(step.status == 'completed' for step in status.stages)
    assert result.mode == 'fixture' and result.problem_hypothesis
    assert opportunity.owner == payload(name)['owner'] and opportunity.next_action
    assert opportunity.inferred_need and opportunity.unresolved_questions
    assert opportunity.source_id == 'evidence_intake'
    assert opportunity.qualification.state == 'qualified'
    assert opportunity.crm_check == 'not_checked' and opportunity.crm_relationship == 'unknown'
    assert any(item.role == role and item.evidence_ids for item in opportunity.candidate.roles)
    assert all(item.basis == 'synthetic' for item in opportunity.candidate.evidence)
    assert opportunity.engagement.channel == channel and opportunity.engagement.follow_up
    assert not any(cap.authorized or cap.implemented for cap in result.action_capabilities)
    if name == 'decompression_belt':
        assert opportunity.engagement.permission == 'synthetic_opt_in'
        assert opportunity.engagement.reply_channel == 'email'
        assert {e.kind for e in opportunity.interaction_history} == {'inbound_request', 'opt_in'}
    else:
        assert opportunity.engagement.permission == 'review_only' and not opportunity.interaction_history


@pytest.mark.parametrize(('name', 'description', 'category'), [
    ('CRM implementation', 'Set up a shared workflow for sales handoffs and follow-up.', 'Services'),
    ('Adult diapers', 'Absorbent care supplies offered with a size and fit guide.', 'Care supplies'),
])
def test_universal_brief_validation(name, description, category):
    brief = ProductBrief(name=name, description=description, category=category, market='Kazakhstan')
    assert brief.buyer_paths == {'organization', 'individual'}


async def test_research_intake_keeps_permission_unverified_and_respects_opt_out(service):
    request = payload('decompression_belt')
    request['mode'] = 'research'
    run_id = service.submit(RunRequest.model_validate(request))
    await service.process(run_id)
    row, = service.result(run_id).opportunities
    assert row.qualification.state == 'needs_review' and row.engagement.permission == 'review_only'
    assert row.contactability == 'reported_inbound_channel'
    request['supplied_candidates'][0]['evidence'].append({'id': 'stop', 'kind': 'opt_out', 'excerpt': 'Stop replying.'})
    run_id = service.submit(RunRequest.model_validate(request))
    await service.process(run_id)
    row, = service.result(run_id).opportunities
    assert row.qualification.state == 'excluded' and row.engagement.permission == 'blocked'


@pytest.mark.parametrize('change', ['crm', 'provenance', 'role_evidence'])
def test_rejects_forged_or_unsafe_fixture_input(change):
    request = payload('cake_trays')
    if change == 'crm':
        request['crm_read_only'] = True
    elif change == 'provenance':
        request['supplied_candidates'][0]['evidence'][0]['basis'] = 'public_page'
    else:
        request['supplied_candidates'][0]['roles'][0]['actor'] = 'Unaffiliated person'
    with pytest.raises(ValidationError):
        RunRequest.model_validate(request)


async def test_unavailable_source_is_persisted_as_structured_partial(service):
    request = payload('cake_trays')
    request['source_ids'] = ['job_boards']
    run_id = service.submit(RunRequest.model_validate(request))
    await service.process(run_id)
    status = service.status(run_id)
    assert status.status == 'partial'
    assert any(e.code == 'source_not_enabled' and e.source == 'job_boards' for e in status.stages[1].errors)
    assert service.result(run_id).opportunities == []


async def test_adapts_existing_verification_routing_and_workbook(service, monkeypatch):
    from app.integrations.lead_hunter_public import PublicResponse

    now = datetime.now(timezone.utc)
    date = now.astimezone(ALMATY).date()
    url = 'https://publication.kz/news/123'
    html = f'''<meta property="article:published_time" content="{date}T00:00:00+05:00">
        <h1>Новости производства</h1><p>ТОО «Учебный пример» открыл новый складской комплекс в Шымкенте.</p>'''

    async def collect(*args, **kwargs):
        return DiscoveryResult(findings=[DiscoveryFinding(url=url, lane='A', provider='fixture', found_at=now)],
                               providers_succeeded=['fixture'])

    async def fetch(self, candidate_url, **kwargs):
        assert candidate_url == url
        return PublicResponse(url, 200, 'text/html', html.encode(), now)

    monkeypatch.setattr(universal_sources, 'collect_findings', collect)
    monkeypatch.setattr(universal_sources.PublicFetcher, 'fetch', fetch)
    request = RunRequest.model_validate({'research_context': 'crm_sales_operations', 'brief': {
        'name': 'Workflow implementation', 'category': 'Services', 'description': 'Improve operational visibility.',
        'market': 'Shymkent', 'buyer_paths': ['organization']}})
    run_id = service.submit(request)
    await service.process(run_id)
    result = service.result(run_id)
    row, = result.opportunities
    assert row.candidate.evidence[0].basis == 'public_page'
    assert row.qualification.state == 'needs_review' and row.crm_check == 'not_checked'
    assert result.artifacts == ['lead-hunter.xlsx'] and is_zipfile(service.artifact(run_id, 'lead-hunter.xlsx'))
    assert service.artifact(run_id, '../lead-hunter.xlsx') is None


async def test_sales_adapter_calls_existing_crm_reader_and_exporter(service, monkeypatch):
    import httpx

    from app.models.sales_team import Prospect
    from app.services import sales_crm

    today = datetime.now().date()
    prospect = Prospect(name='Example Factory', city='Astana', industry='Machinery manufacturing', product='A',
        fact='An additional production line opened.', ethical='Допустимо по профилю',
        sources=[{'url': 'https://example.org/news', 'fact': 'An additional production line opened.', 'observed_at': today},
                 {'url': 'https://example.org/contacts', 'fact': 'Office email: sales@example.org', 'observed_at': today,
                  'evidence_type': 'company_contact'}], emails=['sales@example.org'],
        hypothesis='Confirm how order requests are assigned.', offer='Demonstrate a test order.',
        next_step='Review with the account manager.', process='New order', visual_steps=['Receive', 'Assign', 'Confirm'],
        outcome='Confirm who owns the next action.')

    async def discovery():
        return [prospect], [{'status': 'available'}]

    reads = []

    def read_only_mock(req):
        reads.append(req.url.path.rsplit('/', 1)[-1])
        assert reads[-1] in {'crm.company.list.json', 'crm.contact.list.json', 'crm.lead.list.json', 'crm.deal.list.json'}
        return httpx.Response(200, json={'result': []})

    monkeypatch.setattr(universal_sources, 'discover_national', discovery)
    monkeypatch.setattr(sales_crm, 'get_settings', lambda: Settings(_env_file=None, bitrix24_webhook_url='https://fixture.kz/rest/1/fixture'))
    monkeypatch.setattr(sales_crm, 'LeadHunterCRMClient', lambda url, **kw:
        LeadHunterCRMClient(url, pause=0, transport=httpx.MockTransport(read_only_mock), **kw))
    request = RunRequest.model_validate({'research_context': 'crm_sales_operations', 'crm_read_only': True, 'brief': {
        'name': 'Workflow implementation', 'category': 'Services', 'description': 'Improve operational visibility.',
        'market': 'Kazakhstan', 'buyer_paths': ['organization']}})
    run_id = service.submit(request)
    assert service.store.claim() is None
    await service.process(run_id)
    assert service.status(run_id).status == 'completed'
    result = service.result(run_id)
    row, = result.opportunities
    assert len(reads) == 4 and row.crm_check == 'no_match_accessible'
    assert row.contactability == 'public_business_channel' and row.engagement.permission == 'review_only'
    assert row.qualification.state == 'qualified'
    assert result.artifacts == ['sales-team.xlsx'] and is_zipfile(service.artifact(run_id, 'sales-team.xlsx'))


def test_api_and_cli_share_runs(service, monkeypatch, capsys):
    from app.lead_hunter import main

    settings = Settings(_env_file=None, lead_hunter_api_token='test-token',
                        lead_hunter_db=service.store.path, lead_hunter_output=service.output_root)
    app.dependency_overrides[get_application] = lambda: service
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            prefix = '/api/v1/lead-hunter'
            assert client.get(f'{prefix}/sources').status_code == 401
            headers = {'Authorization': 'Bearer test-token'}
            response = client.post(f'{prefix}/runs', json=payload('cake_trays'), headers=headers)
            assert response.status_code == 202 and response.json()['status'] == 'queued'
            run_id = response.json()['run_id']
            assert client.get(f'{prefix}/runs/{run_id}', headers=headers).json()['status'] == 'completed'
            assert client.get(f'{prefix}/runs/{run_id}/result', headers=headers).json()['run_id'] == run_id
            assert client.get(f'{prefix}/runs/404/result', headers=headers).status_code == 404

            monkeypatch.setattr('sys.argv', ['lead_hunter', 'universal', '--db', str(service.store.path),
                '--output-root', str(service.output_root), 'submit', str(FIXTURES / 'decompression_belt.json')])
            main()
            cli_id = json.loads(capsys.readouterr().out)['run_id']
            data = client.get(f'{prefix}/runs/{cli_id}/result', headers=headers).json()
            assert data['brief']['name'] == 'Decompression belt'
    finally:
        app.dependency_overrides.pop(get_application)
        app.dependency_overrides.pop(get_settings)


async def test_crm_allowlist_blocks_write_before_transport():
    import httpx

    def unexpected(_request):
        pytest.fail('Write reached the transport')

    async with LeadHunterCRMClient('https://fixture.kz/rest/1/fixture', transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(CRMReadError, match='READ ONLY'):
            await client.read('crm.deal.add', {})
