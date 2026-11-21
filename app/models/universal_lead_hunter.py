"""Shared input, opportunity, and run contracts for the API and CLI."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator

PathKind = Literal['organization', 'individual']
Channel = Literal['email', 'sms', 'sip', 'inbound_reply', 'manager_review']
Role = Literal['user', 'decision_maker', 'payer', 'influencer', 'procurement', 'technical_specialist', 'department_head', 'owner', 'caregiver']
Stage = Literal['understand', 'plan_sources', 'discover', 'verify', 'crm_review', 'qualify', 'engagement', 'export']
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2400)]
Key = Annotated[str, StringConstraints(pattern=r'^[a-z][a-z0-9_-]{0,63}$')]


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ProductBrief(Record):
    name: Short
    description: Description
    category: Short
    problem_hint: Description | None = None
    buyer_paths: set[PathKind] = Field(default_factory=lambda: {'organization', 'individual'}, min_length=1)
    market: Short


class BuyerRole(Record):
    role: Role
    actor: Short
    evidence_ids: list[Key] = Field(default_factory=list, max_length=12)


class BuyerPath(Record):
    kind: PathKind
    roles: list[BuyerRole]
    unresolved_questions: list[str]


class Evidence(Record):
    id: Key
    kind: Literal['identity', 'need', 'market_signal', 'role', 'business_contact', 'inbound_request', 'opt_in', 'opt_out']
    excerpt: Description
    url: HttpUrl | None = None
    observed_at: AwareDatetime | None = None
    observed_on: date | None = None
    published_on: date | None = None
    basis: Literal['supplied', 'synthetic', 'public_page'] = 'supplied'
    channel: Channel | None = None


class Candidate(Record):
    id: Key
    label: Short
    path: PathKind
    # A public demand signal represents a topic/channel page, never an
    # identifiable person or permission to contact one.
    subject_kind: Literal['buyer', 'public_demand_signal'] = 'buyer'
    evidence: list[Evidence] = Field(default_factory=list, max_length=40)
    roles: list[BuyerRole] = Field(default_factory=list, max_length=18)
    industry: str | None = Field(default=None, max_length=1200)
    location: str | None = Field(default=None, max_length=1200)

    @model_validator(mode='after')
    def validate_citations(self):
        items = {item.id: item for item in self.evidence}
        if len(items) != len(self.evidence):
            raise ValueError('Evidence IDs must be unique inside a candidate')
        for role in self.roles:
            for ref in role.evidence_ids:
                if ref not in items or items[ref].kind != 'role':
                    raise ValueError('Role citations must reference role evidence on this candidate')
                excerpt = ' '.join(items[ref].excerpt.casefold().split())
                if ' '.join(role.actor.casefold().split()) not in excerpt:
                    raise ValueError('The cited evidence must contain the role actor')
        return self


class RunRequest(Record):
    brief: ProductBrief
    owner: Short = 'LeadHunter review queue'
    mode: Literal['fixture', 'research'] = 'research'
    source_ids: list[Key] = Field(default_factory=list, max_length=8)
    # Select an existing research profile explicitly. Its CRM heuristics only apply to this context.
    research_context: Literal['general', 'crm_sales_operations'] = 'general'
    crm_read_only: bool = False
    limit: int = Field(default=3, ge=1, le=50)
    discovery_queries: list[Annotated[str, StringConstraints(min_length=3, max_length=200)]] = Field(default_factory=list, max_length=4)
    discovery_urls: list[str] = Field(default_factory=list, max_length=20)
    supplied_candidates: list[Candidate] = Field(default_factory=list, max_length=15)

    @model_validator(mode='after')
    def validate_input(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError('Source IDs must be unique')
        if self.mode == 'fixture' and self.crm_read_only:
            raise ValueError('Fixture runs cannot access CRM')
        ids = {item.id for item in self.supplied_candidates}
        if len(ids) != len(self.supplied_candidates):
            raise ValueError('Candidate IDs must be unique')
        for candidate in self.supplied_candidates:
            if candidate.path not in self.brief.buyer_paths:
                raise ValueError('Candidate path must be in the brief')
            if any(item.basis != 'supplied' for item in candidate.evidence):
                raise ValueError('Only adapters can assign evidence provenance')
        return self


class SourceCapability(Record):
    id: Key
    status: Literal['implemented', 'requires_access', 'unavailable']
    access: str
    paths: list[PathKind]
    modes: list[Literal['fixture', 'research']] = Field(default_factory=list)
    context: Literal['general', 'crm_sales_operations'] = 'general'
    markets: list[str] = Field(default_factory=list)


class SourcePlan(Record):
    capability: SourceCapability
    path: PathKind
    objective: str
    enabled: bool
    reason: str


class Qualification(Record):
    state: Literal['qualified', 'needs_review', 'excluded']
    reasons: list[str]
    purchase_interest: Literal['expressed', 'unknown'] = 'unknown'


class InteractionEvent(Record):
    kind: Literal['inbound_request', 'opt_in', 'opt_out']
    evidence_id: Key
    at: datetime | None
    channel: Channel | None
    basis: Literal['supplied', 'synthetic', 'public_page']


class EngagementPlan(Record):
    channel: Channel
    reply_channel: Channel | None = None
    permission: Literal['blocked', 'review_only', 'synthetic_opt_in']
    first_action: str
    follow_up: str


class Opportunity(Record):
    candidate: Candidate
    source_id: Key
    product_category: Short
    crm_relationship: str = 'unknown'
    crm_check: str = 'not_checked'
    crm_owner: str | None = None
    inferred_need: str
    unresolved_questions: list[str]
    qualification: Qualification
    contactability: Literal['unknown', 'public_business_channel', 'reported_inbound_channel', 'synthetic_inbound_channel']
    engagement: EngagementPlan
    interaction_history: list[InteractionEvent]
    owner: Short
    next_action: str


class ActionCapability(Record):
    action: Literal['email', 'sms', 'sip', 'crm_write']
    implemented: Literal[False] = False
    authorized: Literal[False] = False


class StageError(Record):
    stage: Stage
    code: Key
    message: str
    source: Key | None = None


class StageProgress(Record):
    stage: Stage
    status: Literal['pending', 'running', 'completed', 'partial', 'failed']
    detail: str = ''
    errors: list[StageError] = Field(default_factory=list)


class RunStatus(Record):
    run_id: int
    status: Literal['queued', 'running', 'completed', 'partial', 'failed']
    stages: list[StageProgress]


class RunResult(Record):
    run_id: int
    brief: ProductBrief
    mode: Literal['fixture', 'research']
    problem_hypothesis: str
    buyer_paths: list[BuyerPath]
    source_plan: list[SourcePlan]
    opportunities: list[Opportunity]
    action_capabilities: list[ActionCapability]
    artifacts: list[str] = Field(default_factory=list)
