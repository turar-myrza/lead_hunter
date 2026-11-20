"""Validation shared by the local Outreach brief and public-only AI drafts."""
import re
from typing import Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RecommendedChannel = Literal['whatsapp', 'email', 'phone']


def _normalize(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е').split())


_RESEARCH_LANGUAGE = re.compile(
    r'\b(?:увидел[аи]?|заметил[аи]?|выяснил[аи]?|узнал[аи]?|обнаружил[аи]?|'
    r'ознакомил(?:ся|ась|ись)|посмотрел[аи]?|изучил[аи]?|проанализировал[аи]?|'
    r'проверил[аи]?|прочитал[аи]?)\b.{0,70}'
    r'\b(?:ваш\w*|ваканси\w*|сайт\w*|что\s+вы|процесс\w*|компани\w*)\b'
    r'|\b(?:по|из|согласно|судя\s+по)\s+(?:\w+\s+){0,3}'
    r'(?:ваканси\w*|публикаци\w*|отзыв\w*|(?:открыт\w*|публичн\w*)\s+источник\w*)\b'
    r'|\b(?:на\s+ваш\w*\s+сайт\w*|у\s+вас\s+на\s+сайт\w*|на\s+сайт\w*)[\s:—–-]+'
    r'(?:\w+\s+){0,3}(?:указан\w*|написан\w*|описан\w*|опубликован\w*|видно|сказан\w*|упомина\w*)\b'
    r'|\b(?:нам\s+(?:стало\s+)?известно|по\s+нашим\s+наблюдениям)\b'
    r'|\b(?:ваканси\w*|(?:коммерческ\w*|внутренн\w*)\s+разведк\w*|'
    r'истори\w*\s+отношени\w*|подтвержденн\w*\s+сигнал\w*|'
    r'ссылк\w*\s+на\s+источник\w*|fact_refs|source_id)\b'
    r'|(?:https?\s*://|\bwww\.|\[[^\]]+\]\()'
)
_PAIN_ENTITY = r'(?:заявк\w*|звонк\w*|обращени\w*|заказ\w*|клиент\w*)'
_PAIN_VERB = r'(?:теря\w*|пропада\w*|завис\w*|дублиру\w*)'
_ASSERTED_PAIN = re.compile(
    r'\b(?:вы|ваш\w*\s+(?:отдел\w*|менеджер\w*|команд\w*|компани\w*))\s+'
    r'(?:\w+\s+){0,3}(?:теря\w*|упуска\w*|пропуска\w*|не\s+успева\w*)\b'
    r'|\b(?:у\s+вас\s+(?:\w+\s+){0,4}|ваш\w*\s+)'
    rf'{_PAIN_ENTITY}\s+(?:\w+\s+){{0,3}}{_PAIN_VERB}\b'
    rf'|\bу\s+вас\s+(?:\w+\s+){{0,4}}{_PAIN_VERB}\s+(?:\w+\s+){{0,3}}{_PAIN_ENTITY}\b'
    r'|\bу\s+вас\s+(?:уже\s+|есть\s+)?(?:проблем\w*|потери|сбои|хаос)\b'
    r'|\b(?:ваши\s+потери|решить\s+вашу\s+проблему|исправить\s+ваши\s+ошибки)\b'
)
_CONDITIONAL = re.compile(r'\b(?:если|проверить,?\s+теря|выяснить,?\s+теря)\b')
_CTA_ACTIONS = re.compile(
    r'\b(?:ответьте|напишите|позвоните|оставьте|запишитесь|перейдите|нажмите|'
    r'отправьте|пришлите|сообщите|назначьте|забронируйте|свяжитесь|выберите|'
    r'согласуйте|зарегистрируйтесь|посмотрите|скачайте|подтвердите)\b'
)


def validate_customer_facing_text(text: str) -> str:
    value = _normalize(text)
    if _RESEARCH_LANGUAGE.search(value):
        raise ValueError('Customer-facing text must keep research and sources internal')
    for sentence in re.split(r'[.!?\n]+', value):
        problem = _ASSERTED_PAIN.search(sentence)
        if problem and not _CONDITIONAL.search(sentence[:problem.start()].rsplit(',', 1)[-1]):
            raise ValueError('Customer-facing text must not assert an unverified client problem')
    return text


def validate_no_verbatim_research(texts, quotes) -> None:
    """Catch pasted excerpts without banning short, ordinary industry phrases."""
    external = [_normalize(text) for text in texts]
    for quote in quotes:
        excerpt = _normalize(quote).strip(' «»"\'.,;:!?')
        if len(excerpt) >= 28 and len(excerpt.split()) >= 5 and any(excerpt in text for text in external):
            raise ValueError('Customer-facing text must not reproduce research excerpts')


def validate_single_cta(text: str, cta: str) -> None:
    normalized = _normalize(text)
    action = _normalize(cta)
    opening = normalized[:-len(action)].rstrip()
    if (normalized.count(action) != 1 or not normalized.endswith(action)
            or normalized.count('?') > 1 or _CTA_ACTIONS.search(opening)
            or len(_CTA_ACTIONS.findall(action)) > 1):
        raise ValueError('Customer-facing text must end with exactly one supplied CTA')


class ManagerOutreachCopy(BaseModel):
    """External copy only. Evidence, offer rationale and contact history stay in the dossier."""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, frozen=True,
                              hide_input_in_errors=True)

    whatsapp_text: str = Field(min_length=220, max_length=650)
    email_subject: str = Field(min_length=8, max_length=90)
    email_text: str = Field(min_length=100, max_length=950)
    cta: str = Field(min_length=8, max_length=150)
    recommended_channel: RecommendedChannel
    _external_text = field_validator('whatsapp_text', 'email_subject', 'email_text', 'cta')(
        validate_customer_facing_text
    )

    @model_validator(mode='after')
    def separate_channel_copy(self):
        if '\n' in self.email_subject or '\r' in self.email_subject:
            raise ValueError('Email subject must be one line')
        if _normalize(self.whatsapp_text) == _normalize(self.email_text):
            raise ValueError('Write separate WhatsApp and email copy')
        for text in (self.whatsapp_text, self.email_text):
            validate_single_cta(text, self.cta)
        return self
