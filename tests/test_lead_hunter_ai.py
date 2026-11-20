"""AI provider boundaries use mock transport; these tests never call a service."""
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import httpx
from pydantic import ValidationError
import pytest

from app.integrations.lead_hunter_ai import (
    BudgetPolicy, DraftBudgetExceeded, DraftRuntimeDisabled, DraftRuntimeError,
    LocalLlamaDraft, OpenAIResponsesDraft, PublicAnalysisInput, PublicDraftInput, PublicFact, RunBudget,
)


@pytest.fixture
def evidence():
    return PublicAnalysisInput(company='Учебная мастерская', city='Шымкент', sector='Производство',
        facts=[PublicFact(fact_id='f1', source_id='public_catalog', quote='Принимаем индивидуальные заказы.')])


def budget(**changes):
    return RunBudget(BudgetPolicy(**dict(dict(max_run_usd=Decimal('.020'),
        input_usd_per_million=Decimal('1'), output_usd_per_million=Decimal('2'),
        max_input_tokens=6000, max_output_tokens=500, max_requests=3), **changes)))


def analysis_content(**changes):
    return dict(dict(hypothesis='Возможно, стоит проверить согласование сроков индивидуального заказа.',
        opportunity='Изучить маршрут подтверждения даты выдачи.', why_now='Не подтверждено.', fact_refs=['f1']), **changes)


def response(text=None, *, counts=None, status='completed', content_type='output_text'):
    return httpx.Response(200, json=dict(status=status, output=[dict(type='message', role='assistant',
        content=[dict(type=content_type, text=json.dumps(analysis_content(), ensure_ascii=False) if text is None else text)])],
        usage={'input_tokens': 190, 'output_tokens': 50} if counts is None else counts))


def forbid(*_args, **_kwargs):
    pytest.fail('Credential read or network activity is forbidden for this case')


def adapter(run_budget, handler, **changes):
    return OpenAIResponsesDraft('gpt-test', run_budget, **dict(dict(enabled=True,
        transport=httpx.MockTransport(handler), credential_reader=lambda: 'fake-test-credential'), **changes))


@pytest.mark.parametrize('enabled', [False, 'true', 1])
def test_remote_opt_in_is_explicit_and_key_is_never_read_when_disabled(evidence, enabled):
    run = budget()
    provider = adapter(run, forbid, enabled=enabled, credential_reader=forbid)
    with pytest.raises(DraftRuntimeDisabled):
        provider.analyze(evidence)
    assert not run.journal


def test_only_typed_public_projection_reaches_transport(evidence):
    provider = adapter(budget(), forbid, credential_reader=forbid)
    with pytest.raises(DraftRuntimeError, match='public-only typed'):
        provider.analyze(dict(evidence.model_dump(), crm_check={'owner': 'Private'}))
    with pytest.raises(ValidationError):
        PublicAnalysisInput.model_validate(dict(evidence.model_dump(), relationship_review={'owner_name': 'Private'}))
    with pytest.raises(ValidationError):
        PublicFact.model_validate(dict(evidence.facts[0].model_dump(), contact={'phone': '123'}))


@pytest.mark.parametrize('private', ['info@example.org', '+7 (700) 123-45-67',
    'https://portal.kz/rest/1/secret/', 'contact #123', 'owner_name: person', '<|im_end|>', 'sk-proj-secretsecretsecret'])
def test_private_text_and_direct_contacts_do_not_reach_key_or_network(evidence, private):
    with pytest.raises(ValidationError):
        PublicFact(fact_id='f2', source_id='fixture', quote=f'Опубликовано {private}')
    # Even bypassing Pydantic construction and mutating a nested list cannot send it.
    evidence.facts.append(PublicFact.model_construct(fact_id='f2', source_id='fixture', quote=f'Опубликовано {private}'))
    provider = adapter(budget(), forbid, credential_reader=forbid)
    with pytest.raises(DraftRuntimeError, match='Public-only payload failed'):
        provider.analyze(evidence)


def test_responses_uses_strict_schema_and_journals_only_cost_metadata(evidence, tmp_path):
    captured = []
    def handler(req):
        captured.append(json.loads(req.content))
        assert str(req.url) == 'https://api.openai.com/v1/responses'
        assert req.headers['Authorization'] == 'Bearer fake-test-credential'
        return response()
    run = budget()
    answer = adapter(run, handler).analyze(evidence)
    assert answer.fact_refs == ['f1']
    req = captured[0]
    assert req['tools'] == [] and req['tool_choice'] == 'none'
    assert req['store'] is False and req['stream'] is False
    assert req['max_output_tokens'] == 500
    assert json.loads(req['input']) == evidence.model_dump()
    fmt = req['text']['format']
    assert fmt['type'] == 'json_schema' and fmt['strict'] is True
    assert fmt['schema']['additionalProperties'] is False
    assert set(fmt['schema']['required']) == set(fmt['schema']['properties'])
    assert fmt['schema']['properties']['fact_refs']['items']['enum'] == ['f1']
    row = run.journal[0]
    assert row.outcome == 'succeeded' and row.input_tokens == 190 and row.output_tokens == 50
    assert row.reserved_usd == Decimal('.007') and row.estimated_cost_usd == Decimal('.00029')
    assert row.retained_reserve_usd == 0
    path = tmp_path / 'usage.json'
    run.write_journal(path)
    assert path.stat().st_mode & 0o777 == 0o600
    stored = path.read_text()
    for phrase in ('fake-test-credential', evidence.company, evidence.facts[0].quote, answer.hypothesis):
        assert phrase not in stored


@pytest.mark.parametrize('limits', [dict(max_run_usd=Decimal('.0069')), dict(max_input_tokens=256)])
def test_budget_preflight_stops_before_reading_key(evidence, limits):
    run = budget(**limits)
    with pytest.raises(DraftBudgetExceeded):
        adapter(run, forbid, credential_reader=forbid).analyze(evidence)
    assert not run.journal


def test_budget_is_cumulative_per_run_and_charges_known_invalid_usage(evidence):
    run = budget(max_requests=2)
    bad = json.dumps(analysis_content(fact_refs=['unverified']))
    p = adapter(run, lambda _: response(bad))
    for _ in range(2):
        with pytest.raises(DraftRuntimeError, match='schema or evidence'):
            p.analyze(evidence)
    assert [row.outcome for row in run.journal] == ['invalid_response', 'invalid_response']
    assert sum(row.estimated_cost_usd for row in run.journal) == Decimal('.00058')
    with pytest.raises(DraftBudgetExceeded):
        adapter(run, forbid, credential_reader=forbid).analyze(evidence)


def test_unknown_usage_retains_reserve_and_never_retries(evidence):
    seen = []
    def handler(req):
        seen.append(req)
        return response(counts={})
    run = budget(max_run_usd=Decimal('.010'))
    with pytest.raises(DraftRuntimeError, match='full reserve retained'):
        adapter(run, handler).analyze(evidence)
    assert len(seen) == 1
    assert run.journal[0].estimated_cost_usd is None
    assert run.journal[0].retained_reserve_usd == Decimal('.007')
    with pytest.raises(DraftBudgetExceeded):
        adapter(run, forbid, credential_reader=forbid).analyze(evidence)


@pytest.mark.parametrize('status', [302, 401, 429, 503])
def test_transport_never_follows_redirect_or_retries_http_failures(evidence, status):
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(status, headers={'location': 'https://untrusted.invalid/'},
                              text='Secret echoed by provider: fake-test-credential')
    run = budget()
    with pytest.raises(DraftRuntimeError, match=f'HTTP {status}') as e:
        adapter(run, handler).analyze(evidence)
    assert len(seen) == 1 and run.journal[0].retained_reserve_usd == Decimal('.007')
    assert 'fake-test-credential' not in str(e.value)


def test_transport_exception_does_not_log_raw_provider_error(evidence):
    def handler(_req):
        raise httpx.ReadTimeout('fake-test-credential from a private request')
    run = budget()
    with pytest.raises(DraftRuntimeError, match='usage could not be established') as e:
        adapter(run, handler).analyze(evidence)
    assert 'fake-test-credential' not in str(e.value)
    assert run.journal[0].outcome == 'transport_error'


def test_reported_usage_over_cap_stops_later_requests(evidence):
    run = budget()
    with pytest.raises(DraftBudgetExceeded, match='run stopped'):
        adapter(run, lambda _: response(counts={'input_tokens': 6001, 'output_tokens': 50})).analyze(evidence)
    assert run.journal[0].outcome == 'usage_over_limit'
    assert run.journal[0].estimated_cost_usd == Decimal('.006101')
    with pytest.raises(DraftBudgetExceeded):
        adapter(run, forbid, credential_reader=forbid).analyze(evidence)


@pytest.mark.parametrize('make_response', [
    lambda: response(content_type='refusal'), lambda: response(status='incomplete'),
    lambda: response(json.dumps(analysis_content(owner_name='invented'))),
    lambda: response('{"hypothesis":"duplicate","hypothesis":"different"}'),
])
def test_refusal_incomplete_and_extra_fields_never_become_drafts(evidence, make_response):
    run = budget()
    with pytest.raises(DraftRuntimeError):
        adapter(run, lambda _: make_response()).analyze(evidence)
    assert run.journal[0].outcome == 'invalid_response'
    assert run.journal[0].estimated_cost_usd == Decimal('.00029')


def test_missing_credential_releases_reserve_without_http(evidence):
    run = budget()
    with pytest.raises(DraftRuntimeError, match='no request sent'):
        adapter(run, forbid, credential_reader=lambda: None).analyze(evidence)
    assert run.journal[0].outcome == 'not_sent' and run.journal[0].estimated_cost_usd == 0
    assert run.journal[0].retained_reserve_usd == 0


def test_draft_operation_uses_one_cta_and_never_provides_eligibility(evidence):
    draft = PublicDraftInput(**evidence.model_dump(),
        category='A', allowed_channels=['phone'],
        hypothesis='Возможно, полезно фиксировать согласованный срок в карточке заказа.',
        offer='Предлагаем разобрать один заказ и проверить способ фиксации срока.',
        cta='Удобно обсудить один заказ на встрече?')
    good = dict(subject='Согласование даты по индивидуальному заказу',
        whatsapp_text=('Если дату выдачи изделия нужно согласовать с несколькими сотрудниками, '
            'удобно видеть последнюю договорённость сразу. Покажем на одном тестовом заказе, '
            'как сохранить дату выдачи и ответственного в карточке без лишних полей. ' + draft.cta),
        email_body=('По индивидуальному заказу важно видеть, какая дата уже согласована. '
            'Можем показать карточку, в которой сотрудник сразу находит срок, ответственного '
            'и следующую задачу по заказу. ' + draft.cta),
        call_script=f'Добрый день! Можем показать, как фиксировать дату выдачи по одному заказу. {draft.cta}',
        recommended_channel='phone',
        material_markdown='Схема для обсуждения: запрос → согласование срока → подтверждение заказа.', fact_refs=['f1'])
    run = budget()
    def handler(req):
        fmt = json.loads(req.content)['text']['format']
        assert fmt['name'] == 'lead_hunter_draft'
        assert fmt['schema']['properties']['recommended_channel']['enum'] == ['phone']
        return response(json.dumps(good, ensure_ascii=False))
    out = adapter(run, handler).draft(draft)
    assert out.subject == good['subject'] and run.journal[0].operation == 'draft'
    assert out.manager_copy(draft.cta).recommended_channel == 'phone'
    original = good['email_body']
    good['email_body'] = 'На вашем сайте написано о производстве. ' + original
    with pytest.raises(DraftRuntimeError, match='schema or evidence'):
        adapter(run, handler).draft(draft)
    good['email_body'] = original + ' ' + draft.cta
    with pytest.raises(DraftRuntimeError, match='schema or evidence'):
        adapter(run, handler).draft(draft)
    with pytest.raises(ValidationError):
        PublicDraftInput.model_validate(dict(draft.model_dump(), category='C'))


def test_llama_uses_only_verified_local_files_no_cloud_fallback(evidence, tmp_path, monkeypatch):
    binary, model = tmp_path / 'llama-completion', tmp_path / 'local.gguf'
    binary.write_bytes(b'local binary fixture')
    model.write_bytes(b'local model fixture')
    def fake_run(command, **kwargs):
        assert command[0] == str(binary) and '--offline' in command
        assert kwargs['shell'] is False and kwargs['timeout'] <= 120
        assert 'OPENAI_API_KEY' not in kwargs['env']
        file = Path(command[command.index('--file') + 1])
        assert file.parent.stat().st_mode & 0o777 == 0o700
        assert evidence.facts[0].quote in file.read_text()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(analysis_content()).encode() + b' [end of text]\n')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    local = LocalLlamaDraft(binary, model, binary_sha256=sha256(binary.read_bytes()).hexdigest(),
                            model_sha256=sha256(model.read_bytes()).hexdigest())
    assert local.analyze(evidence).fact_refs == ['f1']
    model.write_bytes(b'tampered')
    monkeypatch.setattr(subprocess, 'run', forbid)
    with pytest.raises(DraftRuntimeError, match='unverified'):
        local.analyze(evidence)
