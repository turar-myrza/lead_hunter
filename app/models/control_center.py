"""Product control-center contracts; no product-specific audience defaults."""
from typing import Annotated, Literal
from pydantic import Field, StringConstraints, field_validator
from app.models.universal_lead_hunter import Record, ProductBrief, BuyerRole
from app.integrations.lead_hunter_public import public_https_url

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]

class ProductProfile(Record):
    brief: ProductBrief
    pricing: str = Field(default='', max_length=1500)
    advantages: list[Text] = Field(default_factory=list, max_length=12)
    customer_problems: list[Text] = Field(default_factory=list, max_length=12)
    references: list[str] = Field(default_factory=list, max_length=12)
    materials: str = Field(default='', max_length=12000)
    public_analysis_allowed: bool = False

    @field_validator('references')
    @classmethod
    def references_public(cls, values):
        return [public_https_url(value) for value in values]

class Segment(Record):
    name: Text
    path: Literal['organization', 'individual']
    characteristics: Text
    roles: list[Text] = Field(min_length=1, max_length=10)
    motivations: list[Text] = Field(min_length=1, max_length=6)
    signals: list[Text] = Field(min_length=1, max_length=8)
    queries: list[Annotated[str, StringConstraints(min_length=3, max_length=200)]] = Field(min_length=1, max_length=4)
    criteria: list[Text] = Field(min_length=1, max_length=8)
    source_urls: list[str] = Field(default_factory=list, max_length=20)
    @field_validator('source_urls')
    @classmethod
    def urls(cls, values):
        return [public_https_url(value) for value in values]

class ProductIntelligence(Record):
    value_proposition: Text
    problems_solved: list[Text] = Field(min_length=1, max_length=8)
    use_cases: list[Text] = Field(min_length=1, max_length=8)
    purchase_barriers: list[Text] = Field(min_length=1, max_length=8)
    missing_information: list[Text] = Field(min_length=1, max_length=12)
    segments: list[Segment] = Field(min_length=2, max_length=6)
    # The application labels every generated assertion; the model cannot grant verification.

class Review(Record):
    ethics_passed: bool = False
    state: Literal['needs_review', 'qualified', 'excluded']
    rationale: str = Field(min_length=12, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    next_action: str = Field(min_length=5, max_length=1000)

class CampaignInput(Record):
    segment_id: int = Field(gt=0)
    target: int = Field(default=20, ge=1, le=50)

class SegmentApproval(Record):
    segment: Segment
    approved: bool

class ExtractedOrganization(Record):
    name: str = Field(min_length=2, max_length=160)
    industry: Text
    location: Text
    identity_quote: Text
    location_quote: Text
    need_quote: str = Field(default='', max_length=1200)
    role_quote: str = Field(default='', max_length=1200)
    role_actor: str = Field(default='', max_length=160)
    role_kind: Literal['procurement', 'technical_specialist', 'department_head', 'owner', 'decision_maker', 'user'] = 'decision_maker'

class Extraction(Record):
    organizations: list[ExtractedOrganization] = Field(default_factory=list, max_length=5)
