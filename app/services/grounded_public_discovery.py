"""Grounded discovery of public B2B pages, including public Telegram channels.

This adapter never treats search output as proof. Grounded citations merely
produce a bounded candidate URL set for the existing public-page verifier.
"""
from app.integrations.product_intelligence import IntelligenceError
from app.integrations.lead_hunter_public import PublicFetcher, PublicSourceError
from app.models.universal_lead_hunter import SourceCapability
from app.services.product_discovery import ProductDiscovery
from app.services.universal_sources import Discovery, Verification


class GroundedPublicDiscovery:
    capability=SourceCapability(
        id='grounded_public_web',status='implemented',
        access='Configured Gemini Google Search grounding; only cited public pages are fetched again with robots.txt checks. Public Telegram is limited to t.me/s/<channel>.',
        paths=['organization'],modes=['research'])

    def __init__(self, ai, segment, log=lambda *args: None):
        self.ai,self.segment,self.log=ai,segment,log
        self.fetcher=PublicFetcher(max_requests=100,timeout=12)
        self.verifier=ProductDiscovery(ai,log=log,fetcher=self.fetcher)

    async def discover(self, request):
        signals=[signal for signal in self.segment.signals if signal.strip()][:3]
        roles='; '.join(self.segment.roles[:5])
        topics=signals or ['признаки роста продаж, расширения филиалов или внедрения автоматизации']
        # Multiple narrow searches outperform one generic "find companies" prompt,
        # while retaining a small, predictable external-call budget.
        queries=[
            (f'Найди открытые публичные страницы НАЗВАННЫХ организаций в {request.brief.market} '
             f'с прямым, свежим коммерческим сигналом «{topic}», для которых может быть релевантно «{request.brief.name}». '
             'Верни ссылки только на первоисточники: официальный сайт организации, её публичную вакансию, '
             'публичную деловую публикацию или публичный Telegram-канал самой организации. '
             'Не используй реестры компаний, каталоги, агрегаторы вакансий, страницы поисковой выдачи, личные профили '
             'или материал без названия организации и конкретного сигнала. '
             f'Возможные роли для проверки: {roles}.')
            for topic in topics
        ]
        cited=[];unavailable=False
        per_query=max(4,min(10,(request.limit*2+len(queries)-1)//len(queries)))
        for query in queries:
            try:
                cited.extend(await self.ai.search_public_web(query=query,context={
                    'product':request.brief.model_dump(mode='json'),
                    'segment':{'name':self.segment.name,'characteristics':self.segment.characteristics},
                },limit=per_query))
            except IntelligenceError as exc:
                unavailable=True
                self.log('source_unavailable',{'source':'grounded_public_web','reason':str(exc)})
        urls=[];resolved=0
        for item in cited:
            url=item['url']
            if 'vertexaisearch.cloud.google.com/grounding-api-redirect/' in url:
                try:
                    url=await self.fetcher.resolve_grounding_redirect(url)
                    resolved+=1
                except PublicSourceError:
                    continue
            if url not in urls: urls.append(url)
        self.log('source_result',{'source':'grounded_public_web','queries':len(queries),'cited_pages':len(cited),
                                  'resolved_pages':len(urls),'grounding_redirects_resolved':resolved})
        return Discovery(urls,incomplete=unavailable or (not bool(urls) and bool(cited)))

    async def verify(self, request, discovery):
        return await self.verifier.verify(request,discovery)
