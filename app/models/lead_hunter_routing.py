"""v1.2 routing records. These are local decisions, never CRM commands."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.outreach_copy import ManagerOutreachCopy, validate_no_verbatim_research


class RoutingRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class EvidenceCitation(RoutingRecord):
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=500)
    fact: str = Field(min_length=1)
    scope: Literal['local', 'network'] = 'local'


class CRMUsageAssessment(RoutingRecord):
    status: Literal['unknown', 'bitrix24', 'other', 'present_unspecified', 'none_confirmed'] = 'unknown'
    system_name: str | None = None
    basis: Literal['unknown', 'explicit_usage', 'explicit_absence'] = 'unknown'
    evidence: list[EvidenceCitation] = Field(default_factory=list)
    limitation: str | None = None

    @model_validator(mode='after')
    def require_explicit_evidence(self):
        if self.status != 'unknown' and not self.evidence:
            raise ValueError('A CRM usage conclusion requires explicit source citations')
        if self.status in {'bitrix24', 'other', 'present_unspecified'} and self.basis != 'explicit_usage':
            raise ValueError('Actual CRM use requires an explicit_usage basis')
        if self.status == 'none_confirmed' and self.basis != 'explicit_absence':
            raise ValueError('CRM absence must be explicitly established')
        return self


class CommercialProposal(RoutingRecord):
    product_key: str = Field(min_length=1)
    offer_key: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    concrete_offer: str = Field(min_length=1)
    confirmed_fact: EvidenceCitation
    new_basis: list[EvidenceCitation] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    outreach_copy: ManagerOutreachCopy | None = None

    @model_validator(mode='after')
    def keep_source_text_internal(self):
        if self.outreach_copy:
            copy = self.outreach_copy
            validate_no_verbatim_research(
                (copy.whatsapp_text, copy.email_subject, copy.email_text),
                (citation.quote for citation in (self.confirmed_fact, *self.new_basis)),
            )
        return self


class ScoreItem(RoutingRecord):
    points: int = Field(ge=0)
    max: int = Field(ge=0)
    source_id: str | None = None
    fact: str
    freshness: str


class RoutingDecision(RoutingRecord):
    candidate_name: str
    category: Literal['A', 'B', 'C', 'D']
    category_label: str
    need_profile: Literal['new_crm', 'existing_bitrix24', 'relationship', 'not_scored']
    need_score: int | None = Field(default=None, ge=0, le=100)
    need_breakdown: dict[str, ScoreItem] = Field(default_factory=dict)
    need_rationale: list[str] = Field(default_factory=list)
    offer_prospect_score: int | None = Field(default=None, ge=0, le=100)
    offer_rationale: list[str] = Field(default_factory=list)
    contact_permissibility: Literal['allow_draft', 'needs_review', 'prohibited']
    commercial_priority: Literal['high', 'medium', 'low', 'hold']
    contact_score: int = Field(ge=0, le=100)
    contact_level: str
    purchase_interest: str
    crm_status: str
    crm_source_ids: list[str] = Field(default_factory=list)
    relationship_state: str
    owner_name: str | None = None
    reasons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    active_task: bool = False
    next_step: str
    as_of: datetime

    @model_validator(mode='after')
    def no_active_holds(self):
        if self.active_task and (self.category == 'D' or self.contact_permissibility != 'allow_draft'):
            raise ValueError('A hold/prohibited decision cannot create an active manager recommendation')
        if self.category == 'D' and self.commercial_priority != 'hold':
            raise ValueError('Excluded companies must have hold priority')
        return self
