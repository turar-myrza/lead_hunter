"""Exercise the inherited document engine without dispatch or LibreOffice."""
from pathlib import Path
from zipfile import ZipFile
import pytest

@pytest.mark.parametrize('lang',['ru','kz'])
def test_existing_template_generates_real_docx(tmp_path,lang):
    pytest.importorskip('docxtpl')
    from documents.offer_generator import generate_commercial_offer
    result=generate_commercial_offer(client_company='Synthetic & Sons',
        items=[{'name':'Synthetic product','description':'A & B','price':'0'}],
        lang=lang,output_docx_path=str(tmp_path/'offer.docx'),
        output_pdf_path=str(tmp_path/'offer.pdf'),convert_to_pdf=False)
    assert result['pdf'] is None
    with ZipFile(result['docx']) as doc:
        xml=doc.read('word/document.xml')
        from xml.etree import ElementTree
        ElementTree.fromstring(xml)
        assert b'Synthetic &amp; Sons' in xml
        assert b'Synthetic product' in xml
    assert not (tmp_path/'offer.pdf').exists()
