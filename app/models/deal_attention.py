"""Public response models for the deal-attention scenario."""

from datetime import datetime
from enum import Enum
from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field


class RiskReasonCode(str, Enum):
    """Stable machine-readable identifiers for the seven risk checks."""

    NO_ACTIVITY = "no_activity"
    NO_NEXT_ACTION = "no_next_action"
    OVERDUE_TASK = "overdue_task"
    LONG_IN_STAGE = "long_in_stage"
    NO_REACTION_AFTER_PROPOSAL = "no_reaction_after_proposal"
    OVERDUE_CLOSE_DATE = "overdue_close_date"
    NO_RESPONSIBLE = "no_responsible"


class RiskReason(BaseModel):
    """One triggered risk rule with its contribution to the score."""

    model_config = ConfigDict(extra="forbid")

    code: RiskReasonCode
    label: str
    weight: int = Field(ge=0)
    details: str


class DealAttentionItem(BaseModel):
    """Management-focused view of one risky open deal."""

    model_config = ConfigDict(extra="forbid")

    id: int
    title: str
    responsible: str
    stage: str
    risk_score: int = Field(ge=0, le=165)
    reasons: List[RiskReason]
    recommendations: List[str]


class DealAttentionStatistics(BaseModel):
    """Aggregate statistics for all open deals, including zero-risk deals."""

    model_config = ConfigDict(extra="forbid")

    total_open_deals: int = Field(ge=0)
    requiring_attention: int = Field(ge=0)
    without_detected_risks: int = Field(ge=0)
    average_risk_score: float = Field(ge=0)
    maximum_risk_score: int = Field(ge=0, le=165)
    by_reason: Dict[str, int]


class DealAttentionResponse(BaseModel):
    """Sorted attention list plus whole-pipeline statistics and caveats."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    deals: List[DealAttentionItem]
    statistics: DealAttentionStatistics
    warnings: List[str]
