"""Control-center application service; campaigns execute the universal orchestrator."""
import asyncio,json
from types import SimpleNamespace
from app.models.control_center import ProductProfile,ProductIntelligence,QuickProductInput,Segment,Review
from app.models.universal_lead_hunter import ProductBrief,RunRequest
from app.services.control_store import ControlStore,unpack
from app.services.sales_store import now
from app.services.universal_lead_hunter import LeadHunterApplication
from app.services.universal_sources import ADAPTERS
from app.services.product_discovery import ProductDiscovery
from app.services.grounded_public_discovery import GroundedPublicDiscovery
from app.integrations.product_intelligence import ProductAI,IntelligenceError

class ControlCenter:
    def __init__(self, store, settings, ai=None):
        self.store,self.settings=store,settings
        self.ai=ai or ProductAI(settings,store)

    def analyze(self, ident):
        product=self.store.product(ident)
        if not product:raise ValueError('product_not_found')
        profile=ProductProfile.model_validate(product['payload'])
        if not profile.public_analysis_allowed:raise ValueError('public_material_approval_required')
        return self.store.enqueue('analysis',product,profile.model_dump(mode='json'))

    def quick_start(self, data: QuickProductInput):
        """Save a minimal brief and queue its analysis immediately.

        Category and buyer paths are generated as hypotheses for the operator to
        approve; they are not a questionnaire the operator must fill in.
        """
        if not data.public_analysis_allowed:
            raise ValueError('public_material_approval_required')
        profile=ProductProfile(
            brief=ProductBrief(
                name=data.name,
                description=data.description,
                category='Категория определяется AI после анализа продукта',
                market=data.market,
                buyer_paths={'organization','individual'},
            ),
            public_analysis_allowed=True,
        )
        product=self.store.save_product(profile.model_dump(mode='json'))
        return {'product':product,'analysis_job':self.analyze(product['id'])}

    def offer(self, ident, data):
        product=self.store.product(ident)
        if not product:raise ValueError('product_not_found')
        if not data.template_reviewed:raise ValueError('template_branding_review_required')
        return self.store.enqueue('offer',product,data.model_dump(mode='json'))

    def approve_segment(self, ident, segment, approved):
        with self.store.base.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=unpack(db.execute('SELECT * FROM cc_segment WHERE id=?',(ident,)).fetchone())
            if not row:raise ValueError('segment_not_found')
            product=unpack(db.execute('SELECT * FROM cc_product WHERE id=?',(row['product_id'],)).fetchone())
            if product['revision']!=row['revision']:raise ValueError('product_changed_reanalyze')
            if segment.path not in product['payload']['brief']['buyer_paths']:raise ValueError('buyer_path_mismatch')
            db.execute('UPDATE cc_segment SET payload=?,approved=? WHERE id=?',(segment.model_dump_json(),int(approved),ident))
        self.store.audit('segment_approved' if approved else 'segment_unapproved',ident)

    def campaign(self, segment_id, target):
        segment=self.store.segment(segment_id)
        if not segment or not segment['approved']:raise ValueError('audience_approval_required')
        product=self.store.product(segment['product_id'])
        if product['revision']!=segment['revision']:raise ValueError('product_changed_reanalyze')
        profile=ProductProfile.model_validate(product['payload'])
        if not profile.public_analysis_allowed:raise ValueError('public_material_approval_required')
        # Individual discovery is permissioned inbound only, never web scraping of people.
        if segment['payload']['path']=='individual':raise ValueError('individual_inbound_adapter_required')
        strategy = self._discovery_strategy(profile, Segment.model_validate(segment['payload']))
        return self.store.enqueue('discovery', product, {
            'segment_id': segment_id,
            'segment': segment['payload'],
            'target': target,
            'product': product['payload'],
            'strategy': strategy,
        })

    def _discovery_strategy(self, profile, segment):
        """Choose sources inside the application; operators approve an audience, not adapters."""
        material = ' '.join((profile.brief.name, profile.brief.description, profile.brief.category)).casefold()
        market = profile.brief.market.casefold()
        crm_product = any(word in material for word in ('crm', 'битрикс', 'автоматизац', 'мойсклад'))
        web_research_ready=bool(self.settings.control_ai_web_research_enabled and self.settings.control_ai_enabled and self.settings.control_gemini_key)
        public_web=['grounded_public_web'] if web_research_ready else []
        web_note=(' Public web research is enabled.' if public_web
                  else ' Broader public web research is prepared but not yet enabled.')
        if segment.path == 'organization' and crm_product and market in {'шымкент', 'shymkent'}:
            return {
                'label': 'Публичные коммерческие сигналы Шымкента',
                'source_ids': ['shymkent_crm', *public_web],
                'research_context': 'crm_sales_operations',
                'crm_read_only': True,
                'reason': 'Региональные сигналы роста, вакансий, автоматизации и подтверждённого Битрикс24.'+web_note,
            }
        if segment.path == 'organization' and crm_product and market in {'казахстан', 'kazakhstan'}:
            return {
                'label': 'Публичные коммерческие сигналы Казахстана',
                'source_ids': ['kazakhstan_sales', *public_web],
                'research_context': 'crm_sales_operations',
                'crm_read_only': True,
                'reason': 'Региональные сигналы роста, вакансий, автоматизации и подтверждённого Битрикс24.'+web_note,
            }
        return {
            'label': 'Публичные источники по одобренной аудитории',
            'source_ids': ['product_public', *public_web],
            'research_context': 'general',
            'crm_read_only': True,
            'reason': 'Публичные страницы и публикации с проверяемыми цитатами.'+web_note,
        }

    def review(self, job_id, candidate_id, review):
        job=self.store.job(job_id)
        if not job or not job.get('result'):raise ValueError('results_not_available')
        run_id=job['result'].get('run_id')
        app=LeadHunterApplication(self.store.base,self.settings.lead_hunter_output)
        result=app.result(run_id) if run_id else None
        prospect=next((p for p in result.opportunities if p.candidate.id==candidate_id),None) if result else None
        if not prospect:raise ValueError('prospect_not_found')
        if not set(review.evidence_ids)<={e.id for e in prospect.candidate.evidence}:raise ValueError('invalid_evidence_reference')
        if review.state=='qualified' and not review.ethics_passed:raise ValueError('ethical_review_required')
        if review.state=='qualified' and ('need' not in {e.kind for e in prospect.candidate.evidence} or prospect.qualification.state=='excluded'):
            raise ValueError('qualification_requires_need_and_no_exclusion')
        with self.store.base.connect() as db:
            db.execute('INSERT INTO cc_review VALUES(?,?,?,?) ON CONFLICT(job_id,candidate_id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                (job_id,candidate_id,review.model_dump_json(),now().isoformat()))
        self.store.audit('qualification_'+review.state,str(job_id)+':'+candidate_id)

    def handoff(self, job_id,candidate_id):
        snapshot=self.store.snapshot()
        review=next((r for r in snapshot['reviews'] if r['job_id']==job_id and r['candidate_id']==candidate_id),None)
        if not review or review['payload']['state']!='qualified':raise ValueError('qualified_review_required')
        job=self.store.job(job_id)
        crm=(job['result'] or {}).get('crm',{}).get(candidate_id,{'state':'not_checked'})
        draft={'candidate_id':candidate_id,'job_id':job_id,'status':'prepared_not_synced','crm_review':crm,
               'writes_enabled':False,'next_action':review['payload']['next_action'],
               'blockers':['Explicit production CRM authorization and write adapter required']}
        if crm.get('state')!='no_match_accessible':draft['blockers'].append('CRM identity/history requires review')
        with self.store.base.connect() as db:
            db.execute('INSERT INTO cc_handoff VALUES(?,?,?) ON CONFLICT(job_id,candidate_id) DO UPDATE SET payload=excluded.payload',
                       (job_id,candidate_id,json.dumps(draft)))
        self.store.audit('handoff_prepared',str(job_id)+':'+candidate_id)
        return draft

    def snapshot(self):
        data=self.store.snapshot()
        app=LeadHunterApplication(self.store.base,self.settings.lead_hunter_output)
        for job in data['jobs']:
            rid=(job.get('result') or {}).get('run_id')
            if rid:
                status=app.status(rid);result=app.result(rid)
                job['workflow']=status.model_dump(mode='json') if status else None
                job['opportunities']=result.model_dump(mode='json')['opportunities'] if result else []
        return data

    async def process(self, job):
        try:
            if job['kind']=='offer':
                from pathlib import Path
                from documents.offer_generator import generate_commercial_offer
                from app.models.control_center import OfferInput
                data=OfferInput.model_validate(job['payload'])
                folder=Path(self.settings.lead_hunter_output).resolve()/'offers'/str(job['id'])
                folder.mkdir(parents=True,exist_ok=True)
                await asyncio.to_thread(generate_commercial_offer,
                    client_company=data.client_company,client_name=data.client_name,
                    items=[x.model_dump() for x in data.items],intro_text=data.intro_text,
                    lang=data.lang,output_docx_path=str(folder/'offer.docx'),
                    output_pdf_path=str(folder/'offer.pdf'))
                if not (folder/'offer.pdf').is_file():raise ValueError('pdf_not_created')
                self.store.finish(job['id'],'completed',{'files':['docx','pdf'],
                    'generator':'documents.offer_generator.generate_commercial_offer',
                    'sent':False,'review_required':True})
            elif job['kind']=='analysis':
                profile=ProductProfile.model_validate(job['payload'])
                analysis=await self.ai.generate('product_analysis',profile.model_dump(mode='json'),ProductIntelligence)
                if any(s.path not in profile.brief.buyer_paths for s in analysis.segments):raise IntelligenceError('generated_buyer_path_mismatch')
                with self.store.base.connect() as db:
                    for segment in analysis.segments:
                        db.execute('INSERT INTO cc_segment(job_id,product_id,revision,payload) VALUES(?,?,?,?)',
                            (job['id'],job['product_id'],job['revision'],segment.model_dump_json()))
                self.store.finish(job['id'],'completed',{'analysis':analysis.model_dump(mode='json'),
                    'basis':'AI hypotheses from seller-supplied public material; not independently verified',
                    'confidence':'unverified_hypotheses','model':self.settings.control_ai_model})
            else:
                product=ProductProfile.model_validate(job['payload']['product']);seg=Segment.model_validate(job['payload']['segment'])
                strategy=job['payload'].get('strategy') or self._discovery_strategy(product,seg)
                def log(action, detail):self.store.audit(action,json.dumps({'job_id':job['id'],**detail},ensure_ascii=False))
                factory=lambda: ProductDiscovery(self.ai,log)
                factory.capability=ProductDiscovery.capability
                grounded_factory=lambda: GroundedPublicDiscovery(self.ai,seg,log)
                grounded_factory.capability=GroundedPublicDiscovery.capability
                app=LeadHunterApplication(self.store.base,self.settings.lead_hunter_output,adapters=(*ADAPTERS,factory,grounded_factory))
                brief=product.brief.model_copy(update={'buyer_paths':{seg.path}})
                request=RunRequest(brief=brief, source_ids=strategy['source_ids'],
                    research_context=strategy['research_context'], crm_read_only=strategy['crm_read_only'],
                    limit=job['payload']['target'],
                    # Seller references describe our product. They are never treated as buyer sources.
                    discovery_queries=seg.queries if 'product_public' in strategy['source_ids'] else [],
                    discovery_urls=seg.source_urls if 'product_public' in strategy['source_ids'] else [],
                    owner='CEO review queue')
                run_id=app.submit(request)
                with self.store.base.connect() as db:db.execute('UPDATE cc_job SET result=? WHERE id=?',(json.dumps({'run_id':run_id}),job['id']))
                await app.process(run_id)
                result=app.result(run_id);crm={}
                if result and result.opportunities:
                    # Reuse conservative read-only name matching; never treat name-only match as confirmed.
                    from app.services.sales_crm import check_sales_crm
                    companies=[SimpleNamespace(name=p.candidate.label,aliases=[],site=None,phones=[],emails=[]) for p in result.opportunities]
                    checks,_=await check_sales_crm(companies)
                    crm={p.candidate.id:checks.get(p.candidate.label,{'state':'unavailable'}) for p in result.opportunities}
                count=len(result.opportunities) if result else 0
                workflow=app.status(run_id)
                source_errors=[error.model_dump(mode='json') for stage in (workflow.stages if workflow else []) for error in stage.errors]
                status='completed' if result and workflow and workflow.status=='completed' and count>=request.limit else 'partial'
                self.store.finish(job['id'],status,{'run_id':run_id,'target':request.limit,'found':count,'crm':crm,
                    'shortfall':max(0,request.limit-count),'contacts_verified':0,'strategy':strategy,
                    'source_errors':source_errors,
                    'note':'Evidence-backed candidates require manager qualification; no messages or CRM writes.'})
        except Exception as exc:
            code=str(exc) if isinstance(exc,IntelligenceError) else 'job_failed_check_dependencies'
            self.store.finish(job['id'],'failed',self.store.job(job['id']).get('result'),code)

    async def worker_once(self):
        with self.store.base.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO setting VALUES('control_worker_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(now().isoformat(),))
            row=db.execute("SELECT * FROM cc_job WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:return False
            job=unpack(row);db.execute("UPDATE cc_job SET status='running' WHERE id=?",(job['id'],))
        self.store.audit('job_started',job['id'])
        await self.process(job)
        return True
