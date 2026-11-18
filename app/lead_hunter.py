"""Lead Hunter CLI: research, READ ONLY verification, and local export.

The run command performs public research without Codex. It uses the same
isolated CRM read client as check-crm and review-history.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.models.lead_hunter import ResearchBatch
from app.services.lead_hunter import csv_export, markdown_report, ranked, validate_batch_identity

QUERIES = [
    'Шымкент вакансии менеджер продаж "входящих заявок" CRM',
    'Шымкент "расширение отдела продаж"',
    'Шымкент "менеджер" "Битрикс24"',
    'Шымкент производство оптовые продажи WhatsApp контакты',
    'Шымкент "CRM специалист"',
    'Шымкент "постоянный поток" "заявок"',
]


def write_private(path: Path, text: str, encoding='utf-8'):
    """Reports may include internal CRM ownership/history; keep them local/private."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, 'w', encoding=encoding, newline='') as stream:
        stream.write(text)


async def check_crm(batch):
    # Import configuration/credentials only for the explicitly requested read check.
    from app.core.config import get_settings
    from app.core.exceptions import Bitrix24ConfigurationError
    from app.integrations.lead_hunter_crm import LeadHunterCRMClient, CRMReadError
    from app.models.lead_hunter_crm import CRMCheck
    from app.services.lead_hunter_duplicates import DuplicateService
    from datetime import datetime, timezone
    try:
        settings = get_settings()
        async with LeadHunterCRMClient(settings.require_webhook_url(), timeout=settings.bitrix24_timeout_seconds) as client:
            await DuplicateService(client).check(batch)
            batch.crm_read_audit = dict(client.method_counts)
            print(f'CRM READ ONLY attempts finished; HTTP requests: {client.request_count}. No write methods available.')
            print('Verification states: ' + ', '.join(sorted({c.crm_check.state for c in batch.candidates})))
    except (Bitrix24ConfigurationError, CRMReadError):
        for candidate in batch.candidates:
            candidate.crm_check = CRMCheck(state='unavailable', checked_at=datetime.now(timezone.utc),
                limitations=['CRM connection/configuration unavailable; duplicate absence is NOT established. Check local credentials/network without sharing the webhook.'])
        print('CRM check unavailable. Saved explicit unavailable status; import remains blocked.')
    return batch


async def review_history(batch):
    """Explicit, targeted READ ONLY history review after identity checking."""
    from datetime import datetime, timezone
    from app.core.config import get_settings
    from app.core.exceptions import Bitrix24ConfigurationError
    from app.integrations.lead_hunter_crm import LeadHunterCRMClient, CRMReadError
    from app.models.lead_hunter_relationships import RelationshipReview
    from app.services.lead_hunter_relationships import RelationshipService
    try:
        settings=get_settings()
        async with LeadHunterCRMClient(settings.require_webhook_url(),timeout=settings.bitrix24_timeout_seconds) as client:
            await RelationshipService(client).enrich(batch)
            for method,count in client.method_counts.items():
                batch.crm_read_audit[method]=batch.crm_read_audit.get(method,0)+count
            print(f'Targeted history READ ONLY requests: {client.request_count}. No outbound operations.')
    except (Bitrix24ConfigurationError,CRMReadError):
        for c in batch.candidates:
            c.relationship_review=RelationshipReview(status='unavailable',checked_at=datetime.now(timezone.utc),
              limitations=['История недоступна: проверить локальную конфигурацию и права. Коммерческие рекомендации заблокированы.'])
        print('History unavailable; saved explicit hold condition. No CRM changes.')
    return batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('plan')
    commands.add_parser('schema')
    run = commands.add_parser('run', help='Search and classify up to three accounts; checkpoint each stage')
    run.add_argument('--city', choices=['Шымкент'])
    run.add_argument('--limit', type=int, choices=range(1, 4))
    run.add_argument('--direction', choices=['both', 'new-crm', 'existing-bitrix24'])
    run.add_argument('--public-only', action='store_true', help='Skip CRM; export research only')
    run.add_argument('--sources', type=Path, help='Optional TOML registry of additional permitted public sources')
    location = run.add_mutually_exclusive_group()
    location.add_argument('--run-dir', type=Path, help='New directory for checkpoints and reports')
    location.add_argument('--resume', type=Path, help='Existing run directory; reuse committed observations')
    workbook = commands.add_parser('export-xlsx', help='Export an existing v1.2 evidence batch without network calls')
    workbook.add_argument('input', type=Path)
    workbook.add_argument('--output', type=Path, required=True)
    workbook.add_argument('--as-of', help='ISO 8601 timestamp with timezone, historical replay only')
    crm = commands.add_parser('check-crm', help='Explicit READ ONLY CRM duplicate check; no CRM writes')
    crm.add_argument('input', type=Path)
    crm.add_argument('--output', type=Path, required=True)
    crm.add_argument('--expanded-reviewed', action='store_true')
    history=commands.add_parser('review-history',help='Targeted READ ONLY relationship review; no sending or CRM writes')
    history.add_argument('input',type=Path)
    history.add_argument('--output',type=Path,required=True)
    history.add_argument('--expanded-reviewed',action='store_true')
    export = commands.add_parser('export')
    export.add_argument('input', type=Path)
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--expanded-reviewed', action='store_true', help='Use only after user review of pilot')
    args = parser.parse_args()
    if args.command == 'run':
        from uuid import uuid4
        from app.services.lead_hunter_run import RunOptions, RunStore, execute

        if args.resume and (args.city or args.limit or args.direction or args.public_only or args.sources):
            parser.error('--resume uses the original options; start a new run to change them')
        if args.resume:
            if not (args.resume / 'run.json').is_file():
                parser.error('--resume must point to an existing Lead Hunter run directory')
            store = RunStore(args.resume)
        else:
            directory = args.run_dir or Path('outputs/lead_hunter/runs') / uuid4().hex
            try:
                sources = []
                if args.sources:
                    import tomllib

                    with args.sources.open('rb') as stream:
                        data = stream.read(65537)
                    if len(data) > 65536:
                        raise ValueError
                    registry = tomllib.loads(data.decode('utf-8'))
                    if set(registry) != {'sources'} or not isinstance(registry['sources'], list):
                        raise ValueError
                    sources = registry['sources']
                store = RunStore.create(directory, RunOptions(city=args.city or 'Шымкент', limit=args.limit or 3,
                                            direction=args.direction or 'both', public_only=args.public_only, sources=sources))
            except FileExistsError:
                parser.error('Run directory already exists; use --resume or choose a new directory')
            except (ValueError, OSError):
                parser.error('Could not create run or read source registry; check permissions and the TOML format')
        print(f'Run directory: {store.path.resolve()}', flush=True)
        try:
            summary = asyncio.run(execute(store, check=check_crm, history=review_history))
        except KeyboardInterrupt:
            print('Run interrupted. Use run --resume with the same directory.', flush=True)
            raise SystemExit(130) from None
        except Exception as exc:
            # No uncontrolled remote exception text, query, or credential in CLI logs.
            parser.exit(2, f'Run stopped ({type(exc).__name__}); inspect events.jsonl and resume.\n')
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary['status'] in {'discovery_unavailable', 'partial_public', 'partial_crm', 'stale_crm_snapshot'}:
            raise SystemExit(2)
    elif args.command == 'export-xlsx':
        from datetime import datetime
        from app.services.lead_hunter_run import project_rows, report_counts, utcnow
        from app.services.lead_hunter_workbook import export_workbook

        if args.output.suffix.lower() != '.xlsx':
            parser.error('Excel output must end in .xlsx')
        batch = ResearchBatch.model_validate_json(args.input.read_text(encoding='utf-8'))
        if batch.version != '1.2':
            parser.error('Excel export requires a v1.2 evidence batch')
        validate_batch_identity(batch)
        now = utcnow()
        try:
            as_of = datetime.fromisoformat(args.as_of) if args.as_of else now
            if as_of.tzinfo is None or as_of > now:
                raise ValueError
        except ValueError:
            parser.error('--as-of must be an ISO 8601 timestamp with timezone, no later than now')
        if args.as_of:
            observations = [stamp for c in batch.candidates
                            for stamp in (c.crm_check.checked_at, c.relationship_review.checked_at,
                                          *(s.checked_at for s in c.sources), *(s.last_attempt_at for s in c.sources))
                            if stamp is not None]
            if any(t.tzinfo is None or t > as_of for t in observations):
                parser.error('Evidence contains observations later than --as-of; use the snapshot from that date')
        rows = list(project_rows(batch, as_of, archive=bool(args.as_of)))
        export_workbook(args.output, rows=rows, run={
            'id': 'offline-export', 'city': 'Шымкент', 'status': 'archive_only' if args.as_of else 'offline_export',
            'completed_at': now.isoformat(), 'evaluated_as_of': as_of.isoformat(), 'counts': report_counts(rows),
        })
        print(f'Excel: {args.output.resolve()}. No source refresh or CRM access.')
    elif args.command == 'plan':
        print(json.dumps({'mode': 'Codex session; not an unattended background process',
                          'queries': QUERIES, 'pilot_limit': 5,
                          'version': '1.1',
                          'required': ['Verify current public contacts', 'Record vacancy date/status',
                                       'Separate need/contact/purchase interest', 'Explicit READ ONLY check-crm before import eligibility'],
                          'runbook': 'docs/lead_hunter.md'}, ensure_ascii=False, indent=2))
    elif args.command == 'schema':
        print(json.dumps(ResearchBatch.model_json_schema(), ensure_ascii=False, indent=2))
    else:
        batch = ResearchBatch.model_validate_json(args.input.read_text(encoding='utf-8'))
        if batch.mode == 'expanded' and not args.expanded_reviewed:
            parser.error('Expanded export requires pilot review and --expanded-reviewed')
        if args.command in {'check-crm','review-history'}:
            if batch.version not in {'1.1','1.2'} or (args.command=='review-history' and batch.version!='1.2'):
                parser.error('Identity checking requires v1.1/v1.2; relationship review requires v1.2')
            batch = asyncio.run(check_crm(batch) if args.command=='check-crm' else review_history(batch))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_private(args.output, batch.model_dump_json(indent=2))
            print('Local CRM verification result saved. No leads created.')
            return
        if batch.version=='1.2':
            validate_batch_identity(batch)  # Identity validation precedes category-specific scoring.
        else:
            ranked(batch)  # Legacy export validation/ordering remains unchanged.
        args.output.mkdir(parents=True, exist_ok=True)
        if batch.version == '1.2':
            from datetime import datetime,timezone
            from app.services.lead_hunter_v12_reporting import csv_v12,report_v12,decisions
            now=datetime.now(timezone.utc)
            for category in ['A','B','C','D']:
                write_private(args.output/f'category_{category}.csv',csv_v12(batch,category,now),'utf-8-sig')
            write_private(args.output/'review.csv',csv_v12(batch,as_of=now),'utf-8-sig')
            write_private(args.output/'report.md',report_v12(batch,now))
            write_private(args.output/'decisions.json',json.dumps([d.model_dump(mode='json') for _,d in decisions(batch,now)],ensure_ascii=False,indent=2))
            write_private(args.output/'evidence.json',batch.model_dump_json(indent=2))
            print(f'Exported {len(batch.candidates)} classified accounts to four separate local CSVs. No CRM writes or sending.')
            return
        if batch.version == '1.1':
            from datetime import datetime, timezone
            from app.services.lead_hunter_reporting import csv_v11, report_v11
            as_of = datetime.now(timezone.utc)
            write_private(args.output / 'review.csv', csv_v11(batch, as_of=as_of), 'utf-8-sig')
            write_private(args.output / 'companies.csv', csv_v11(batch, import_only=True, as_of=as_of), 'utf-8-sig')
            write_private(args.output / 'report.md', report_v11(batch, as_of=as_of))
            write_private(args.output / 'evidence.json', batch.model_dump_json(indent=2))
            print(f'Exported review of {len(batch.candidates)} companies; import CSV excludes unresolved/confirmed matches and unavailable checks.')
            return
        (args.output / 'companies.csv').write_text(csv_export(batch), encoding='utf-8-sig', newline='')
        (args.output / 'report.md').write_text(markdown_report(batch), encoding='utf-8')
        (args.output / 'evidence.json').write_text(batch.model_dump_json(indent=2), encoding='utf-8')
        print(f'Exported {len(batch.candidates)} companies to {args.output.resolve()}')


if __name__ == '__main__':
    main()
