"""Local Excel view of already assessed Lead Hunter records.

This module does not score candidates, load CRM settings, or refresh sources.
"""
from collections import Counter
from datetime import date, datetime
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from xlsxwriter import Workbook

from app.models.outreach_copy import ManagerOutreachCopy


SHEETS = ('Сводная', 'Новое внедрение', 'Доработка Битрикс24', 'Наши клиенты', 'HOLD', 'Квалифицировать')
_COLUMNS = (
    ('name', 'Компания', 32),
    ('sector', 'Отрасль', 26),
    ('category', 'Категория', 12),
    ('direction', 'Направление', 25),
    ('need_score', 'NEED_SCORE / 100', 17),
    ('offer_score', 'OFFER_SCORE / 100', 17),
    ('contact_score', 'CONTACT_SCORE / 100', 17),
    ('crm_status', 'Статус CRM', 31),
    ('relationship_state', 'Тип отношений', 24),
    ('priority', 'Приоритет', 16),
    ('contact_permissibility', 'Допустимость обращения', 31),
    ('active_task', 'Рекомендована активная работа', 20),
    ('qualified', 'В очереди квалификации', 20),
    ('signals', 'Подтверждённые сигналы', 48),
    ('sources', 'Источники и актуальность', 48),
    ('evidence_urls', 'Ссылки на источники', 44),
    ('history', 'История CRM Consulting', 47),
    ('contact', 'Публичный деловой контакт', 31),
    ('offer', 'Предложение / гипотеза', 44),
    ('why_now', 'Почему актуально', 39),
    ('purchase_interest', 'Интерес к покупке', 23),
    ('channel', 'Рекомендуемый канал', 24),
    ('next_step', 'Следующее действие', 46),
    ('reasons', 'Обоснование решения', 47),
)
_OUTREACH_COLUMNS = (
    ('outreach_status', 'Статус черновика', 28),
    ('whatsapp_text', 'Черновик WhatsApp', 63),
    ('email_subject', 'Тема email', 33),
    ('email_text', 'Черновик email', 67),
    ('cta', 'Призыв к действию', 36),
    ('outreach_product', 'Продукт CRM Consulting', 30),
    ('outreach_hypothesis', 'Коммерческая гипотеза · внутри', 45),
    ('outreach_sources', 'Источники гипотезы · внутри', 44),
)
_CHANNEL_LABELS = {'whatsapp': 'WhatsApp', 'email': 'Email', 'phone': 'Деловой телефон'}
_SCORES = frozenset({'need_score', 'offer_score', 'contact_score'})
_FLAGS = frozenset({'qualified', 'active_task'})
_TRUNCATION = '\n… [сокращено для Excel]'
_CELL_LIMIT = 32_767


def _text(value):
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        return '\n'.join(_text(item) for item in value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _write_text(sheet, row, col, value, fmt):
    text = _text(value)
    if len(text) > _CELL_LIMIT:
        text = text[:_CELL_LIMIT - len(_TRUNCATION)] + _TRUNCATION
    if text:
        # Even strings starting with '=', '+', '-' or '@' are literal cells.
        # Do not interpret source links as workbook relationships either.
        sheet.write_string(row, col, text, fmt)
    else:
        sheet.write_blank(row, col, None, fmt)


def _is_hold(row):
    return row.get('category') == 'D' or _text(row.get('priority')).strip().casefold() == 'hold'


def _outreach_row(row):
    fields = {key: None for key, _, _ in _OUTREACH_COLUMNS if key not in {
        'outreach_hypothesis', 'outreach_product', 'outreach_sources',
    }}
    prepared = {**row, **fields, 'channel': None}
    if row.get('category') not in {'A', 'B'}:
        prepared['outreach_status'] = 'Холодный черновик закрыт'
    elif row.get('contact_permissibility') != 'allow_draft':
        prepared['outreach_status'] = 'Требуется проверка допуска'
    elif row.get('outreach_copy') is None:
        prepared['outreach_status'] = 'Тексты ещё не подготовлены'
    else:
        draft = row['outreach_copy']
        if isinstance(draft, ManagerOutreachCopy):
            draft = draft.model_dump()
        draft = ManagerOutreachCopy.model_validate(draft)
        if not all(row.get(key) for key in ('outreach_hypothesis', 'outreach_product', 'outreach_sources')):
            raise ValueError('Outreach requires an internal hypothesis, product and sources')
        channel = _CHANNEL_LABELS[draft.recommended_channel]
        if row.get('outreach_channel_verified') is False:
            channel += ' · уточнить публичный контакт'
        prepared.update(outreach_status='Черновик на согласование',
                        whatsapp_text=draft.whatsapp_text, email_subject=draft.email_subject,
                        email_text=draft.email_text, cta=draft.cta, channel=channel)
    return prepared


def _check_rows(rows):
    # Excel allows 1,048,576 rows; the summary uses twelve rows before its data.
    if len(rows) > 1_048_564:
        raise ValueError('Too many records for the Excel summary sheet')
    for i, row in enumerate(rows, start=1):
        if not isinstance(row.get('name'), str) or not row['name'].strip():
            raise ValueError(f'Row {i} has no company name')
        if row.get('category') not in (None, '', 'A', 'B', 'C', 'D'):
            raise ValueError(f'Row {i} has an unknown category')
        for key in _SCORES:
            value = row.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 100
            ):
                raise ValueError(f'Row {i}: {key} must be a score from 0 to 100 or null')
        for key in _FLAGS:
            if key in row and not isinstance(row[key], bool):
                raise ValueError(f'Row {i}: {key} must be boolean')
        if _is_hold(row) and row.get('active_task'):
            raise ValueError(f'Row {i}: a HOLD record cannot recommend active work')


def _new_formats(book):
    return {
        'title': book.add_format({'bold': True, 'font_size': 16, 'font_color': '#17243A'}),
        'note': book.add_format({'font_color': '#475569', 'text_wrap': True, 'valign': 'vcenter'}),
        'meta': book.add_format({'bold': True, 'font_color': '#243858', 'valign': 'top'}),
        'header': book.add_format({'bold': True, 'bg_color': '#243858', 'font_color': '#FFFFFF',
                                   'text_wrap': True, 'valign': 'vcenter'}),
        'text': book.add_format({'num_format': '@', 'text_wrap': True, 'valign': 'top'}),
        'number': book.add_format({'valign': 'top'}),
        'hold': book.add_format({'bg_color': '#FFF0CB', 'font_color': '#805600', 'valign': 'top'}),
    }


def _title(sheet, title, note, formats):
    sheet.hide_gridlines(2)
    sheet.set_row(0, 27)
    sheet.set_row(1, 30)
    sheet.merge_range(0, 0, 0, 7, title, formats['title'])
    sheet.merge_range(1, 0, 1, 7, note, formats['note'])


def _summary(sheet, run, rows, hold_count, qualification_count, formats):
    category_counts = Counter(row.get('category') for row in rows)
    metrics = (('Всего записей', len(rows)), ('A', category_counts['A']), ('B', category_counts['B']),
               ('C', category_counts['C']), ('D', category_counts['D']),
               ('HOLD¹', hold_count), ('Квалифицировать¹', qualification_count),
               ('Активная работа', sum(row.get('active_task', False) for row in rows)))
    for col, (label, count) in enumerate(metrics):
        sheet.write_string(3, col, label, formats['header'])
        sheet.write_number(4, col, count, formats['number'])
    sheet.set_row(3, 32)
    for i, (label, key) in enumerate((('Запуск', 'id'), ('Город', 'city'), ('Статус', 'status'),
                                     ('Начат', 'started_at'), ('Завершён', 'completed_at'),
                                     ('Дата оценки', 'evaluated_as_of'))):
        row, col = 6 + i // 3, 2 * (i % 3)
        sheet.set_row(row, 32)
        sheet.write_string(row, col, label, formats['meta'])
        _write_text(sheet, row, col + 1, run.get(key), formats['text'])
    counts = run.get('counts') or {}
    if counts:
        sheet.write_string(9, 0, 'Счётчики запуска', formats['meta'])
        sheet.merge_range(9, 1, 9, 7, '', formats['note'])
        _write_text(sheet, 9, 1, '; '.join(f'{key}: {_text(value)}' for key, value in counts.items()), formats['note'])
        sheet.set_row(9, 28)


def _table(sheet, rows, header_row, formats, columns):
    sheet.set_row(header_row, 31)
    for col, (_, label, width) in enumerate(columns):
        sheet.set_column(col, col, width)
        sheet.write_string(header_row, col, label, formats['header'])
    sheet.freeze_panes(header_row + 1, 1)
    if not rows:
        sheet.write_string(header_row + 1, 0, 'Нет записей', formats['note'])
        return
    sheet.autofilter(header_row, 0, header_row + len(rows), len(columns) - 1)
    for index, row in enumerate(rows, start=header_row + 1):
        line_count = 1
        for col, (key, _, width) in enumerate(columns):
            value = row.get(key)
            if key in _SCORES:
                if value is None:
                    sheet.write_blank(index, col, None, formats['number'])
                else:
                    sheet.write_number(index, col, value, formats['number'])
                continue
            if key in _FLAGS:
                value = 'Да' if value else 'Нет' if value is not None else None
            text = _text(value)
            line_count = max(line_count, sum(max(1, math.ceil(len(line) / (width - 3))) for line in text.splitlines()))
            fmt = formats['hold'] if key == 'priority' and _is_hold(row) else formats['text']
            _write_text(sheet, index, col, text, fmt)
        sheet.set_row(index, min(400, max(26, 15 * line_count + 6)))


def export_workbook(path: str | Path, *, run: dict, rows: list[dict]) -> Path:
    """Write a private XLSX; HOLD and qualification may repeat category rows.

    ``qualified=True`` includes a row in the internal qualification queue.
    Categories are exclusive; D and other HOLD priorities share the HOLD view.
    No date, score, permission or campaign state is inferred from text.
    """
    _check_rows(rows)
    rows = [_outreach_row(row) for row in rows]
    target = Path(path)
    if target.suffix.lower() != '.xlsx':
        raise ValueError('Workbook path must end in .xlsx')
    target.parent.mkdir(parents=True, exist_ok=True)
    grouped = (
        rows,
        [row for row in rows if row.get('category') == 'A'],
        [row for row in rows if row.get('category') == 'B'],
        [row for row in rows if row.get('category') == 'C'],
        [row for row in rows if _is_hold(row)],
        [row for row in rows if row.get('qualified') is True],
    )
    notes = (
        '¹ HOLD и очередь квалификации могут пересекаться с категориями. Пустой балл означает «не оценён».',
        'Категория A — гипотеза внедрения. Неизвестный статус CRM не означает отсутствие CRM.',
        'Категория B — потенциальные доработки подтверждённого Битрикс24.',
        'Категория C сохраняется для подтверждённых отношений, в том числе при HOLD предложения.',
        'Категория D и записи с приоритетом HOLD. Только проверка ограничений; коммерческая работа не рекомендована.',
        'Внутренняя очередь проверки. Наличие записи здесь не означает допуск к обращению.',
    )
    temp_path = None
    try:
        # The workbook and any intermediate XML stay in memory or in a 0600
        # file. Publish atomically; do not follow an existing output symlink.
        with NamedTemporaryFile(mode='w+b', dir=target.parent, prefix='.lead-hunter-', delete=False) as stream:
            temp_path = Path(stream.name)
            with Workbook(stream, {'in_memory': True, 'strings_to_formulas': False,
                                   'strings_to_urls': False}) as book:
                formats = _new_formats(book)
                for index, (name, members, note) in enumerate(zip(SHEETS, grouped, notes)):
                    sheet = book.add_worksheet(name)
                    _title(sheet, 'AI Lead Hunter — сводная' if index == 0 else name, note, formats)
                    if index == 0:
                        _summary(sheet, run, rows, len(grouped[4]), len(grouped[5]), formats)
                    columns = _COLUMNS + _OUTREACH_COLUMNS
                    if index in (1, 2):
                        # The manager opens a direction sheet to see the copy beside the account.
                        channel, = (col for col in _COLUMNS if col[0] == 'channel')
                        columns = (_COLUMNS[:2] + (channel,) + _OUTREACH_COLUMNS[:5]
                                   + tuple(col for col in _COLUMNS[2:] if col != channel)
                                   + _OUTREACH_COLUMNS[5:])
                    _table(sheet, members, 11 if index == 0 else 3, formats, columns)
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return target
