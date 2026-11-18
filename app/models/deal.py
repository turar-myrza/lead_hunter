"""Public response models for the latest-deals endpoint."""

from datetime import datetime
from typing import List

from pydantic import BaseModel, ConfigDict, Field


class DealSummary(BaseModel):
    """The five deal fields required by the first test scenario."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(description="Bitrix24 deal ID")
    title: str = Field(description="Deal title")
    stage: str = Field(description="Readable stage name, with ID as fallback")
    responsible: str = Field(description="Responsible user's name, with ID as fallback")
    updated_at: datetime = Field(description="Last modification time reported by Bitrix24")


class LatestDealsResponse(BaseModel):
    """Envelope that also explains a valid empty result."""

    model_config = ConfigDict(extra="forbid")

    count: int
    message: str
    deals: List[DealSummary]
