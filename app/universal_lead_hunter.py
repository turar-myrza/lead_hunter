"""CLI commands for the universal workflow, mounted by app.lead_hunter."""

import asyncio
import json
from pathlib import Path


def add_commands(parser):
    parser.add_argument('--db', type=Path)
    parser.add_argument('--output-root', type=Path)
    commands = parser.add_subparsers(dest='universal_action', required=True)
    submit = commands.add_parser('submit', help='Submit a product-brief request JSON and process the run')
    submit.add_argument('request', type=Path)
    submit.add_argument('--queue-only', action='store_true')
    for name in ('process', 'status', 'result'):
        commands.add_parser(name).add_argument('run_id', type=int)
    commands.add_parser('sources')


def execute(args, parser):
    from pydantic import ValidationError

    from app.core.config import get_settings
    from app.models.universal_lead_hunter import RunRequest
    from app.services.sales_store import SalesStore
    from app.services.universal_lead_hunter import LeadHunterApplication

    settings = get_settings()
    service = LeadHunterApplication(SalesStore(args.db or settings.lead_hunter_db),
                                    args.output_root or settings.lead_hunter_output)
    action = args.universal_action
    if action == 'submit':
        try:
            request = RunRequest.model_validate_json(args.request.read_bytes())
        except (OSError, ValidationError):
            parser.error('Expected a readable JSON file matching the universal RunRequest schema.')
        try:
            run_id = service.submit(request)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps({'run_id': run_id, 'status': 'queued'}), flush=True)
        if not args.queue_only:
            asyncio.run(service.process(run_id))
            if service.status(run_id).status == 'failed':
                raise SystemExit(2)
    elif action == 'sources':
        print(json.dumps([source.model_dump(mode='json') for source in service.capabilities()], ensure_ascii=False, indent=2))
    else:
        status = service.status(args.run_id)
        if status is None:
            parser.error('Run not found.')
        if action == 'process':
            asyncio.run(service.process(args.run_id))
            status = service.status(args.run_id)
        value = service.result(args.run_id) if action == 'result' else status
        if value is None:
            parser.error('Run result is unavailable. Check run status.')
        print(value.model_dump_json(indent=2))
        if action == 'process' and status.status == 'failed':
            raise SystemExit(2)
