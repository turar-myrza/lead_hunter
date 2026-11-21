"""Product-derived public discovery behind the shared SourceAdapter contract."""
import hashlib,json,re
from urllib.parse import urlencode
from app.integrations.lead_hunter_public import PublicFetcher, PublicSourceError, public_https_url
from app.models.control_center import Extraction
from app.models.universal_lead_hunter import Candidate, Evidence, BuyerRole, SourceCapability
from app.services.lead_hunter_discovery import _Page
from app.services.universal_sources import Discovery, Verification, VerifiedCandidate

class ProductDiscovery:
    capability=SourceCapability(id='product_public',status='implemented',
        access='Product-derived GDELT DOC queries and reviewer-supplied public URLs; page quotes checked. No general social search.',
        paths=['organization'],modes=['research'])
    def __init__(self, ai, log=lambda *args: None, fetcher=None):
        self.ai,self.log=ai,log
        self.fetcher=fetcher or PublicFetcher(max_requests=100,timeout=12)
        self.request=None

    async def discover(self, request):
        self.request=request
        urls=list(request.discovery_urls);failed=False
        for query in request.discovery_queries:
            url='https://api.gdeltproject.org/api/v2/doc/doc?'+urlencode({'query':query,'mode':'artlist','format':'json','maxrecords':min(50,request.limit*2),'timespan':'3months','sort':'datedesc'})
            try:
                response=await self.fetcher.fetch(url,media={'application/json','text/plain'})
                rows=json.loads(response.body).get('articles',[])
                urls.extend(r['url'] for r in rows if isinstance(r,dict) and isinstance(r.get('url'),str))
                self.log('source_result',{'source':'gdelt','query':query,'count':len(rows)})
            except (PublicSourceError,ValueError,KeyError) as exc:
                reason=str(exc) if isinstance(exc,PublicSourceError) else 'invalid_source_response'
                failed=True;self.log('source_unavailable',{'source':'gdelt','query':query,'reason':reason})
        safe=[]
        for url in urls:
            try: clean=public_https_url(url)
            except PublicSourceError: continue
            if clean not in safe:safe.append(clean)
        return Discovery(safe[:min(40,request.limit*2)],incomplete=failed)

    async def verify(self, request, discovery):
        records={};failed=False
        for url in discovery.payload:
            if len(records)>=request.limit:break
            try:
                response=await self.fetcher.fetch(url,media={'text/html','application/xhtml+xml'})
                page=_Page(response.body)
                text='\n'.join(t for _,t in page.blocks)[:18000]
                if len(text)<100:continue
                normalized=' '.join(text.casefold().split())
                result=await self.ai.generate('public_page',{'product':request.brief.model_dump(mode='json'),'public_page':text},Extraction)
                for item in result.organizations:
                    exact=lambda quote: bool(quote) and ' '.join(quote.casefold().split()) in normalized
                    if not exact(item.identity_quote) or item.name.casefold() not in item.identity_quote.casefold():continue
                    if not exact(item.location_quote) or request.brief.market.casefold() not in item.location_quote.casefold():continue
                    # A named company and a city are not a commercial signal.
                    # Discovery must retain an exact, page-backed need/growth/
                    # automation statement before it can enter even the manager
                    # qualification queue.
                    if not exact(item.need_quote):continue
                    key='org-'+hashlib.sha256(item.name.casefold().strip().encode()).hexdigest()[:20]
                    evidence=[Evidence(id='identity',kind='identity',excerpt=item.identity_quote,url=url,observed_at=response.checked_at,basis='public_page'),
                              Evidence(id='location',kind='identity',excerpt=item.location_quote,url=url,observed_at=response.checked_at,basis='public_page')]
                    evidence.append(Evidence(id='need',kind='need',excerpt=item.need_quote,url=url,observed_at=response.checked_at,basis='public_page'))
                    roles=[]
                    if exact(item.role_quote) and item.role_actor and item.role_actor.casefold() in item.role_quote.casefold():
                        evidence.append(Evidence(id='role',kind='role',excerpt=item.role_quote,url=url,observed_at=response.checked_at,basis='public_page'))
                        roles=[BuyerRole(role=item.role_kind,actor=item.role_actor,evidence_ids=['role'])]
                    # Do not attribute arbitrary news-page email/phones to a named business.
                    # Contacts require explicit reviewer enrichment and evidence in this milestone.
                    records.setdefault(key,VerifiedCandidate(Candidate(id=key,label=item.name,path='organization',evidence=evidence,roles=roles,
                        industry=item.industry,location=item.location)))
                self.log('page_verified',{'url':url,'candidate_count':len(records),'checked_at':response.checked_at.isoformat()})
            except Exception:
                failed=True;self.log('page_unavailable',{'url':url,'reason':'fetch_or_analysis_failed'})
        return Verification(list(records.values())[:request.limit],incomplete=failed)
