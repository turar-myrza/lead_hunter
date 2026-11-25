"""Public, source-level B2C demand research with no personal-data collection.

The adapter discovers public pages and public Telegram channel views.  It does
not turn commenters, subscribers or profile owners into leads.  Its output is
an auditable demand signal that can inform consent-based advertising, content
or an inbound form.
"""
import hashlib
import re
from datetime import datetime, timezone

from app.integrations.lead_hunter_public import PublicFetcher, PublicSourceError
from app.integrations.product_intelligence import IntelligenceError
from app.models.control_center import PublicDemandExtraction
from app.models.universal_lead_hunter import Candidate, Evidence, SourceCapability
from app.services.lead_hunter_discovery import _Page
from app.services.universal_sources import Discovery, Verification, VerifiedCandidate


# A product/category page can establish only market context.  It must not turn
# a medical statement into an inferred consumer need.
_MEDICAL_CLAIM = re.compile(
    r'лечени|профилактик|диагноз|остеохондроз|радикулит|ревматизм|неврит|люмбаго|'
    r'грыж|травм|операц|после родов|хроническ\S* бол|бол\S*|проблем\S* со спин|'
    r'заболев|здоров|медицин|ортопед|treatment|diagnos|chronic\s+(?:back\s+)?pain|'
    r'back\s+pain|medical|health\s+condition|injur(?:y|ies)|surgery', re.IGNORECASE)
_URL_YEAR = re.compile(r'/(20\d{2})(?:/|$)')


class B2CPublicDemandDiscovery:
    capability=SourceCapability(
        id='b2c_public_demand', status='implemented', paths=['individual'], modes=['research'],
        access=('Configured Gemini Google Search grounding; public source-level demand research only. '
                'Telegram is limited to cited t.me/s/<public-channel> pages. No people, profiles, '
                'phone numbers, member lists, messages or health inference are collected.'))

    def __init__(self, ai, segment, log=lambda *args: None):
        self.ai,self.segment,self.log=ai,segment,log
        self.fetcher=PublicFetcher(max_requests=70,timeout=12)

    async def discover(self, request):
        topics=[item for item in self.segment.signals if item.strip()][:3]
        if not topics:
            topics=['публичные обсуждения выбора товара и способов безопасного использования']
        cited=[]; unavailable=False
        per_query=max(4,min(8,(request.limit+len(topics)-1)//len(topics)))
        for topic in topics:
            query=(f'Найди открытые публичные страницы и публичные Telegram-каналы в {request.brief.market}, '
                   f'где на уровне темы сообщества или публикации виден спрос/интерес к «{request.brief.name}» '
                   f'или к задаче «{topic}». Верни только публичные страницы самих каналов (Telegram только t.me/s/...), '
                   'публичные статьи или страницы сообществ. Не возвращай личные профили, комментарии отдельных людей, '
                   'контактные базы, закрытые группы, чаты, приглашения, страницы авторизации, медицинские диагнозы '
                   'или утверждения о свойствах товара.')
            try:
                cited.extend(await self.ai.search_public_web(query=query,context={
                    'product':request.brief.model_dump(mode='json'),
                    'segment':{'name':self.segment.name,'characteristics':self.segment.characteristics},
                    'mode':'b2c_public_demand_research',
                },limit=per_query))
            except IntelligenceError as exc:
                unavailable=True
                self.log('source_unavailable',{'source':'b2c_public_demand','reason':str(exc)})
        urls=[]; resolved=0
        for item in cited:
            url=item['url']
            if 'vertexaisearch.cloud.google.com/grounding-api-redirect/' in url:
                try:
                    url=await self.fetcher.resolve_grounding_redirect(url)
                    resolved+=1
                except PublicSourceError:
                    continue
            if url not in urls:
                urls.append(url)
        self.log('source_result',{'source':'b2c_public_demand','queries':len(topics),'cited_pages':len(cited),
                                  'resolved_pages':len(urls),'grounding_redirects_resolved':resolved})
        return Discovery(urls,incomplete=unavailable or (not urls and bool(cited)))

    async def verify(self, request, discovery):
        records={};failed=False
        for url in discovery.payload:
            if len(records)>=request.limit:
                break
            try:
                # URLs that explicitly identify an old publication cannot be
                # current B2C market evidence merely because they were fetched
                # today.  Pages without an explicit date are handled as
                # undated, not fresh.
                year = _URL_YEAR.search(url)
                if year and int(year.group(1)) < datetime.now(timezone.utc).year - 1:
                    self.log('page_skipped', {'url':url, 'reason':'stale_explicit_url_year'})
                    continue
                response=await self.fetcher.fetch(url,media={'text/html','application/xhtml+xml'})
                text='\n'.join(part for _,part in _Page(response.body).blocks)[:18000]
                if len(text)<100:
                    continue
                normalized=' '.join(text.casefold().split())
                result=await self.ai.generate('public_b2c_demand_page',{
                    'product':request.brief.model_dump(mode='json'),
                    'public_page':text,
                    'rule':'Extract public source-level demand only; never an individual or contact.',
                },PublicDemandExtraction)
                for signal in result.signals:
                    exact=lambda quote: bool(quote) and ' '.join(quote.casefold().split()) in normalized
                    if not all((exact(signal.source_label_quote),exact(signal.location_quote),exact(signal.demand_quote))):
                        continue
                    if request.brief.market.casefold() not in signal.location_quote.casefold():
                        continue
                    if _MEDICAL_CLAIM.search(signal.demand_quote):
                        self.log('signal_skipped', {'url':url, 'reason':'medical_claim_not_market_evidence'})
                        continue
                    key='b2c-'+hashlib.sha256((url+'\n'+signal.demand_quote).encode()).hexdigest()[:20]
                    # Candidate labels are intentionally short: a channel/page is
                    # a research subject, not a consumer record.
                    label=f'Публичный B2C-рыночный сигнал: {signal.source_label[:112]}'
                    evidence=[
                        Evidence(id='source',kind='identity',excerpt=signal.source_label_quote,url=url,
                                 observed_at=response.checked_at,basis='public_page'),
                        Evidence(id='location',kind='identity',excerpt=signal.location_quote,url=url,
                                 observed_at=response.checked_at,basis='public_page'),
                        Evidence(id='market_signal',kind='market_signal',excerpt=signal.demand_quote,url=url,
                                 observed_at=response.checked_at,basis='public_page'),
                    ]
                    records.setdefault(key,VerifiedCandidate(Candidate(
                        id=key,label=label,path='individual',subject_kind='public_demand_signal',evidence=evidence,
                        industry='Публичный B2C-рыночный сигнал (не контакт)',location=request.brief.market)))
                self.log('page_verified',{'url':url,'candidate_count':len(records),'checked_at':response.checked_at.isoformat()})
            except Exception:
                failed=True
                self.log('page_unavailable',{'url':url,'reason':'fetch_or_analysis_failed'})
        return Verification(list(records.values())[:request.limit],incomplete=failed)
