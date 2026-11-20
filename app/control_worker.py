"""Durable control-center worker. Run separately from the web server."""
import asyncio
from app.core.config import get_settings
from app.services.sales_store import SalesStore
from app.services.control_store import ControlStore
from app.services.control_center import ControlCenter

async def main():
    settings=get_settings();base=SalesStore(settings.lead_hunter_db)
    service=ControlCenter(ControlStore(base),settings)
    with base.worker_lock('control-center') as locked:
        if not locked:raise SystemExit('Control worker already running')
        # An interrupted external request may have incurred usage. Never silently replay it.
        with base.connect() as db:
            db.execute("UPDATE cc_job SET status='failed',error='worker_interrupted_review_required' WHERE status='running'")
        while True:
            if not await service.worker_once():await asyncio.sleep(2)

if __name__=='__main__':asyncio.run(main())
