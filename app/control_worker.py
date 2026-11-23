"""Durable control-center worker. Run separately from the web server."""
import asyncio
from app.core.config import get_settings
from app.services.sales_store import SalesStore
from app.services.control_store import ControlStore
from app.services.control_center import ControlCenter
from app.services.ads_store import AdsStore
from app.services.ads_manager import AdsManager


async def main():
    settings = get_settings()
    base = SalesStore(settings.lead_hunter_db)
    ctrl_store = ControlStore(base)
    service = ControlCenter(ctrl_store, settings)
    ads = AdsManager(AdsStore(base), settings)
    with base.worker_lock('control-center') as locked:
        if not locked:
            raise SystemExit('Control worker already running')
        # An interrupted external request may have incurred usage. Never silently replay it.
        with base.connect() as db:
            db.execute("UPDATE cc_job SET status='failed',error='worker_interrupted_review_required' WHERE status='running'")
            db.execute("UPDATE am_dial_queue SET status='failed',result='worker_interrupted' WHERE status='running'")
        while True:
            did = await service.worker_once()
            did2 = False
            try:
                did2 = await ads.worker_tick()
            except Exception as exc:
                ctrl_store.audit('am_worker_error', str(exc)[:300])
            if not (did or did2):
                await asyncio.sleep(2)


if __name__ == '__main__':
    asyncio.run(main())
