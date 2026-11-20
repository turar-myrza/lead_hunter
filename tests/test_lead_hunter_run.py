"""Phase 2: exercise interruption, stale evidence and the local output boundary."""
from datetime import timedelta
import json
from zipfile import ZipFile

import pytest

from app.models.lead_hunter import Candidate
from app.models.lead_hunter_crm import CRMCheck
from app.models.lead_hunter_relationships import RelationshipReview
from app.services.lead_hunter_discovery import DiscoveryFinding, DiscoveryResult
from app.services.lead_hunter_routing import route
from app.services.lead_hunter_run import RunOptions, RunStore, draft_candidate, execute, project_rows, qualification, utcnow
from app.services.lead_hunter_scoring import freshness


def public_candidate(n, *, now=None):
    now = now or utcnow()
    name, url = f'Тестовая организация {n}', f'https://business-{n}.kz/'
    fact = 'В Шымкенте открыт новый филиал'
    cite = dict(source_id='site', quote=fact, fact=fact, signal='growth')
    return Candidate.model_validate(dict(name=name, sector='Тест', city='Шымкент', website=url,
        identity=dict(source_id='site', quote=name, fact='Название'),
        geography=dict(source_id='site', quote='Шымкент', fact='Город'),
        sources=[dict(id='site', url=url, title=name, checked_at=now, published_on=now.date(),
                      access='public_page', kind='company_site', vacancy_status='not_applicable',
                      excerpt=f'{name}. {fact}. sales{n}@business-{n}.kz')],
        contacts=[dict(type='email', value=f'sales{n}@business-{n}.kz',
                       evidence=dict(source_id='site', quote=f'sales{n}@business-{n}.kz', fact='Отдел продаж'))],
        evidence=[cite], products=['Систематизация заявок'], hypothesis='Проверить процесс нового филиала.'))


def findings(candidates):
    return [DiscoveryFinding(url=c.website, title=c.name, lane=lane, query='Шымкент филиал',
                             found_at=utcnow(), provider='fixture')
            for c, lane in zip(candidates, ('A', 'B', 'A'))]


async def checked(batch):
    batch.crm_read_audit = {'crm.company.list': 1}
    for c in batch.candidates:
        c.crm_check = CRMCheck(state='no_match_accessible', checked_at=utcnow(),
                              coverage={k: True for k in ('company', 'contact', 'lead')})
    return batch


async def read_history(batch):
    for c in batch.candidates:
        c.relationship_review = RelationshipReview(status='complete', checked_at=utcnow(), relationship_state='no_history')
    return batch


def test_draft_candidate_uses_only_public_proposal_and_projects_both_texts():
    from app.integrations.lead_hunter_ai import DraftCopy, DraftRuntimeError
    from app.models.lead_hunter import ResearchBatch
    from app.models.lead_hunter_routing import CommercialProposal

    now = utcnow()
    candidate = public_candidate(1, now=now)
    candidate.crm_check = CRMCheck(state='no_match_accessible', checked_at=now,
                                   coverage={key: True for key in ('company', 'contact', 'lead')})
    candidate.commercial_proposal = CommercialProposal(
        product_key='bitrix24', offer_key='branch-routing',
        hypothesis='Уточнить, как поступившие заявки назначаются новому филиалу.',
        concrete_offer='Демонстрация маршрута тестовой заявки между двумя филиалами.',
        confirmed_fact={'source_id': 'site', 'quote': 'В Шымкенте открыт новый филиал',
                        'fact': 'Открыт новый филиал'},
        rationale='Отдельный маршрут для нового филиала',
    )
    cta = 'Удобно разобрать одну заявку за 15 минут?'
    calls = []

    class Runtime:
        def draft(self, payload):
            calls.append(payload)
            assert payload.category == 'A'
            assert payload.allowed_channels == ['email']
            assert payload.facts[0].quote == 'В Шымкенте открыт новый филиал'
            assert 'business-1.kz' not in payload.model_dump_json()
            assert 'crm_check' not in payload.model_dump()
            return DraftCopy(
                whatsapp_text=('При записи в новый филиал важно сразу назначить администратора, '
                               'который подтвердит визит. Мы из CRM Consulting. Покажем на одной '
                               'тестовой записи, как назначить филиал и проверить статус подтверждения '
                               'в карточке. ' + cta),
                subject='Заявки между филиалами',
                email_body=('Здравствуйте! Предлагаю демонстрацию: тестовая заявка попадает нужному '
                            'администратору, а в карточке видно подтверждение. ' + cta),
                call_script='Представиться и предложить показать на тестовой записи, как выбирается филиал. ' + cta,
                material_markdown='Тестовая карточка содержит филиал, администратора и статус подтверждения.',
                recommended_channel='email', fact_refs=['fact1'],
            )

    drafted = draft_candidate(candidate, Runtime(), cta=cta, as_of=now)
    assert candidate.commercial_proposal.outreach_copy is None
    batch = ResearchBatch(version='1.2', candidates=[drafted], search_log=['Тестовый источник'])
    row, = project_rows(batch, now)
    assert row['outreach_copy'].email_subject == 'Заявки между филиалами'
    assert row['outreach_copy'].whatsapp_text.endswith(cta)
    assert row['outreach_sources'] == [candidate.website]
    assert row['outreach_channel_verified']
    archive_row, = project_rows(batch, now, archive=True)
    assert archive_row['outreach_copy'] is None
    candidate.crm_check = CRMCheck()
    with pytest.raises(DraftRuntimeError, match='does not allow'):
        draft_candidate(candidate, Runtime(), cta=cta, as_of=now)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_resume_reuses_each_completed_unit_and_preserves_source_dates(tmp_path):
    items = [public_candidate(1), public_candidate(2)]
    search_count = 0
    verify_calls, identity_calls, history_calls = [], [], []
    store = RunStore.create(tmp_path / 'run', RunOptions())

    async def collect(city, limit, **kw):
        nonlocal search_count
        assert city == 'Шымкент' and kw['lanes'] == ('A', 'B')
        search_count += 1
        return DiscoveryResult(findings=findings(items), providers_succeeded=['fixture'])

    async def verify(city, items_found, limit, **kw):
        i = 0 if items_found[0].url == items[0].website else 1
        verify_calls.append(i)
        if verify_calls == [0, 1]:
            raise RuntimeError('interrupt the second fetch')
        return DiscoveryResult(candidates=[items[i].model_copy(deep=True)])

    async def check(batch):
        identity_calls.append(len(batch.candidates))
        return await checked(batch)

    async def history(batch):
        history_calls.append(batch.candidates[0].name)
        if len(history_calls) == 2:
            raise RuntimeError('interrupt the second history')
        return await read_history(batch)

    kwargs = dict(check=check, history=history, collect=collect, verify=verify, fetcher=object())
    with pytest.raises(RuntimeError, match='second fetch'):
        await execute(store, **kwargs)
    assert (store.path / 'verified-0000.json').exists()
    with pytest.raises(RuntimeError, match='second history'):
        await execute(store, **kwargs)
    assert (store.path / 'identity.json').exists()
    result = await execute(store, **kwargs)
    again = await execute(store, **kwargs)
    assert result['status'] == again['status'] == 'awaiting_llm'
    assert search_count == 1 and verify_calls == [0, 1, 1] and identity_calls == [2]
    assert history_calls == [items[0].name, items[1].name, items[1].name]
    assert result['counts']['qualify'] == 2 and result['counts']['recommended'] == 0
    assert result['llm_calls'] == 0
    assert result['crm_read_attempts_in_snapshot'] == 1
    evidence = store.load('evidence.json')
    for actual, original in zip(evidence['candidates'], items):
        assert actual['sources'][0]['checked_at'] == original.sources[0].model_dump(mode='json')['checked_at']
    with ZipFile(store.path / 'report.xlsx') as workbook:
        assert workbook.testzip() is None
    events = [json.loads(line) for line in (store.path / 'events.jsonl').read_text().splitlines()]
    assert sum(e['stage'] == 'crm_identity' and e['status'] == 'reused' for e in events) == 2
    assert all(e['status'] == 'deferred' and e['requests'] == 0 for e in events if e['stage'] == 'llm')


@pytest.mark.asyncio
async def test_outage_is_retryable_and_not_substituted_by_archive(tmp_path):
    store = RunStore.create(tmp_path / 'run', RunOptions())

    async def no_calls(*args, **kwargs):
        pytest.fail('No verification or CRM access without discovered URLs')

    async def outage(*args, **kwargs):
        return DiscoveryResult(providers_failed=['fixture'])

    async def empty(*args, **kwargs):
        return DiscoveryResult(providers_succeeded=['fixture'])

    kwargs = dict(check=no_calls, history=no_calls, verify=no_calls, fetcher=object())
    assert (await execute(store, collect=outage, **kwargs))['status'] == 'discovery_unavailable'
    assert store.load('discovery.json') is None
    assert (await execute(store, collect=empty, **kwargs))['status'] == 'no_verified_candidates'
    assert store.load('evidence.json')['candidates'] == []


@pytest.mark.asyncio
async def test_temporary_failure_is_retried_without_rescanning_successful_pages(tmp_path):
    items = [public_candidate(1), public_candidate(2)]
    counts = {item.website: 0 for item in items}
    store = RunStore.create(tmp_path / 'run', RunOptions(limit=2))

    async def collect(*args, **kw):
        return DiscoveryResult(findings=findings(items), providers_succeeded=['fixture'])

    async def verify(city, refs, limit, **kw):
        url = refs[0].url
        counts[url] += 1
        if url == items[0].website and counts[url] == 1:
            return DiscoveryResult(verification_incomplete=True, skipped_sources=['Temporary timeout'])
        return DiscoveryResult(candidates=[next(item for item in items if item.website == url).model_copy(deep=True)])

    async def check(batch):
        assert len(batch.candidates) == 2
        return await checked(batch)

    kwargs = dict(check=check, history=read_history, collect=collect, verify=verify, fetcher=object())
    assert (await execute(store, **kwargs))['status'] == 'partial_public'
    assert store.load('identity.json') is None and store.load('public_batch.json') is None
    assert store.load('verified-0001.json') is not None
    assert (await execute(store, **kwargs))['status'] == 'awaiting_llm'
    assert counts == {items[0].website: 2, items[1].website: 1}


@pytest.mark.asyncio
async def test_stale_crm_replay_does_not_claim_current_status_or_repeat_scan(tmp_path):
    c = public_candidate(1)
    store = RunStore.create(tmp_path / 'run', RunOptions(limit=1))

    async def collect(*args, **kw):
        return DiscoveryResult(findings=findings([c]), providers_succeeded=['fixture'])

    async def verify(*args, **kw):
        return DiscoveryResult(candidates=[c])

    async def old_check(batch):
        batch = await checked(batch)
        batch.candidates[0].crm_check.checked_at = utcnow() - timedelta(days=2)
        return batch

    kwargs = dict(history=read_history, collect=collect, verify=verify, fetcher=object())
    assert (await execute(store, check=old_check, **kwargs))['status'] == 'stale_crm_snapshot'

    async def forbidden(*args):
        pytest.fail('Resuming a stale archived snapshot must not implicitly read CRM')

    assert (await execute(store, check=forbidden, **kwargs))['status'] == 'stale_crm_snapshot'


def test_fresh_signal_can_qualify_despite_low_score_but_not_conflicted_history():
    now = utcnow()
    c = public_candidate(1, now=now)
    c.crm_check = CRMCheck(state='no_match_accessible', checked_at=now,
                          coverage={k: True for k in ('company', 'contact', 'lead')})
    decision = route(c, now)
    assert decision.category == 'A' and decision.need_score == 20 and not decision.active_task
    assert qualification(c, decision, now)
    c.crm_check.state = 'possible_match'
    c.relationship_review = RelationshipReview(status='partial', identity='possible')
    assert not qualification(c, route(c, now), now)


def test_archive_does_not_use_later_attempt_or_expired_source():
    source = public_candidate(1, now=utcnow() - timedelta(days=2)).sources[0]
    source.last_attempt_at = source.checked_at + timedelta(hours=2)
    assert not freshness(source, source.checked_at + timedelta(hours=1))[0]
    source.valid_until = source.checked_at.date()
    assert not freshness(source, source.checked_at + timedelta(days=1))[0]


def test_configured_sources_cannot_enable_private_or_prohibited_hosts():
    allowed = {'kind': 'employer', 'name': 'official', 'url': 'https://business.kz/career', 'employer': 'Организация'}
    options = RunOptions(sources=[allowed])
    assert options.sources[0].adapter().pages == ('https://business.kz/career',)
    for url in ('https://localhost.internal/career', 'https://hh.kz/employer/10',
                'https://business.kz/career?api_key=not-a-key'):
        with pytest.raises(ValueError):
            RunOptions(sources=[{**allowed, 'url': url}])
