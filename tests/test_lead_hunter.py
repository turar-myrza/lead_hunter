"""Meaningful offline checks for evidence, score, duplicate and CSV boundaries."""

import csv
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
from itertools import combinations
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.models.lead_hunter import Candidate, Criterion, ResearchBatch, WEIGHTS
from app.services.lead_hunter import csv_export, markdown_report, qualify, ranked


@pytest.fixture
def record():
    return dict(name='Example Business', sector='Опт', city='Шымкент', website='https://business.kz/',
                identity=dict(source_id='s', quote='Example Business', fact='Название'),
                geography=dict(source_id='s', quote='Шымкент', fact='Город'),
                sources=[dict(id='s', url='https://business.kz/', title='Business',
                              checked_at=datetime.now(timezone.utc).isoformat(), kind='company_site',
                              access='public_page', excerpt='Example Business Шымкент CRM WhatsApp поток команда рост +77001234567')],
                evidence=[dict(source_id='s', quote='CRM', fact='Использует CRM', criterion='crm_or_automation')],
                products=['Аудит CRM'], hypothesis='Как контролируете повторные обращения?', contacts=[])


def test_all_score_combinations_and_once_per_criterion(record):
    for count in range(1, 6):
        for criteria in combinations(list(Criterion), count):
            record['evidence'] = [dict(source_id='s', quote='CRM', fact='Offline fixture', criterion=c) for c in criteria]
            record['evidence'] *= 2
            candidate = Candidate.model_validate(record)
            score = sum(WEIGHTS[c] for c in criteria)
            assert qualify(candidate)['score'] == score
            assert qualify(candidate)['priority'] == ('A' if score >= 70 else 'B' if score >= 40 else 'C')


@pytest.mark.parametrize('change', ['missing_quote', 'missing_source', 'no_city', 'search_only', 'no_signal', 'invented_contact', 'duplicate_source', 'future'])
def test_rejects_untraceable_evidence(record, change):
    if change == 'missing_quote':
        record['evidence'][0]['quote'] = 'Not present'
    elif change == 'missing_source':
        record['evidence'][0]['source_id'] = 'unknown'
    elif change == 'no_city':
        record['geography']['quote'] = 'CRM'
    elif change == 'search_only':
        record['sources'][0].update(access='search_excerpt', limitation='Page unavailable')
    elif change == 'no_signal':
        record['evidence'][0]['criterion'] = None
    elif change == 'invented_contact':
        record['contacts'] = [dict(type='phone', value='+77009999999', evidence=dict(source_id='s', quote='+77001234567', fact='Contact'))]
    elif change == 'duplicate_source':
        record['sources'] *= 2
    else:
        record['sources'][0]['checked_at'] = '2099-01-01T00:00:00Z'
    with pytest.raises(ValidationError):
        Candidate.model_validate(record)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://127.0.0.1/', 'http://localhost/', 'https://user:secret@business.kz/', 'https://portal.kz/rest/1/token/', 'http://business.local/', 'https://business.kz:8080/'])
def test_rejects_private_or_credential_urls(record, url):
    record['website'] = url
    with pytest.raises(ValidationError):
        Candidate.model_validate(record)


@pytest.mark.parametrize('duplicate', ['name', 'url', 'contact'])
def test_duplicate_rejection(record, duplicate):
    other = deepcopy(record)
    other.update(name='Other', website='https://other.kz/')
    if duplicate == 'name':
        other['name'] = 'EXAMPLE BUSINESS!'
    elif duplicate == 'url':
        other['website'] = 'https://www.business.kz'
    else:
        for item in (record, other):
            item['contacts'] = [dict(type='phone', value='+77001234567', evidence=dict(source_id='s', quote='+77001234567', fact='Contact'))]
    batch = ResearchBatch(candidates=[record, other], search_log=['query'])
    with pytest.raises(ValueError, match='duplicate'):
        ranked(batch)


def test_csv_round_trip_formula_protection_and_report(record):
    record['name'] = '=FORMULA; "test"\nновая строка'
    record['contacts'] = [dict(type='phone', value='+77001234567', evidence=dict(source_id='s', quote='+77001234567', fact='Public contact'))]
    batch = ResearchBatch(candidates=[record], search_log=['query'])
    rows = list(csv.DictReader(StringIO(csv_export(batch)), delimiter=';'))
    assert rows[0]['COMPANY_TITLE'] == "'" + record['name']
    assert rows[0]['PHONE_WORK'] == '77001234567'
    assert rows[0]['SCORE'] == '15'
    assert 'https://business.kz/' in rows[0]['COMMENTS']
    assert 'ГИПОТЕЗА' in rows[0]['COMMENTS']
    assert 'A: 0; B: 0; C: 1' in markdown_report(batch)


def test_pilot_limit(record):
    with pytest.raises(ValidationError, match='five'):
        ResearchBatch(candidates=[record] * 6, search_log=['query'])


def test_cli_export_and_expansion_gate_without_network(record, tmp_path):
    path = tmp_path / 'research.json'
    path.write_text(json.dumps(dict(mode='expanded', candidates=[record], search_log=['query'])), encoding='utf-8')
    command = [sys.executable, '-m', 'app.lead_hunter', 'export', str(path), '--output', str(tmp_path / 'result')]
    denied = subprocess.run(command, capture_output=True, text=True)
    assert denied.returncode != 0
    assert not (tmp_path / 'result').exists()
    allowed = subprocess.run(command + ['--expanded-reviewed'], capture_output=True, text=True)
    assert allowed.returncode == 0, allowed.stderr
    assert (tmp_path / 'result/companies.csv').read_bytes().startswith(b'\xef\xbb\xbf')
    assert len(list(csv.DictReader((tmp_path / 'result/companies.csv').open(encoding='utf-8-sig'), delimiter=';'))) == 1
