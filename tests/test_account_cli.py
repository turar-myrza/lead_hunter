"""Offline CLI acceptance: bounded pilot, fail-closed export, separate categories."""
import csv
from datetime import datetime, timezone
import json
import socket
import sys

import httpx
import pytest

from app.account_outreach import main as outreach_main
from app.lead_hunter import main as lead_hunter_main
from app.models.lead_hunter import ResearchBatch
from app.models.lead_hunter_relationships import RelationshipReview
from app.models.lead_hunter_routing import CRMUsageAssessment, EvidenceCitation
from app.services.account_outreach import OutreachBlocked
from tests.test_account_outreach import account  # Reuse the actual offline dossier fixture.


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Local export CLI must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(httpx.Client, 'request', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)


def save_batch(path, candidates):
    batch = ResearchBatch(version='1.2', mode='expanded', candidates=candidates, search_log=['Offline CLI acceptance fixture'])
    path.write_text(batch.model_dump_json(), encoding='utf-8')
    return path


def save_briefs(path, briefs):
    path.write_text(json.dumps([brief.model_dump(mode='json') for brief in briefs]), encoding='utf-8')
    return path


def test_cli_rejects_more_than_three_before_creating_output(account, tmp_path, monkeypatch, capsys):
    _, candidate, _ = account
    research = save_batch(tmp_path/'research.json', [candidate])
    briefs = tmp_path/'briefs.json'
    briefs.write_text(json.dumps([{}, {}, {}, {}]), encoding='utf-8')
    output = tmp_path/'too-large-pilot'
    monkeypatch.setattr(sys, 'argv', ['account-outreach', 'export-pilot', str(research), str(briefs), '--output', str(output)])
    with pytest.raises(SystemExit) as error:
        outreach_main()
    assert error.value.code == 2
    assert 'at most three' in capsys.readouterr().err
    assert not output.exists()


def test_cli_exports_one_reviewed_account_as_private_draft_only(account, tmp_path, monkeypatch):
    brief, candidate, _ = account
    research = save_batch(tmp_path/'research.json', [candidate])
    briefs = save_briefs(tmp_path/'briefs.json', [brief])
    output = tmp_path/'pilot'
    monkeypatch.setattr(sys, 'argv', ['account-outreach', 'export-pilot', str(research), str(briefs), '--output', str(output)])
    outreach_main()
    assert {path.name for path in output.iterdir()} == {'account-1.md', 'account-1.json', 'pilot_summary.json'}
    summary = json.loads((output/'pilot_summary.json').read_text(encoding='utf-8'))
    assert summary == {'prepared': 1, 'sent': 0, 'calls': 0, 'crm_writes': 0,
                       'accounts': [candidate.name], 'status': 'draft_only'}
    assert json.loads((output/'account-1.json').read_text(encoding='utf-8'))['status'] == 'draft'
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())


def test_cli_validates_entire_pilot_before_writing_first_brief(account, tmp_path, monkeypatch):
    brief, candidate, _ = account
    blocked = candidate.model_copy(deep=True)
    blocked.name = 'Company under identity review'
    blocked.crm_check.state = 'possible_match'
    blocked_brief = brief.model_copy(deep=True)
    blocked_brief.candidate_name = blocked.name
    research = save_batch(tmp_path/'research.json', [candidate, blocked])
    briefs = save_briefs(tmp_path/'briefs.json', [brief, blocked_brief])
    output = tmp_path/'blocked-pilot'
    monkeypatch.setattr(sys, 'argv', ['account-outreach', 'export-pilot', str(research), str(briefs), '--output', str(output)])
    with pytest.raises(OutreachBlocked, match='Routing does not allow'):
        outreach_main()
    assert not output.exists()


def test_v12_cli_exports_four_categories_and_excludes_hold_from_active_work(account, tmp_path, monkeypatch):
    def old_score_forbidden(*args, **kwargs):
        raise AssertionError('v1.2 must route by history before any category-specific scoring; legacy score is not applicable')
    monkeypatch.setattr('app.services.lead_hunter.qualify',old_score_forbidden)
    _, base, _ = account
    candidates = []
    for index, category in enumerate('ABCD', 1):
        c = base.model_copy(deep=True)
        c.name = f'Offline company {category}'
        c.website = f'https://company{index}.kz/'
        old_phone = c.contacts[0].value
        phone = f'+7 700 123 45 0{index}'
        c.contacts[0].value = phone
        c.contacts[0].evidence.quote = phone
        c.sources[0].url = c.website
        c.sources[0].excerpt = c.sources[0].excerpt.replace(old_phone, phone) + f' {c.name}'
        c.identity.quote = c.name
        if category == 'B':
            quote = 'Ведём сделки в Bitrix24'
            c.sources[0].excerpt += ' ' + quote
            c.crm_usage_evidence = CRMUsageAssessment(status='bitrix24', basis='explicit_usage', evidence=[
                EvidenceCitation(source_id='site', quote=quote, fact='Операционное использование Битрикс24')])
        elif category == 'C':
            c.crm_check.state = 'confirmed_duplicate'
            c.relationship_review = RelationshipReview(status='complete', identity='confirmed',
                relationship_state='history_only', checked_at=datetime.now(timezone.utc), owner_name='CRM account owner')
            from app.models.lead_hunter_relationships import RelationshipEntity
            c.relationship_review.entities=[RelationshipEntity(entity='company',id=1,title='Scoped history',
                notes='PRIVATE_TRANSCRIPT_SENTINEL_'+'x'*140000)]
        elif category == 'D':
            c.crm_check.state = 'possible_match'
            c.commercial_proposal.concrete_offer = 'HOLD_SENTINEL_MUST_NOT_BECOME_AN_ACTIVE_OFFER'
        candidates.append(c)
    research = save_batch(tmp_path/'research.json', candidates)
    output = tmp_path/'classified'
    monkeypatch.setattr(sys, 'argv', ['lead-hunter', 'export', str(research), '--output', str(output), '--expanded-reviewed'])
    lead_hunter_main()
    for category in 'ABCD':
        with (output/f'category_{category}.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream, delimiter=';'))
        assert len(rows) == 1
        assert rows[0]['CATEGORY'] == category
        if category == 'D':
            assert rows[0]['ACTIVE_RECOMMENDATION'] == 'false'
            assert rows[0]['CONTACT_PERMISSIBILITY'] == 'needs_review'
            assert rows[0]['COMMERCIAL_PRIORITY'] == 'hold'
            assert rows[0]['RECOMMENDED_CHANNEL'] == 'только внутренняя проверка'
            assert 'HOLD_SENTINEL' not in rows[0]['PRODUCT_HYPOTHESIS']
        assert 'PRIVATE_TRANSCRIPT_SENTINEL' not in rows[0]['HISTORY_REVIEW']  # Manager CSV is a summary, not a full transcript dump.
    decisions = json.loads((output/'decisions.json').read_text(encoding='utf-8'))
    hold = next(decision for decision in decisions if decision['category'] == 'D')
    assert hold['active_task'] is False
    assert not (output/'companies.csv').exists()  # v1.2 never falls into legacy import export.
    assert not any(path.name.startswith('account-') for path in output.iterdir())
    report = (output/'report.md').read_text(encoding='utf-8')
    assert 'HOLD_SENTINEL' not in report
    assert all(f'## {category}.' in report for category in 'ABCD')
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())
