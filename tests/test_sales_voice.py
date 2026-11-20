from datetime import datetime, timedelta, timezone

import pytest

from app.services.sales_voice import (
    CallPolicy,
    CallRequest,
    VoiceDossier,
    evaluate_call_admission,
    simulate_voice_dialogue,
)


@pytest.fixture
def dossier():
    return VoiceDossier(
        company='Учебный поставщик',
        offer='Для вашего отдела продаж подготовили пример передачи заявки инженеру.',
        language_variants={'kk': 'Өтінімді инженерге беру мысалын дайындадық.'},
    )


def test_simulates_language_switch_and_manager_handoff(dossier):
    result = simulate_voice_dialogue(dossier, ['Қазақша айтыңыз', 'Бағасы қанша?', 'Иә'])

    assert result.transport == 'simulated'
    assert result.language == 'kk'
    assert 'голосовой AI-помощник CRM Consulting' in result.transcript[0].text
    assert 'Өтінімді инженерге' in result.transcript[2].text
    assert 'Бағасы жұмыс көлемі' in result.transcript[4].text
    assert result.status == 'handoff_requested'
    assert result.handoff


def test_refusal_blocks_handoff_even_with_previous_interest(dossier):
    result = simulate_voice_dialogue(dossier, ['Интересно', 'Меня не интересует, больше не звоните', 'Да'])

    assert result.status == 'opted_out'
    assert result.opt_out and not result.handoff
    assert result.transcript[-2].text == 'Меня не интересует, больше не звоните'


def test_handles_scenario_question_instead_of_repeating_question(dossier):
    result = simulate_voice_dialogue(dossier, ['Как это работает?'])

    assert result.transcript[-1].text.startswith(dossier.offer)
    assert result.status == 'conversation_open'


@pytest.fixture
def approved_sip():
    now = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)
    tomorrow = now + timedelta(days=1)
    return (
        now,
        CallPolicy(campaign_id='trial', enabled=True, approved_by='manager',
                   legal_review='L-1', operator_review='O-1', expires_at=tomorrow,
                   allowed_channels={'sip', 'whatsapp'}, allowed_segments={'industry'},
                   daily_limit=2, approved_test_numbers={'+7 (700) 000-00-00'}),
        CallRequest(campaign_id='trial', phone='+77000000000', channel='sip',
                    segment='industry', consent_kind='opt_in', consent_channel='sip',
                    consent_reference='TEST-CONSENT', consent_expires_at=tomorrow,
                    test=True),
    )


def test_approved_policy_and_consent_produce_admission(approved_sip):
    now, policy, request = approved_sip
    result = evaluate_call_admission(policy, request, now=now)
    assert result.allowed and not result.blockers


@pytest.mark.parametrize(('change', 'blocker'), [
    ({'consent_kind': 'public_listing'}, 'missing_recipient_consent'),
    ({'channel': 'whatsapp'}, 'missing_recipient_consent'),
    ({'do_not_contact': True}, 'opted_out'),
    ({'phone': '+77000000001'}, 'unapproved_test_number'),
    ({'recording_requested': True}, 'recording_not_reviewed'),
    ({'calls_today': 2}, 'daily_limit_reached'),
])
def test_specific_reasons_block_admission(approved_sip, change, blocker):
    now, policy, request = approved_sip
    result = evaluate_call_admission(policy, request.model_copy(update=change), now=now)
    assert not result.allowed and blocker in result.blockers


def test_stopped_or_expired_campaign_blocks_admission(approved_sip):
    now, policy, request = approved_sip
    stopped = evaluate_call_admission(policy.model_copy(update={'stopped': True}), request, now=now)
    assert not stopped.allowed and 'campaign_disabled' in stopped.blockers

    expired = evaluate_call_admission(policy, request, now=now + timedelta(days=1))
    assert not expired.allowed
    assert {'campaign_expired', 'missing_recipient_consent'} <= set(expired.blockers)
