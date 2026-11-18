"""Voice handoff and admission checks, plus a text-only dialogue simulator."""

from collections.abc import Iterable
from datetime import datetime, timezone
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


Language = Literal['ru', 'kk']
CallChannel = Literal['sip', 'whatsapp']
VoiceStatus = Literal['no_response', 'conversation_open', 'callback_requested',
                      'handoff_requested', 'declined', 'opted_out']


class VoiceDossier(BaseModel):
    """Only facts and customer-facing copy that may be shared with the recipient."""

    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    company: str = Field(min_length=1, max_length=180)
    offer: str = Field(min_length=1, max_length=1000)
    language: Language = 'ru'
    product: str | None = Field(default=None, max_length=180)
    language_variants: dict[Language, str] = Field(default_factory=dict)
    source_urls: list[str] = Field(default_factory=list, max_length=30)

    @field_validator('language_variants')
    @classmethod
    def validate_variants(cls, variants: dict[Language, str]) -> dict[Language, str]:
        if any(not variant.strip() or len(variant) > 1000 for variant in variants.values()):
            raise ValueError('Each language variant must have 1 to 1000 characters')
        return {language: variant.strip() for language, variant in variants.items()}


class VoiceTurn(BaseModel):
    speaker: Literal['agent', 'customer']
    language: Language
    text: str


class VoiceResult(BaseModel):
    transport: Literal['simulated'] = 'simulated'
    status: VoiceStatus
    language: Language
    transcript: list[VoiceTurn]
    opt_out: bool = False
    handoff: bool = False


_COPY = {
    'ru': {
        'opening': 'Здравствуйте! Я голосовой AI-помощник CRM Consulting.',
        'permission': 'Удобно обсудить это сейчас?',
        'opted_out': 'Понял. Отмечу отказ от дальнейших обращений. Всего доброго.',
        'declined': 'Понял, спасибо за ответ. Всего доброго.',
        'callback_requested': 'Понял. Передам менеджеру просьбу связаться позже.',
        'handoff_requested': 'Спасибо. Передам ваш запрос менеджеру, чтобы согласовать следующий шаг.',
        'language_handoff': 'Передам разговор менеджеру, который сможет продолжить на русском. Удобно?',
        'price': 'Стоимость зависит от объёма работ и вашей конфигурации. Хотите, чтобы менеджер уточнил стоимость на вашем примере?',
        'compatibility': 'Совместимость можно проверить на вашем контуре. Хотите, чтобы менеджер уточнил параметры?',
        'interest': 'Можно передать ваш запрос менеджеру CRM Consulting, чтобы согласовать короткий показ?',
        'clarify': 'Что вам было бы полезно уточнить — как это работает, совместимость или стоимость?',
    },
    'kk': {
        'opening': 'Сәлеметсіз бе! Мен CRM Consulting компаниясының жасанды интеллект негізіндегі дауыстық көмекшісімін.',
        'permission': 'Қазір осыны қысқаша талқылауға ыңғайлы ма?',
        'opted_out': 'Түсіндім. Енді хабарласпау туралы өтінішіңізді белгілеймін. Сау болыңыз.',
        'declined': 'Түсіндім, жауабыңызға рақмет. Сау болыңыз.',
        'callback_requested': 'Түсіндім. Кейінірек хабарласу туралы өтінішіңізді менеджерге жеткіземін.',
        'handoff_requested': 'Рақмет. Келесі қадамды келісу үшін өтінішіңізді менеджерге жеткіземін.',
        'language_handoff': 'Әңгімені қазақ тілінде жалғастыра алатын менеджерге өтінішіңізді жеткізейін бе?',
        'price': 'Бағасы жұмыс көлемі мен қолданыстағы жүйеге байланысты. Менеджер жағдайыңызға сай құнын нақтылап берсін бе?',
        'compatibility': 'Үйлесімділікті қолданыстағы жүйеңізде тексеруге болады. Менеджер параметрлерді нақтыласын ба?',
        'interest': 'Қысқа көрсетілімді келісу үшін өтінішіңізді CRM Consulting менеджеріне жеткізейін бе?',
        'clarify': 'Нені нақтылаған пайдалы болар еді — жұмыс тәртібін, үйлесімділікті немесе бағасын ба?',
    },
}

_OPT_OUT = re.compile(
    r'не\s+(?:звоните|перезванивайте|пишите|беспокойте|тревожьте)|'
    r'больше\s+не\s+надо\s+звонить|удалите\s+(?:мой\s+)?номер|'
    r'хабарласпа(?:ңыз|ндар|ңдар)|қоңырау\s+шалма(?:ңыз|ңдар)|'
    r'нөмірімді\s+өшіріңіз',
)
_DECLINE = re.compile(
    r'не\s*(?:интересует|интересно|нужно|нужна|нужен|актуально|хочу)|'
    r'қызық(?:тырмайды|\s+емес)|қажет\s+емес|керек\s+емес|^(?:нет|жоқ)[.!\s]*$',
)
_CALLBACK = re.compile(
    r'перезвоните|позвоните\s+(?:завтра|позже|вечером)|'
    r'(?:кейінірек|ертең)\s+(?:хабарласыңыз|қоңырау\s+шалыңыз)',
)
_HANDOFF = re.compile(
    r'(?:соедините|свяжите)\s+(?:меня\s+)?с\s+(?:менеджером|человеком|сотрудником)|'
    r'передайте\s+(?:мой\s+)?(?:запрос|просьбу)\s+менеджеру|'
    r'(?:согласуем|назначьте|давайте)\s+(?:встречу|демонстрацию|показ)|'
    r'(?:менеджерге\s+қосыңыз|менеджерге\s+жеткізіңіз|кездесу\s+белгілейік)',
)
_AGREE = re.compile(r'^(?:да|согласен|согласна|хорошо|давайте|иә|ия|келістік|жарайды)[.!\s]*$')
_PRICE = re.compile(r'цен[ауеы]|стоимост|сколько\s+стоит|прайс|баға|құны|қанша\s+тұрады')
_COMPATIBILITY = re.compile(r'совместим|поддержива|подключить|үйлесімді|қосуға\s+бола')
_INTEREST = re.compile(r'интересно|расскажите|покажите|хочу\s+(?:узнать|посмотреть)|қызықты|көрсетіңіз')
_HOW = re.compile(r'как\s+(?:это\s+)?работает|қалай\s+жұмыс\s+істейді')
_RU_REQUEST = re.compile(r'по[- ]русски|на\s+русском|орысша|орыс\s+тілінде')
_KK_REQUEST = re.compile(r'по[- ]казахски|на\s+казахском|қазақша|қазақ\s+тілінде')
_KK_LETTERS = re.compile(r'[әғқңөұүһі]')


def _language(text: str, current: Language) -> Language:
    if _RU_REQUEST.search(text):
        return 'ru'
    if _KK_REQUEST.search(text) or _KK_LETTERS.search(text):
        return 'kk'
    # Use unmistakable Russian cues only. Proper nouns do not select a language.
    if re.search(r'\b(?:здравствуйте|скажите|перезвоните|сколько|спасибо)\b', text):
        return 'ru'
    return current


def _offer(dossier: VoiceDossier, language: Language) -> str | None:
    return dossier.language_variants.get(language) or (
        dossier.offer if language == dossier.language else None
    )


def simulate_voice_dialogue(dossier: VoiceDossier, customer_turns: Iterable[str]) -> VoiceResult:
    """Exercise routing on test utterances without audio, network, or CRM operations."""
    language = dossier.language
    opening = f"{_COPY[language]['opening']} {_offer(dossier, language)} {_COPY[language]['permission']}"
    transcript = [VoiceTurn(speaker='agent', language=language, text=opening)]
    status: VoiceStatus = 'no_response'
    requested_handoff = False
    for turn in customer_turns:
        body = turn.strip()
        if not body:
            continue
        normalized = body.casefold().replace('ё', 'е')
        previous_language, language = language, _language(normalized, language)
        transcript.append(VoiceTurn(speaker='customer', language=language, text=body))
        copy = _COPY[language]
        status = 'conversation_open'
        if _OPT_OUT.search(normalized):
            status = 'opted_out'
        elif _DECLINE.search(normalized):
            status = 'declined'
        elif _CALLBACK.search(normalized):
            status = 'callback_requested'
        elif _HANDOFF.search(normalized) or (requested_handoff and _AGREE.fullmatch(normalized)):
            status = 'handoff_requested'

        if status != 'conversation_open':
            reply = copy[status]
        elif _PRICE.search(normalized):
            reply = copy['price']
        elif _COMPATIBILITY.search(normalized):
            reply = copy['compatibility']
        elif (_RU_REQUEST.search(normalized) or _KK_REQUEST.search(normalized) or
              _HOW.search(normalized) or previous_language != language):
            localized = _offer(dossier, language)
            reply = f"{localized} {copy['permission']}" if localized else copy['language_handoff']
        elif _INTEREST.search(normalized) or _AGREE.fullmatch(normalized):
            reply = copy['interest']
        else:
            reply = copy['clarify']

        transcript.append(VoiceTurn(speaker='agent', language=language, text=reply))
        if status != 'conversation_open':
            break
        requested_handoff = reply in {copy['language_handoff'], copy['price'], copy['compatibility'], copy['interest']}

    return VoiceResult(status=status, language=language, transcript=transcript,
                       opt_out=status == 'opted_out',
                       handoff=status in {'handoff_requested', 'callback_requested'})


class CallPolicy(BaseModel):
    """Reviewed policy state supplied by trusted operations records."""

    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)

    campaign_id: str = ''
    enabled: bool = False
    stopped: bool = False
    approved_by: str = ''
    legal_review: str = ''
    operator_review: str = ''
    recording_review: str = ''
    expires_at: AwareDatetime | None = None
    allowed_channels: frozenset[CallChannel] = frozenset()
    allowed_segments: frozenset[str] = frozenset()
    daily_limit: int = Field(default=0, ge=0)
    approved_test_numbers: frozenset[str] = frozenset()


class CallRequest(BaseModel):
    """Recipient's consent is specific to the approved calling channel."""

    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)

    campaign_id: str
    phone: str
    channel: CallChannel
    segment: str
    consent_kind: Literal['unknown', 'public_listing', 'opt_in', 'callback'] = 'unknown'
    consent_channel: CallChannel | None = None
    consent_reference: str = ''
    consent_expires_at: AwareDatetime | None = None
    do_not_contact: bool = False
    calls_today: int = Field(default=0, ge=0)
    recording_requested: bool = False
    test: bool = False


class CallAdmission(BaseModel):
    allowed: bool
    blockers: tuple[str, ...]


def _phone(raw: str) -> str:
    phone = re.sub(r'[ ()-]', '', raw)
    return phone if re.fullmatch(r'\+7[0-9]{10}', phone) else ''


def evaluate_call_admission(policy: CallPolicy, request: CallRequest, *,
                            now: datetime | None = None) -> CallAdmission:
    """Return an admission decision. This function cannot dial or reserve capacity."""
    at = now if now is not None else datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('Call admission time must include an offset')
    phone = _phone(request.phone)
    rules = {
        'campaign_disabled': policy.enabled and not policy.stopped,
        'campaign_mismatch': bool(policy.campaign_id) and request.campaign_id == policy.campaign_id,
        'campaign_unapproved': bool(policy.approved_by),
        'missing_legal_review': bool(policy.legal_review),
        'missing_operator_review': bool(policy.operator_review),
        'campaign_expired': policy.expires_at is not None and at < policy.expires_at,
        'channel_not_allowed': request.channel in policy.allowed_channels,
        'segment_not_allowed': request.segment in policy.allowed_segments,
        'unusable_phone': bool(phone),
        'opted_out': not request.do_not_contact,
        'missing_recipient_consent': request.consent_kind in {'opt_in', 'callback'}
        and bool(request.consent_reference) and request.consent_channel == request.channel
        and request.consent_expires_at is not None and at < request.consent_expires_at,
        'daily_limit_reached': request.calls_today < policy.daily_limit,
        'recording_not_reviewed': not request.recording_requested or bool(policy.recording_review),
        'unapproved_test_number': not request.test or (bool(phone) and any(
            _phone(number) == phone for number in policy.approved_test_numbers
        )),
    }
    blockers = tuple(reason for reason, passed in rules.items() if not passed)
    return CallAdmission(allowed=not blockers, blockers=blockers)
