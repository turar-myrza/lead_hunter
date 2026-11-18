"""FastAPI application entry point."""

from fastapi import FastAPI

from app.api.analytics import router as analytics_router
from app.api.deals import router as deals_router
from app.core.config import get_settings


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description=(
        "Read-only Bitrix24 analytics with a concise Executive Report by default."
    ),
    version="0.2.0",
)
app.include_router(deals_router)
app.include_router(analytics_router)


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
