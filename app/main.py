"""FastAPI application entry point."""

from fastapi import FastAPI

from app.api.analytics import router as analytics_router
from app.api.deals import router as deals_router
from app.api.open_channels import router as open_channels_router
from app.api.control_center import router as control_center_router
from app.api.ads_manager import router as ads_manager_router
from app.api.universal_lead_hunter import router as lead_hunter_router
from app.core.config import get_settings
from version import VERSION


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description=(
        "Read-only Bitrix24 analytics with a concise Executive Report by default."
    ),
    version=VERSION,
)
app.include_router(deals_router)
app.include_router(analytics_router)
app.include_router(lead_hunter_router)
app.include_router(open_channels_router)
app.include_router(control_center_router)
app.include_router(ads_manager_router)


@app.middleware('http')
async def control_security_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/platform'):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.get("/", tags=["Project"])
async def project_info() -> dict[str, str]:
    """Point a new user to the test endpoint and interactive documentation."""

    return {
        "project": settings.app_name,
        "mode": "Bitrix24 read-only",
        "latest_deals": "/api/v1/deals/latest",
        "executive_report": "/api/v1/analytics/deals/attention",
        "deal_attention_details": "/api/v1/analytics/deals/attention/details",
        "documentation": "/docs",
    }
