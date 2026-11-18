"""Local autonomous sales worker and dashboard, with a separate review-only channel."""
import argparse
import asyncio
import json
import plistlib
from pathlib import Path
import sys
import time

from app.models.sales_team import Outcome, Prospect
from app.services.sales_store import DEFAULT_DB, SalesStore
from app.services.sales_team import OUT_DIR, Stopped, load_skills, process_run

ROOT = Path(__file__).resolve().parents[1]


def _input(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _profile(path):
    if not path:
        return []
    rows = _input(path)
    if not isinstance(rows, list) or len(rows) > 20 or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Профиль источников должен содержать не более 20 записей')
    return rows


def _fixture(model):
    company = Prospect(
        name='Учебный поставщик «Арна»', city='Караганда', industry='Поставка промышленного оборудования',
        sources=[{'url':'https://www.gov.kz/', 'fact':'Учебное описание проектной поставки насосного оборудования.'}],
        product='A', fact='Учебная карточка проектной поставки', process='Подбор насоса и техническое предложение',
        hypothesis='Проверка передачи технической спецификации от инженера продавцу.',
        offer='На условной заявке показать связь выбора насоса с согласованной спецификацией и сроком ответа.',
        next_step='Передать запрос демонстрации Гайни.',
        visual_steps=['Запрос паспорта', 'Инженер подтвердил параметры', 'Менеджер согласовал срок ответа'],
        outcome='Видно, кто проверяет спецификацию и когда заказчик получит ответ.',
    )
    return {'prospects':[company.model_dump(mode='json')], 'model':model,
            'voice_variants':{'kk':'Сорғы таңдауды, келісілген сипаттаманы және жауап беру мерзімін бір оқу өтінімінде көрсетеміз.'},
            'voice_turns':['Қазақша айтыңыз', 'Бағасы қанша?', 'Иә']}


def advance(store, config, *, interval=None, stop_at=None):
    if interval is not None:
        store.tick(interval)
    with store.worker_lock() as acquired:
        if not acquired:
            return
        while (run := store.claim()) is not None:
            try:
                result = asyncio.run(process_run(store,run,**config))
                print(json.dumps({'id':run['id'],'prepared':result['prepared'], 'qualified':result['qualified'],
                                 'review':result['needs_review']},ensure_ascii=False),flush=True)
            except Stopped:
                print('Очередь остановлена. Контрольные точки сохранены.',flush=True)
                break
            except (ValueError, OSError, RuntimeError) as exc:
                # A failing source or local dependency must not silently complete the item.
                store.finish(run['id'],'failed',type(exc).__name__)
                print(f'Запуск {run["id"]} завершился ошибкой {type(exc).__name__}: {exc}',file=sys.stderr,flush=True)
                return 2
            if run['id']==stop_at:
                return 0
    return 0


def _launchd(args):
    python = ROOT / '.venv/bin/python'
    directory = args.db.resolve().parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [str(python),'-m','app.sales_team','--db',str(args.db.resolve()),'--output',str(args.output.resolve())]
    if args.source_config:
        command += ['--source-config',str(args.source_config.resolve())]
    if args.previous:
        command += ['--previous',str(args.previous.resolve())]
    command += ['worker','--once','--interval',str(args.interval)]
    return {'Label':'kz.crmconsulting.sales-team','ProgramArguments':command,'WorkingDirectory':str(ROOT),
            'StartInterval':900,'RunAtLoad':True,'ProcessType':'Background',
            'StandardOutPath':str(directory/'worker.log'),'StandardErrorPath':str(directory/'worker.log')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=DEFAULT_DB)
    parser.add_argument('--output',type=Path,default=OUT_DIR)
    parser.add_argument('--source-config',type=Path)
    parser.add_argument('--previous',type=Path, help='JSON список ключей компаний из предыдущих исследованных портфелей')
    commands = parser.add_subparsers(dest='action', required=True)
    run = commands.add_parser('run')
    run.add_argument('--prospects',type=Path,help='Входной пакет публичных свидетельств. Без него работает автоматический поиск.')
    run.add_argument('--model',choices=('rules','codex'),default='rules')
    run.add_argument('--model-limit',type=int,choices=(1,2,3),default=1)
    run.add_argument('--limit',type=int,choices=range(1,31),default=30)
    simulation = commands.add_parser('simulate')
    simulation.add_argument('--model',choices=('rules','codex'),default='rules')
    worker = commands.add_parser('worker')
    worker.add_argument('--interval',type=int,default=86400)
    worker.add_argument('--once',action='store_true')
    dashboard = commands.add_parser('dashboard')
    dashboard.add_argument('--port',type=int,default=8792)
    result = commands.add_parser('record')
    result.add_argument('event',type=Path)
    schedule = commands.add_parser('schedule-plist')
    schedule.add_argument('--interval',type=int,default=86400)
    for name in ('status','stop','resume','skills'):
        commands.add_parser(name)
    args = parser.parse_args(argv)
    if args.action=='skills':
        print(json.dumps([s.reference() for s in load_skills().values()],ensure_ascii=False))
        return 0
    if args.action=='schedule-plist':
        sys.stdout.buffer.write(plistlib.dumps(_launchd(args)))
        return 0
    store=SalesStore(args.db)
    if args.action=='status':
        print(json.dumps(store.stats(),ensure_ascii=False,indent=2))
        return 0
    if args.action in {'stop','resume'}:
        store.set_stopped(args.action=='stop')
        return 0
    if args.action=='record':
        print('записано' if store.record(Outcome.model_validate(_input(args.event))) else 'уже записано')
        return 0
    if args.action=='dashboard':
        import uvicorn
        from app.api.sales_team_dashboard import create_dashboard
        uvicorn.run(create_dashboard(store,args.output.resolve()),host='127.0.0.1',port=args.port,loop='asyncio',http='h11',ws='none')
        return 0
    previous = _input(args.previous) if args.previous else []
    if not isinstance(previous,list) or not all(isinstance(item,str) for item in previous):
        raise ValueError('Предыдущие компании ожидаются в виде списка строковых ключей')
    config = {'output_root':args.output.resolve(),'sources':_profile(args.source_config),'previous_keys':previous}
    if args.action=='worker':
        if args.interval<300:
            raise ValueError('Минимальный интервал поиска — 300 секунд')
        while True:
            rc = advance(store,config,interval=args.interval)
            if args.once:
                return rc
            time.sleep(15)
    if args.action=='simulate':
        ident=store.enqueue('simulation',_fixture(args.model))
    elif args.prospects:
        rows=_input(args.prospects)
        if not isinstance(rows,list) or not 1<=len(rows)<=150:
            raise ValueError('Публичный пакет должен содержать от 1 до 150 компаний')
        valid=[Prospect.model_validate(row).model_dump(mode='json') for row in rows]
        ident=store.enqueue('public_batch',{'prospects':valid,'limit':args.limit,
                                           'model':args.model,'model_limit':args.model_limit})
    else:
        ident=store.enqueue('automatic',{'limit':args.limit,'model':args.model,'model_limit':args.model_limit})
    print(f'Запуск {ident} в очереди',flush=True)
    return advance(store,config,stop_at=ident)


if __name__ == '__main__':
    raise SystemExit(main())
