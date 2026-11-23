"""Pydantic request and rule models for the Ads-Manager-style cockpit."""

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.catalog import CATALOG_KEYS

VERSION = "2.9.1"

KATO_MAP = {
    "almaty": "750000000",
    "astana": "710000000",
    "shymkent": "790000000",
    "karaganda": "350000000",
    "aktobe": "150000000",
    "atyrau": "230000000",
}

CITY_ALIASES = {
    "almaty": ("almaty", "алматы"),
    "astana": ("astana", "астана", "нур-султан", "нұр-сұлтан"),
    "shymkent": ("shymkent", "шымкент"),
    "karaganda": ("karaganda", "караганда", "қарағанды"),
    "aktobe": ("aktobe", "актобе", "ақтөбе"),
    "atyrau": ("atyrau", "атырау"),
}

RegionCode = Literal["almaty", "astana", "shymkent", "karaganda", "aktobe", "atyrau", "all"]
SourceCode = Literal[
    "csv_seed",
    "goszakup",
    "manual_import",
    "satu_kz",
    "uchet_pk",
    "twogis",
    "egov_open_data",
    "statsnet",
    "e_ondiris",
]
SegmentCode = Literal["b2b", "b2g", "b2c"]
ObjectiveCode = Literal["call_then_offer", "offer_then_call", "call_only"]
LangCode = Literal["kz", "ru"]


class AudienceRules(BaseModel):
    """Visual filter rules controlling discovery sources and ICP scoring."""

    model_config = ConfigDict(extra="forbid")

    regions: list[RegionCode] = Field(default_factory=lambda: ["all"], min_length=1, max_length=7)
    keywords: list[str] = Field(default_factory=list, max_length=10)
    min_contract_kzt: Optional[float] = Field(default=None, ge=0)
    max_contract_kzt: Optional[float] = Field(default=None, ge=0)
    year: int = Field(default=2026, ge=2020, le=2030)
    sources: list[SourceCode] = Field(default_factory=lambda: ["csv_seed"], min_length=1, max_length=9)
    require_phone: bool = True
    manual_rows: Optional[str] = Field(default=None, max_length=204800)

    @field_validator("regions", "sources")
    @classmethod
    def deduplicate_list(cls, values: list) -> list:
        seen = []
        for val in values:
            if val not in seen:
                seen.append(val)
        return seen

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, values: list[str]) -> list[str]:
        cleaned = []
        for raw in values:
            item = raw.strip()
            if not (2 <= len(item) <= 60):
                raise ValueError("Each keyword must be 2 to 60 characters")
            if item not in cleaned:
                cleaned.append(item)
        if len(cleaned) > 10:
            raise ValueError("Maximum 10 keywords allowed")
        return cleaned

    @model_validator(mode="after")
    def validate_bounds(self):
        if (
            self.min_contract_kzt is not None
            and self.max_contract_kzt is not None
            and self.max_contract_kzt < self.min_contract_kzt
        ):
            raise ValueError("max_contract_kzt must be greater than or equal to min_contract_kzt")
        return self


class CampaignIn(BaseModel):
    """Campaign creation/update payload."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=120)
    product_key: str
    objective: ObjectiveCode = "call_then_offer"
    lang: LangCode = "kz"

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("Campaign name must be at least 2 characters")
        return cleaned

    @field_validator("product_key")
    @classmethod
    def check_product(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned or len(cleaned) < 2 or len(cleaned) > 80:
            raise ValueError(f"Invalid product_key '{value}'")
        return cleaned


class AudienceIn(BaseModel):
    """Audience rule-set creation/update payload."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: int = Field(ge=1)
    name: str = Field(min_length=2, max_length=120)
    segment: SegmentCode = "b2b"
    rules: AudienceRules = Field(default_factory=AudienceRules)
    auto_discovery: bool = True

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("Audience name must be at least 2 characters")
        return cleaned

    @model_validator(mode="after")
    def enforce_b2c_policy(self):
        if self.segment == "b2c" and self.rules.sources != ["manual_import"]:
            raise ValueError("B2C audiences only support manual_import source (individual web scraping disabled)")
        return self


class QueueIn(BaseModel):
    """Batch queue request payload."""

    model_config = ConfigDict(extra="forbid")

    lead_ids: list[int] = Field(min_length=1, max_length=100)
    lang: Optional[LangCode] = None
    gap_seconds: int = Field(default=90, ge=30, le=600)


class ToggleIn(BaseModel):
    """Row enabled/active toggle payload."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


LeadStatusCode = Literal[
    "new",
    "queued",
    "called",
    "offer_sent",
    "qualified",
    "callback",
    "rejected",
    "blocked_whitelist",
    "deferred_cap",
    "failed",
]


class LeadUpdateIn(BaseModel):
    """Lead edit payload for updating company, contact, BIN, phone, ICP, and status."""

    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1, max_length=200)
    bin: str = Field(default="", max_length=24)
    city: str = Field(default="", max_length=120)
    category: str = Field(default="", max_length=200)
    service: str = Field(default="", max_length=200)
    director: str = Field(default="", max_length=160)
    phone_raw: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=160)
    contract_sum_kzt: Optional[float] = Field(default=None, ge=0)
    icp_score: Optional[int] = Field(default=None, ge=0, le=100)
    status: Optional[LeadStatusCode] = None
    source_url: str = Field(default="", max_length=500)
    enabled: Optional[bool] = None

    @field_validator("company")
    @classmethod
    def strip_company(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Company name cannot be empty")
        return cleaned
