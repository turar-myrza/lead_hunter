"""Durable control-center worker. Run separately from the web server."""
import asyncio
from app.core.config import get_settings
from app.services.sales_store import SalesStore
from app.services.control_store import ControlStore
from app.services.control_center import ControlCenter

def recover_interrupted_jobs(base):
    """Requeue only bounded adaptive research; never replay external AI work."""
    with base.connect() as db:
        resumed=db.execute("UPDATE cc_job SET status='queued',error=NULL WHERE status='running' AND kind='discovery'").rowcount
        reviewed=db.execute("UPDATE cc_job SET status='failed',error='worker_interrupted_review_required' WHERE status='running'").rowcount
    return {'resumed_discovery':resumed,'review_required':reviewed}

async def main():
    settings=get_settings();base=SalesStore(settings.lead_hunter_db)
    service=ControlCenter(ControlStore(base),settings)
    with base.worker_lock('control-center') as locked:
        if not locked:raise SystemExit('Control worker already running')
        # An interrupted product-analysis request may have incurred external AI
        # usage, so it still needs review. Discovery is bounded public research
        # with an idempotent adaptive ledger and can resume its existing run.
        recover_interrupted_jobs(base)
        while True:
            if not await service.worker_once():await asyncio.sleep(2)

if __name__=='__main__':asyncio.run(main())
