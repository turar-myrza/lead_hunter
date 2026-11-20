"""Offline three-account pilot export and explicit local outcome accounting."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from app.lead_hunter import write_private
from app.models.account_outreach import AccountOpportunityBrief, AccountOutcomeEvent, AccountOutcomeLedger
from app.models.lead_hunter import ResearchBatch
from app.services.account_outreach import export_brief, validate_brief, validate_portfolio, append_outcome, outcome_summary
from app.services.lead_hunter_routing import route


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    export=commands.add_parser('export-pilot',help='Validate and export at most three local drafts; never sends')
    export.add_argument('research',type=Path)
    export.add_argument('briefs',type=Path)
    export.add_argument('--output',type=Path,required=True)
    export.add_argument('--allow-relationship-followup', action='store_true',
                        help='Explicit permission for local category C follow-up drafts; never sends')
    event=commands.add_parser('record-outcome',help='Append one explicitly entered local event, no CRM writes')
    event.add_argument('event',type=Path)
    event.add_argument('--ledger',type=Path,required=True)
    summary=commands.add_parser('metrics',help='Read explicitly entered local outcomes')
    summary.add_argument('ledger',type=Path)
    args=parser.parse_args()
    if args.command=='export-pilot':
        batch=ResearchBatch.model_validate_json(args.research.read_text(encoding='utf-8'))
        if batch.version!='1.2': parser.error('Account Outreach requires the v1.2 history-first model')
        raw=json.loads(args.briefs.read_text(encoding='utf-8'))
        if not isinstance(raw,list) or not 1<=len(raw)<=3:
            parser.error('Approved pilot contains at most three accounts; expansion requires a separate user decision')
        briefs=[AccountOpportunityBrief.model_validate(x) for x in raw]
        if len({b.candidate_name for b in briefs})!=len(briefs): parser.error('Duplicate pilot accounts')
        candidates={c.name:c for c in batch.candidates}
        now=datetime.now(timezone.utc)
        prepared=[]
        for brief in briefs:
            if brief.candidate_name not in candidates: parser.error('Account must come from the classified Lead Hunter batch')
            c=candidates[brief.candidate_name];d=route(c,now)
            validate_brief(brief,c,d,as_of=now,allow_relationship_followup=args.allow_relationship_followup)
            prepared.append((brief,c,d))
        validate_portfolio(briefs)
        # Validate the entire pilot before creating any result files.
        args.output.mkdir(parents=True,exist_ok=True)
        for index,(brief,c,d) in enumerate(prepared,1):
            export_brief(brief,c,d,args.output,as_of=now,stem=f'account-{index}',
                         allow_relationship_followup=args.allow_relationship_followup)
        write_private(args.output/'pilot_summary.json',json.dumps({'prepared':len(briefs),'sent':0,'calls':0,'crm_writes':0,
            'accounts':[b.candidate_name for b in briefs],'status':'draft_only'},ensure_ascii=False,indent=2))
        print(f'Exported {len(briefs)} local drafts; no messages, calls or CRM writes.')
    elif args.command=='record-outcome':
        event=AccountOutcomeEvent.model_validate_json(args.event.read_text(encoding='utf-8'))
        added=append_outcome(args.ledger,event)
        print('Local event recorded.' if added else 'Identical event already recorded; no duplicate.')
    else:
        ledger=AccountOutcomeLedger.model_validate_json(args.ledger.read_text(encoding='utf-8'))
        print(json.dumps(outcome_summary(ledger),ensure_ascii=False,indent=2))


if __name__=='__main__': main()
