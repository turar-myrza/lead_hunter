"""Grounded discovery of public B2B pages, including public Telegram channels.

This adapter never treats search output as proof. Grounded citations merely
produce a bounded candidate URL set for the existing public-page verifier.
"""
from app.integrations.product_intelligence import IntelligenceError
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
        self.verifier=ProductDiscovery(ai,log=log)

    async def discover(self, request):
        signals='; '.join(self.segment.signals[:6])
        roles='; '.join(self.segment.roles[:5])
        query=(f'Найди открытые публичные страницы организаций в рынке {request.brief.market}, '
               f'для которых релевантны услуги «{request.brief.name}». '
               f'Ищи свежие коммерческие сигналы: {signals}. '
               f'Приоритет: официальные сайты компаний, публичные вакансии и публикации, '
               f'публичные Telegram-каналы организаций. Возможные роли: {roles}.')
        try:
            sources=await self.ai.search_public_web(query=query,context={
                'product':request.brief.model_dump(mode='json'),
                'segment':{'name':self.segment.name,'characteristics':self.segment.characteristics},
            },limit=min(30,max(12,request.limit*2)))
        except IntelligenceError as exc:
            self.log('source_unavailable',{'source':'grounded_public_web','reason':str(exc)})
            return Discovery([],incomplete=True)
        urls=[item['url'] for item in sources]
        self.log('source_result',{'source':'grounded_public_web','cited_pages':len(urls)})
        return Discovery(urls)

    async def verify(self, request, discovery):
        return await self.verifier.verify(request,discovery)
