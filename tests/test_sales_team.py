"""Exercise the sales worker using local state and synthetic source evidence."""
from datetime import date
import json

import pytest
from fastapi.testclient import TestClient

from app.api.sales_team_dashboard import create_dashboard
from app.models.sales_team import Outcome, Prospect
from app.sales_team import main
from app.services import sales_team
from app.services.sales_store import OBSERVED_EVENTS, SalesStore, now


@pytest.fixture
def prospect():
    today = date.today()
    return Prospect(
        name='Учебная компания «Таң»',
        city='Шымкент',
        industry='Поставка промышленного оборудования',
        site='https://company.example.com/',
        emails=['sales@company.example.com'],
        sources=[
            {'url':'https://company.example.com/contacts', 'observed_at':today,
             'fact':'Деловой контакт отдела продаж sales@company.example.com'},
            {'url':'https://catalog.example.org/tan', 'observed_at':today,
             'fact':'Инженер согласует конфигурацию насосов по заявке с чертежом.'},
        ],
        product='A',
        fact='Опубликована форма подачи технического чертежа.',
        hypothesis='Передача согласованного чертежа поможет подготовить техническое предложение.',
        offer='Показать связь выбранного насоса и согласованного чертежа в одной карточке.',
        next_step='Согласовать с руководителем короткий показ на учебных данных.',
        process='Согласование технической спецификации',
        visual_steps=['Подбор насоса', 'Согласование спецификации', 'Дата отправки предложения'],
        outcome='Менеджер видит согласованный чертёж и следующий срок.',
        ethical='По учебному описанию запрещённых направлений не обнаружено.',
    )


def _enqueue(store, prospect):
    run_id = store.enqueue('public_batch', {'prospects':[prospect.model_dump(mode='json')]})
    run = store.claim()
    assert run['id'] == run_id
    return run


def _new_crm():
    return {'state':'no_match_accessible', 'checked_at':now().isoformat()}


async def _verified_crm(candidates, **_):
    return {p.name:_new_crm() for p in candidates}, {'mode':'synthetic_fixture'}


def test_simulation_cli_runs_all_eight_roles(tmp_path):
    db = tmp_path / 'state.db'
    result = tmp_path / 'outputs'
    assert main(['--db',str(db),'--output',str(result),'simulate']) == 0

    stats = SalesStore(db).stats()
    assert stats['runs'][0]['status'] == 'completed'
    assert all(stats['events'][role] == 1 for role in sales_team.ROLES)
    assert stats['simulated_dialogues'] == 1
    assert not stats['metrics']
    assert not OBSERVED_EVENTS.intersection(stats['events'])
    folder = result / str(stats['runs'][0]['id'])
    summary = json.loads((folder / 'summary.json').read_text())
    assert summary['qualified'] == 0 and summary['prepared'] == 1
    assert summary['outbound_enabled'] is False
    voice = json.loads((folder / 'simulation.json').read_text())
    assert voice[0]['transport'] == 'simulated' and voice[0]['language'] == 'kk'
    assert voice[0]['handoff'] is True
    assert list(folder.glob('demo-*.html'))
    assert (folder / 'leads.xlsx').read_bytes().startswith(b'PK')


@pytest.mark.asyncio
async def test_restart_uses_saved_work_and_checks_prior_run(tmp_path, prospect, monkeypatch):
    store = SalesStore(tmp_path / 'state.db')
    run = _enqueue(store, prospect)
    output = tmp_path / 'outputs'
    calls = 0

    async def crm(candidates, **kwargs):
        nonlocal calls
        calls += 1
        return await _verified_crm(candidates, **kwargs)

    def stop_after_qualification(_):
        store.set_stopped(True)
        raise sales_team.Stopped()

    with monkeypatch.context() as patch:
        patch.setattr(sales_team, 'recognition', stop_after_qualification)
        with pytest.raises(sales_team.Stopped):
            await sales_team.process_run(store, run, output_root=output, crm_reader=crm)
    assert store.claim() is None
    q = store.step(run['id'], prospect.name, 'qualification_analyst')
    assert q['status'] == 'qualified'

    store.set_stopped(False)
    retry = store.claim()
    assert retry['id'] == run['id']
    recovered = await sales_team.process_run(store, retry, output_root=output, crm_reader=crm)
    assert recovered['qualified'] == 1
    assert calls == 1
    assert store.stats()['events']['qualification_analyst'] == 1
    assert store.step(run['id'], prospect.name, 'ai_voice_sdr')['status'] == 'blocked_pending_approval'
    assert not OBSERVED_EVENTS.intersection(store.stats()['events'])

    later = _enqueue(store, prospect)
    repeated = await sales_team.process_run(store, later, output_root=output, crm_reader=crm)
    assert repeated['qualified'] == 0
    assert repeated['review'][0]['status'] == 'hold'
    assert store.step(later['id'], prospect.name, 'direct_response_marketer') is None


@pytest.mark.asyncio
async def test_crm_without_complete_coverage_blocks_output(tmp_path, prospect):
    store = SalesStore(tmp_path / 'state.db')
    run = _enqueue(store, prospect)

    async def incomplete(_, **kwargs):
        return {prospect.name:{'state':'incomplete'}}, {'mode':'synthetic_fixture'}

    result = await sales_team.process_run(store, run, output_root=tmp_path / 'reports', crm_reader=incomplete)
    assert result['qualified'] == 0
    assert result['review'][0]['status'] == 'hold'
    assert store.stats()['runs'][0]['status'] == 'partial_crm'
    assert store.step(run['id'], prospect.name, 'ai_voice_sdr') is None


def test_qualification_requires_product_evidence_and_ethical_scope(prospect):
    assert sales_team.qualify(prospect, _new_crm())['contact_permission'] == 'draft_only'
    without_product_proof = prospect.model_copy(update={'product':'B'})
    assert sales_team.qualify(without_product_proof, _new_crm())['contact_permission'] == 'blocked'
    prohibited = prospect.model_copy(update={'industry':'Ломбард'})
    assert sales_team.qualify(prohibited, _new_crm())['status'] == 'excluded'
    account = sales_team.qualify(prospect, {'state':'confirmed_duplicate', 'relationship':'current_customer'})
    assert account['status'] == 'account' and account['contact_permission'] == 'blocked'


def test_recorded_events_exclude_simulations_and_deduplicate(tmp_path, prospect):
    store = SalesStore(tmp_path / 'state.db')
    normal = store.enqueue('public_batch')
    simulation = store.enqueue('simulation')
    for run_id in (normal, simulation):
        store.save(run_id, prospect.name, 'commercial_researcher', {})
    event = Outcome(event_id='delivery-001', run_id=normal, company=prospect.name,
                    kind='email_delivered', at=now(), variant='email_text', language='ru', segment='A')
    assert store.record(event)
    assert not store.record(event)
    assert store.stats()['events']['email_delivered'] == 1
    assert store.stats()['metrics'][0]['n'] == 1
    with pytest.raises(ValueError):
        store.record(event.model_copy(update={'run_id':simulation, 'event_id':'delivery-002'}))
    with pytest.raises(ValueError):
        store.record(event.model_copy(update={'kind':'meeting_booked'}))


def test_dashboard_serves_only_known_local_artifacts(tmp_path):
    store = SalesStore(tmp_path / 'state.db')
    run = store.enqueue('public_batch')
    output = tmp_path / 'outputs'
    folder = output / str(run)
    folder.mkdir(parents=True)
    (folder / 'index.html').write_text('<h1>Демонстрация</h1>', encoding='utf-8')
    outside = tmp_path / 'private.txt'
    outside.write_text('private', encoding='utf-8')
    (folder / 'summary.json').symlink_to(outside)
    (folder / 'notes.json').write_text('private', encoding='utf-8')
    client = TestClient(create_dashboard(store, output), base_url='http://127.0.0.1')
    path = f'/runs/{run}/files/'
    preview = client.get(path + 'index.html')
    assert preview.status_code == 200
    assert 'sandbox' in preview.headers['content-security-policy']
    for invalid in ('summary.json', 'notes.json', '%2e%2e/private.txt'):
        assert client.get(path + invalid).status_code == 404
    assert client.post('/').status_code == 405
    assert client.get('/', headers={'Host':'external.example'}).status_code == 400


def test_soft_drinks_do_not_match_alcohol_and_restricted_number_stays_review(prospect):
    soft_drinks = prospect.model_copy(update={'industry':'Производство безалкогольных напитков'})
    assert sales_team.qualify(soft_drinks,_new_crm())['status'] == 'qualified'
    restricted = soft_drinks.model_copy(update={
        'watch':'Номер опубликован для дистрибьюторов, его не следует использовать как универсальный канал.'
    })
    decision = sales_team.qualify(restricted,_new_crm())
    assert decision['status']=='research'
    assert 'для другой цели' in decision['reason']
