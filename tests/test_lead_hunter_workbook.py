"""Inspect the real XLSX package without a second spreadsheet dependency."""
from datetime import datetime, timezone
import socket
import stat
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from app.services.lead_hunter_workbook import export_workbook


NS = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
EXPECTED_SHEETS = ['Сводная', 'Новое внедрение', 'Доработка Битрикс24', 'Наши клиенты', 'HOLD', 'Квалифицировать']


def read_workbook(path):
    with ZipFile(path) as package:
        assert package.testzip() is None
        assert not any('externalLinks' in name or 'vbaProject' in name or 'connections.xml' in name
                       for name in package.namelist())
        workbook = ET.fromstring(package.read('xl/workbook.xml'))
        names = [element.attrib['name'] for element in workbook.findall('x:sheets/x:sheet', NS)]
        shared = ET.fromstring(package.read('xl/sharedStrings.xml'))
        strings = [''.join(si.itertext()) for si in shared]
        sheets = {}
        for i, name in enumerate(names, start=1):
            sheet = ET.fromstring(package.read(f'xl/worksheets/sheet{i}.xml'))
            assert sheet.find('.//x:f', NS) is None
            assert sheet.find('.//x:hyperlink', NS) is None
            cells = {}
            for cell in sheet.findall('.//x:c', NS):
                value = cell.findtext('x:v', namespaces=NS)
                if value is not None:
                    value = strings[int(value)] if cell.attrib.get('t') == 's' else float(value)
                cells[cell.attrib['r']] = value
            sheets[name] = cells
    return sheets


def detail_value(sheet, header, row=5):
    column, = (address[:-1] for address, value in sheet.items()
               if address.endswith('4') and value == header)
    return sheet.get(f'{column}{row}')


def test_workbook_lists_categories_and_overlapping_internal_views(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Workbook export must not access the network')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    name = '=HYPERLINK("https://example.com", "Unsafe formula")'
    rows = [
        {'name': name, 'category': 'A', 'need_score': 0, 'offer_score': None,
         'contact_score': 65, 'contact': '+7 700 000 00 00', 'qualified': False,
         'signals': ['Проверенный факт', '=1+1'], 'evidence_urls': ['https://example.com/evidence']},
        {'name': 'Bitrix account', 'category': 'B', 'direction': 'EXISTING BITRIX24',
         'need_score': 72.5, 'offer_score': 65, 'priority': 'high', 'active_task': True, 'qualified': False},
        {'name': 'Known customer', 'category': 'C', 'priority': 'HOLD', 'qualified': False},
        {'name': 'Ambiguous identity', 'category': 'D', 'priority': 'hold', 'qualified': True},
        {'name': 'Needs qualification', 'category': None, 'qualified': True},
    ]
    path = tmp_path / 'reports' / 'pipeline.xlsx'
    assert export_workbook(path, run={
        'id': 'run-01', 'city': 'Шымкент', 'status': 'completed',
        'started_at': datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc),
        'completed_at': '2026-11-06T10:05:00+00:00', 'counts': {'observed': 7},
    }, rows=rows) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]

    sheets = read_workbook(path)
    assert list(sheets) == EXPECTED_SHEETS
    summary = sheets['Сводная']
    assert [summary[f'{c}5'] for c in 'ABCDEFGH'] == [5, 1, 1, 1, 1, 2, 2, 1]
    assert [summary[f'A{i}'] for i in range(13, 18)] == [row['name'] for row in rows]
    assert summary['B7'] == 'run-01'
    assert summary['D7'] == 'Шымкент'
    assert summary['F7'] == 'completed'
    assert summary['B8'] == '2026-11-06T10:00:00+00:00'
    assert summary['D8'] == '2026-11-06T10:05:00+00:00'
    assert summary['B10'] == 'observed: 7'
    new = sheets['Новое внедрение']
    assert new['A5'] == name
    assert detail_value(new, 'NEED_SCORE / 100') == 0
    assert detail_value(new, 'OFFER_SCORE / 100') is None
    assert detail_value(new, 'CONTACT_SCORE / 100') == 65
    assert detail_value(new, 'Публичный деловой контакт') == '+7 700 000 00 00'
    assert detail_value(new, 'Подтверждённые сигналы') == 'Проверенный факт\n=1+1'
    assert detail_value(new, 'Ссылки на источники') == 'https://example.com/evidence'
    bitrix = sheets['Доработка Битрикс24']
    assert detail_value(bitrix, 'NEED_SCORE / 100') == 72.5
    assert detail_value(bitrix, 'Направление') == 'EXISTING BITRIX24'
    assert sheets['Наши клиенты']['A5'] == sheets['HOLD']['A5'] == 'Known customer'
    assert sheets['HOLD']['A6'] == sheets['Квалифицировать']['A5'] == 'Ambiguous identity'
    assert sheets['Квалифицировать']['A6'] == 'Needs qualification'


def test_workbook_empty_batch_has_all_sheets_and_zero_counts(tmp_path):
    sheets = read_workbook(export_workbook(tmp_path / 'empty.xlsx', run={}, rows=[]))
    assert list(sheets) == EXPECTED_SHEETS
    assert [sheets['Сводная'][f'{column}5'] for column in 'ABCDEFGH'] == [0] * 8
    assert sheets['Сводная']['A13'] == 'Нет записей'
    assert all(sheets[name]['A5'] == 'Нет записей' for name in EXPECTED_SHEETS[1:])


def test_workbook_rejects_active_hold_before_replacing_existing_report(tmp_path):
    path = tmp_path / 'report.xlsx'
    path.write_bytes(b'previous report')
    with pytest.raises(ValueError, match='HOLD record cannot recommend active work'):
        export_workbook(path, run={}, rows=[{'name': 'Blocked account', 'category': 'D', 'active_task': True}])
    assert path.read_bytes() == b'previous report'
    assert list(tmp_path.iterdir()) == [path]


def test_workbook_exports_both_drafts_with_internal_basis_only_when_routing_allows(tmp_path):
    cta = 'Удобно разобрать заявку за 15 минут?'
    copy = dict(whatsapp_text=('Когда родитель выбирает курс и филиал, администратору полезно сразу '
                              'видеть, кто подтвердит пробный урок. Мы из CRM Consulting. Предлагаем '
                              'показать на одном курсе, как пройти путь от первой заявки до отметки '
                              'о посещении. ' + cta),
                email_subject='Запись на пробный урок',
                email_text=('Здравствуйте! Покажем, как связать время ответа, филиал и явку в карточке '
                            'пробного занятия. Возьмём один курс и тестовую запись. ' + cta),
                cta=cta, recommended_channel='email')
    fields = dict(contact_permissibility='allow_draft', outreach_copy=copy, outreach_product='Битрикс24',
                  outreach_hypothesis='Внутренний факт о филиалах', outreach_sources=['https://business.kz/about'],
                  outreach_channel_verified=False)
    rows = [{'name': 'Проверить', 'category': 'A', **fields},
            {'name': 'Наш клиент', 'category': 'C', **fields},
            {'name': 'HOLD', 'category': 'D', **fields},
            {'name': 'Архив', 'category': 'B', **fields, 'contact_permissibility': 'archive_only'}]
    sheets = read_workbook(export_workbook(tmp_path / 'outreach.xlsx', run={}, rows=rows))
    new = sheets['Новое внедрение']
    assert detail_value(new, 'Черновик WhatsApp') == copy['whatsapp_text']
    assert detail_value(new, 'Черновик email') == copy['email_text']
    assert detail_value(new, 'Источники гипотезы · внутри') == 'https://business.kz/about'
    assert detail_value(new, 'Рекомендуемый канал') == 'Email · уточнить публичный контакт'
    for tab in ('Наши клиенты', 'HOLD', 'Доработка Битрикс24'):
        assert detail_value(sheets[tab], 'Черновик WhatsApp') is None
        assert detail_value(sheets[tab], 'Черновик email') is None
