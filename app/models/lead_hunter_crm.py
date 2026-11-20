"""Local CRM duplicate results. Never serialize credentials or the full CRM index."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CRMRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entity: Literal['company', 'contact', 'lead', 'deal']
    id: int = Field(gt=0)
    name: str
    company_id: int | None = None
    assigned_by_id: int | None = None
    status_id: str | None = None
    phones: list[str] = Field(default_factory=list)
    websites: list[str] = Field(default_factory=list)
    company_title: str = ''
    category_id: int = 0


class CRMMatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entity: str
    id: int
    name: str
    confidence: Literal['confirmed', 'possible']
    reasons: list[str]
    responsible_id: int | None = None
    responsible_name: str | None = None
    status: str | None = None
    history: list[dict] = Field(default_factory=list)
    related_deals: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CRMCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    state: Literal['not_checked', 'unavailable', 'incomplete', 'confirmed_duplicate', 'possible_match', 'no_match_accessible'] = 'not_checked'
    checked_at: datetime | None = None
    coverage: dict[str, bool] = Field(default_factory=dict)
    scanned: dict[str, int] = Field(default_factory=dict)
    matches: list[CRMMatch] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
