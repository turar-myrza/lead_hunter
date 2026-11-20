import pytest

from app.integrations.sales_robovoice import prepare_robovoice_handoff
from app.services.sales_voice import VoiceDossier


def test_robovoice_preview_keeps_only_reviewable_script_values():
    dossier = VoiceDossier(company='Учебная компания', offer='Покажем маршрут заявки.',
                           product='CRM', source_urls=['https://source.kz/example'])

    preview = prepare_robovoice_handoff(dossier)

    assert preview.mode == 'dry_run'
    assert preview.planned_path == '/api/v1/scripts/invoke'
    assert preview.body_preview['channel'] == {'type':2, 'id':'<approved_sip_account_id>'}
    assert preview.body_preview['contact']['account'] == '<approved_test_phone>'
    assert preview.body_preview['parameters'] == {
        'company_name':'Учебная компания', 'offer':'Покажем маршрут заявки.', 'language':'ru',
    }
    assert 'https://' not in preview.model_dump_json()
    assert 'recordCallFromDial' not in preview.body_preview


@pytest.mark.parametrize('offer', ['Позвоните +7 (700) 000-00-00', 'Пишите name@provider.kz',
                                  'Смотрите https://site.kz/', 'Идентификатор 123456789012'])
def test_robovoice_preview_rejects_contact_data_in_copy(offer):
    with pytest.raises(ValueError, match='without contact details or links'):
        prepare_robovoice_handoff(VoiceDossier(company='Учебная компания', offer=offer))
