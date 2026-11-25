"""Typed, reviewable state for the adaptive LeadHunter decision layer.

The records in this module deliberately capture *decisions and evidence*, not
hidden model reasoning. They are shared by the API, CLI and control panel and
are safe to persist in the local run ledger.
"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1800)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
Key = Annotated[str, StringConstraints(pattern=r'^[a-z][a-z0-9_-]{0,79}$')]


class AdaptiveRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CampaignBudget(AdaptiveRecord):
    """Bounded internal-research budget; it never authorizes external action."""
    max_attempts: int = Field(default=3, ge=1, le=12)
    max_public_requests: int = Field(default=100, ge=0, le=500)
    max_seconds: int = Field(default=180, ge=1, le=3600)
    max_external_cost: float = Field(default=0, ge=0)


class CampaignAuthorization(AdaptiveRecord):
    public_research: bool = True
    crm_read_only: bool = False
    external_messages: bool = False
    outbound_calls: bool = False
    crm_writes: bool = False


class AdaptiveCampaignRequest(AdaptiveRecord):
    """Optional campaign goal. Omission preserves the legacy one-pass path."""
    enabled: bool = True
    ultimate_objective: Text | None = None
    current_objective: Text | None = None
    success_criteria: list[Text] = Field(default_factory=lambda: [
        'Retain only evidence-backed opportunities or public market signals suitable for review.'
    ], max_length=8)
    known_information: list[Text] = Field(default_factory=list, max_length=12)
    unknowns: list[Text] = Field(default_factory=list, max_length=12)
    budget: CampaignBudget = Field(default_factory=CampaignBudget)
    authorization: CampaignAuthorization = Field(default_factory=CampaignAuthorization)
    stop_conditions: list[Text] = Field(default_factory=lambda: [
        'The candidate target is reached.',
        'All feasible implemented strategies are exhausted.',
        'The configured research budget is exhausted.',
    ], max_length=12)
    allow_alternative_sources: bool = True


class CampaignGoal(AdaptiveRecord):
    ultimate_objective: Text
    current_objective: Text
    success_criteria: list[Text]
    resources: list[ShortText]
    known_information: list[Text]
    unknowns: list[Text]
    budget: CampaignBudget
    authorization: CampaignAuthorization
    stop_conditions: list[Text]


class EvidenceLink(AdaptiveRecord):
    candidate_id: Key | None = None
    evidence_id: Key | None = None
    relation: Literal['required', 'supports', 'contradicts', 'missing']
    note: Text


class CampaignHypothesis(AdaptiveRecord):
    id: Key
    revision: int = Field(default=1, ge=1)
    buyer_path: Literal['organization', 'individual']
    segment: Text
    expected_problem: Text
    purchase_motivation: Text
    observable_signals: list[Text] = Field(min_length=1, max_length=8)
    supporting_evidence_requirements: list[Text] = Field(min_length=1, max_length=8)
    contradicting_evidence_requirements: list[Text] = Field(min_length=1, max_length=8)
    alternative_verification: list[Text] = Field(min_length=1, max_length=8)
    evidence: list[EvidenceLink] = Field(default_factory=list, max_length=40)
    confidence: float = Field(default=0.35, ge=0, le=1)
    state: Literal['active', 'supported', 'insufficient_evidence', 'contradicted', 'superseded'] = 'active'
    next_action: Text
    change_reason: Text = 'Initial product-derived hypothesis.'


class AdaptiveStrategy(AdaptiveRecord):
    id: Key
    hypothesis_id: Key
    capability_id: Key
    objective: Text
    expected_result: Text
    expected_value: float = Field(ge=0, le=1)
    cost: float = Field(default=0, ge=0)
    reversibility: Literal['reversible', 'review_required'] = 'reversible'
    status: Literal['planned', 'selected', 'attempted', 'skipped', 'blocked'] = 'planned'
    recovery_options: list[Text] = Field(default_factory=list, max_length=6)


class FailureRecord(AdaptiveRecord):
    id: Key
    classification: Literal[
        'temporary_technical', 'unavailable_source', 'unsupported_adapter',
        'insufficient_evidence', 'incorrect_hypothesis', 'ineffective_strategy',
        'missing_permission', 'budget_exhausted',
    ]
    source_id: Key | None = None
    summary: Text
    recovery_action: Text
    retry_allowed: bool = False


class ActionEvaluation(AdaptiveRecord):
    expected_result: Text
    actual_result: Text
    evidence_quality: Literal['none', 'limited', 'verifiable']
    alternative_explanations: list[Text] = Field(default_factory=list, max_length=6)
    next_action: Text


class ActionAttempt(AdaptiveRecord):
    sequence: int = Field(ge=1)
    strategy_id: Key
    capability_id: Key
    status: Literal['planned', 'running', 'succeeded', 'partial', 'failed', 'blocked', 'stopped']
    expected_result: Text
    actual_result: str = Field(default='', max_length=1800)
    candidate_ids: list[Key] = Field(default_factory=list, max_length=100)
    failures: list[FailureRecord] = Field(default_factory=list, max_length=8)
    evaluation: ActionEvaluation | None = None
    elapsed_ms: int = Field(default=0, ge=0)
    reused_after_restart: bool = False
    # Only public, already-verified candidate payloads are checkpointed here.
    # It lets a worker resume without repeating a completed source request.
    verified_candidates: list[dict] = Field(default_factory=list, max_length=100)


class ExperienceItem(AdaptiveRecord):
    id: Key
    category: Literal['internal_success', 'internal_unsuccessful', 'external_success', 'external_unsuccessful']
    provenance: Text
    applicability: Text
    lesson: Text
    confidence: float = Field(ge=0, le=1)
    supporting_outcomes: list[Text] = Field(default_factory=list, max_length=8)
    reviewed: bool = False


class AdaptiveRunView(AdaptiveRecord):
    run_id: int
    status: Literal['planned', 'running', 'completed', 'partial', 'stopped', 'failed']
    goal: CampaignGoal
    hypotheses: list[CampaignHypothesis]
    strategies: list[AdaptiveStrategy]
    attempts: list[ActionAttempt]
    experience: list[ExperienceItem]
    lessons: list[Text]
    journal: list[dict] = Field(default_factory=list, max_length=500)
    stop_reason: Text | None = None
