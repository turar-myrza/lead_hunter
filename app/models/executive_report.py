"""Generic public models for concise management reports."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ExecutivePriority(str, Enum):
    """Management urgency, independent from a particular CRM scenario."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def label(self) -> str:
        """Return the human-readable priority used in the report UI."""

        return {
            self.CRITICAL: "🚨 КРИТИЧЕСКИЙ",
            self.HIGH: "🔴 ВЫСОКИЙ",
            self.MEDIUM: "🟡 СРЕДНИЙ",
            self.LOW: "🟢 НИЗКИЙ",
        }[self]


class EvidenceLevel(str, Enum):
    """How strongly CRM data supports a statement."""

    FACT = "fact"
    RISK = "risk"
    ASSUMPTION = "assumption"

    @property
    def label(self) -> str:
        """Return the Russian evidence marker shown to management."""

        return {
            self.FACT: "ФАКТ",
            self.RISK: "РИСК",
            self.ASSUMPTION: "ПРЕДПОЛОЖЕНИЕ",
        }[self]


class ExecutiveAttentionItem(BaseModel):
    """One material object that deserves management attention."""

    model_config = ConfigDict(extra="forbid")

    deal_id: int = Field(gt=0)
    client_or_deal: str = Field(min_length=1)
    responsible: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    risk_score: int = Field(ge=0, le=165)
    priority: ExecutivePriority
    priority_label: str = Field(min_length=1)
    why_attention: str = Field(min_length=1)
    recommended_action: str = Field(min_length=1)
    evidence_level: EvidenceLevel
    evidence_label: str = Field(min_length=1)


class SystemIssue(BaseModel):
    """A repeated signal, not an isolated issue in one CRM card."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    evidence_level: EvidenceLevel
    evidence_label: str = Field(min_length=1)
    affected_count: int = Field(ge=2)


class ExecutiveStatistics(BaseModel):
    """Only the scale indicators needed for a management decision."""

    model_config = ConfigDict(extra="forbid")

    total_open_deals: int = Field(ge=0)
    requiring_attention: int = Field(ge=0)
    critical_priority: int = Field(ge=0)
    high_priority: int = Field(ge=0)
    medium_priority: int = Field(ge=0)
    low_priority: int = Field(ge=0)
    without_detected_risks: int = Field(ge=0)


class ExecutiveReport(BaseModel):
    """Progressively disclosed management view of any analytical result."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    main_conclusion: list[str] = Field(min_length=2, max_length=4)
    attention_items: list[ExecutiveAttentionItem] = Field(max_length=10)
    actions_today: list[str] = Field(max_length=5)
    systemic_issues: list[SystemIssue] = Field(max_length=3)
    statistics: ExecutiveStatistics
    data_limitations: list[str] = Field(max_length=5)
    details_available: str = Field(min_length=1)
