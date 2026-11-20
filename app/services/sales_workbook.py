"""Manager workbook generated from the persisted run."""
from pathlib import Path
from tempfile import NamedTemporaryFile
from xlsxwriter import Workbook


def export_sales_workbook(path, report):
    path = Path(path)
    with NamedTemporaryFile(dir=path.parent, prefix='.xlsx-', delete=False) as pending:
        temporary = Path(pending.name)
    try:
        with Workbook(temporary, {'strings_to_formulas':False, 'strings_to_urls':False}) as book:
            heading = book.add_format({'bold':True,'bg_color':'#183B53','font_color':'#FFFFFF','text_wrap':True,'valign':'vcenter'})
            wrapped = book.add_format({'text_wrap':True,'valign':'top','font_color':'#1B3344'})
            edit = book.add_format({'text_wrap':True,'valign':'top','bg_color':'#FFF4D6'})

            def table(label, headers, rows, widths, *, editable=()):
                page = book.add_worksheet(label)
                page.hide_gridlines(2)
                page.freeze_panes(1,1)
                page.set_row(0,32,heading)
                page.write_row(0,0,headers,heading)
                page.set_default_row(66)
                for col,width in enumerate(widths):
                    page.set_column(col,col,width,edit if col in editable else wrapped)
                for i, row in enumerate(rows, 1):
                    page.write_row(i,0,row)
                if rows:
                    page.autofilter(0,0,len(rows),len(headers)-1)
                return page

            rows = report['qualification']
            headers = ['Компания','Город','Продукт','Язык черновика','Канал из публичного источника','Проверка CRM',
                       'Публичный сигнал','Источники','Гипотеза','Индивидуальное предложение','Следующий шаг',
                       'Тема email','Email','Начало звонка','HTML','Допустимость','Автор черновика','Ответственный','Стадия']
            fields = ['name','city','product','language','contact','crm_state','fact','sources','hypothesis','offer','next_step',
                      'subject','email','call','visual','permission','copy_mode']
            table('Лиды',headers,[[row.get(k,row['qualification'].get(k,'')) for k in fields]+['Гайни','На квалификацию']
                                  for row in rows], [33,22,11,13,35,24,58,62,58,64,58,42,74,60,23,19,18,18,21], editable=(17,18))
            table('HOLD и резерв',['Компания','Город','Решение','Подтверждение','Причина','CRM','Следующий шаг'],
                  [[x['name'],x['city'],x['status'],x['fact'],x['reason'],x['crm_state'],x['next_step']] for x in report['review']],
                  [32,25,20,55,60,25,60])
            details = report['source_details']
            table('Источники',['Компания','Публичный источник','Факт','Публикация','Проверено','Тип'],
                  [[x['company'],x['url'],x['fact'],x.get('published') or 'Не указана', x.get('observed_at') or 'Не проверен',x['evidence_type']]
                   for x in details], [32,66,75,18,19,24])
            table('Сводная',['Проверка','Результат'],[
                ['Начало работы',f'Запуск {report["run_id"]} · {report["origin"]}'],
                ['Исследовано',report['researched']],['Квалификация',report['qualified']],
                ['Требуют проверки / исключены',report['needs_review']],['Готовые материалы',report['prepared']],
                ['Допуск к отправке','Черновики и проверка канала.'],
                ['Исходящие вызовы','Закрыто до проверки поставщика, правомерности и утверждения политики.'],
            ],[38,77])
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
