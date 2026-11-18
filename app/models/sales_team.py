"""Typed local envelopes. Public evidence and relationship state stay separate."""
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.lead_hunter import public_url


class PublicSignal(BaseModel):
    model_config = ConfigDict(extra='allow', str_strip_whitespace=True)
    url: str
    fact: str = Field(min_length=8, max_length=1400)
    published: str | None = None
    observed_at: date | None = None
    evidence_type: str = 'public'
    _url = field_validator('url')(public_url)


class Prospect(BaseModel):
    model_config = ConfigDict(extra='ignore', str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=160)
    city: str = Field(min_length=2, max_length=240)
    industry: str = Field(min_length=3, max_length=250)
    site: str | None = None
    aliases: list[str] = Field(default_factory=list, max_length=30)
    phones: list[str] = Field(default_factory=list, max_length=15)
    emails: list[str] = Field(default_factory=list, max_length=15)
    sources: list[PublicSignal] = Field(min_length=1, max_length=15)
    product: Literal['A','B','C']
    crm_status: str = Field(default='Не установлена')
    fact: str = Field(min_length=8, max_length=1500)
    hypothesis: str = Field(min_length=8, max_length=1000)
    offer: str = Field(min_length=8, max_length=900)
    next_step: str = Field(min_length=8, max_length=900)
    process: str = Field(default='', max_length=250)
    visual_steps: list[str] = Field(default_factory=list, max_length=6)
    outcome: str = Field(default='', max_length=500)
    language: Literal['ru','kk'] = 'ru'
    ethical: str = 'не проверено'
    watch: str = ''
    contact_status: str = 'research_only'

    @field_validator('site')
    @classmethod
    def validate_site(cls, value):
        return public_url(value) if value else None


class Outcome(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    event_id: str = Field(min_length=3, max_length=120)
    run_id: int = Field(gt=0)
    company: str = Field(min_length=2, max_length=160)
    kind: str
    at: datetime
    variant: Literal['email_text','email_visual','voice','human','unknown'] = 'unknown'
    language: Literal['ru','kk','unknown'] = 'unknown'
    segment: Literal['A','B','C','unknown'] = 'unknown'

    @field_validator('at')
    @classmethod
    def observed_time(cls, value):
        if value.tzinfo is None or value > datetime.now(value.tzinfo):
            raise ValueError('At must be an observed time with a time zone')
        return value
