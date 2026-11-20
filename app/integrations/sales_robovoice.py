"""Local review of documented Robovoice script input. No network client."""
import re
from typing import Literal

from pydantic import BaseModel

from app.services.sales_voice import VoiceDossier


_EXTERNAL_DATA = re.compile(
    r'https?://|www\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}|'
    r'(?<!\d)(?:\+?7|8)(?:[ ()-]*\d){10}(?!\d)|'
    r'(?<!\d)\d{12}(?!\d)',
    re.I,
)


class RobovoicePreview(BaseModel):
    mode: Literal['dry_run'] = 'dry_run'
    provider: Literal['robovoice'] = 'robovoice'
    planned_method: Literal['POST'] = 'POST'
    planned_path: Literal['/api/v1/scripts/invoke'] = '/api/v1/scripts/invoke'
    body_preview: dict


def prepare_robovoice_handoff(dossier: VoiceDossier) -> RobovoicePreview:
    """Preview public scenario parameters without accepting a number or token."""
    offer = dossier.language_variants.get(dossier.language, dossier.offer)
    values = {'company_name':dossier.company, 'offer':offer, 'language':dossier.language}
    if any(_EXTERNAL_DATA.search(value) for value in values.values()):
        raise ValueError('Robovoice preview accepts customer-facing copy without contact details or links')
    return RobovoicePreview(body_preview={
        'scriptId':'<configured_script_id>',
        'channel':{'type':2, 'id':'<approved_sip_account_id>'},
        'contact':{'account':'<approved_test_phone>'},
        'parameters':values,
    })
