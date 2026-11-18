"""Run the evidence-to-draft pipeline. Dispatch is outside the read-only worker."""
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from app.models.sales_team import Prospect
from app.services.sales_copy_model import CopyModelError, codex_copy
from app.services.sales_crm import check_sales_crm, identity_keys
from app.services.sales_discovery import discover_national
from app.services.sales_store import now
from app.services.sales_voice import VoiceDossier, simulate_voice_dialogue

ROLES = ('commercial_researcher','qualification_analyst','buyer_psychologist','direct_response_marketer',
         'visual_creative','communication_specialist','ai_voice_sdr','sales_learning_analyst')
PRODUCTS = {'A':'Внедрение Битрикс24','B':'Доработка Битрикс24','C':'Регистрация WhatsApp-звонков'}
FORBIDDEN = re.compile(r'микрозайм|микрофинанс|процентн\w* кредит|займы под зал|ломбард|(?<!\w)алкогол|табак|вейп|казино|букмекер|азартн', re.I)
B24 = re.compile(r'(?:bitrix|битрикс)\s*24', re.I)
THEMES = {'A':'#1b5a91','B':'#7151a2','C':'#217365'}
_BANNED_COPY = re.compile(r'мы (?:изучили|увидели|заметили|проанализировали)|вашем сайте написано|вы теряете|гарантиру\w*\s+рост', re.I)
_KK = re.compile(r'[әғқңөұүһі]', re.I)
OUT_DIR = Path('outputs/sales_team')
KNOWLEDGE = Path(__file__).resolve().parents[1] / 'sales_knowledge'


class Stopped(RuntimeError):
    pass


@dataclass(frozen=True)
class Skill:
    role: str
    text: str
    version: str
    digest: str

    def bound(self, key, default, lower, upper):
        header = self.text.split('---',2)[1]
        value = re.search(fr'^{key}:\s*(\d+)\s*$',header,re.M)
        result = int(value[1]) if value else default
        if not lower <= result <= upper:
            raise ValueError(f'Недопустимый параметр навыка {self.role}: {key}')
        return result

    def reference(self):
        return {'role':self.role, 'version':self.version, 'sha256':self.digest}


def load_skills():
    skills = {}
    for role in ROLES:
        raw = (KNOWLEDGE / role / 'SKILL.md').read_text(encoding='utf-8')
        version = re.search(r'^version:\s*["\']?([\d.]+)', raw, re.M)
        if not version or not re.search(fr'^role:\s*{role}\s*$', raw, re.M):
            raise ValueError(f'Не найден паспорт рабочего навыка {role}')
        skills[role] = Skill(role, raw, version[1], sha256(raw.encode()).hexdigest())
    return skills


def _write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _contact_proof(prospect, sources):
    from app.services.lead_hunter_duplicates import normalize_phone
    evidence = {}
    for source in sources:
        if source.evidence_type in {'employer_vacancy','vacancy','social','news','job_listing'}:
            continue
        plain = source.fact.casefold()
        for email in prospect.emails:
            if email.casefold() in plain:
                evidence.setdefault(email, source.url)
        numbers = {normalize_phone(value) for value in re.findall(r'(?<!\d)(?:\+?7|8)[\s().-]*\d(?:[\s().-]*\d){9}(?!\d)', plain)}
        for phone in prospect.phones:
            if (normalized := normalize_phone(phone)) and normalized in numbers:
                evidence.setdefault(phone, source.url)
    return evidence


def qualify(p, crm, *, prior=False, simulated=False, min_sources=2, min_steps=3):
    """Separate facts, hypotheses, relationship safety and contact permission."""
    state = crm.get('state', 'not_checked')
    status, reason = 'research', 'Требуется проверка вида деятельности, делового контакта или второго источника.'
    gap = (date.today() - timedelta(days=30))
    current = [s for s in p.sources if s.observed_at and gap <= s.observed_at <= date.today()]
    contact = _contact_proof(p,current)
    contact_source = next(iter(contact.values()),None)
    sourced_bitrix = any(B24.search(s.fact) for s in current)
    evidence = [s.url for s in current]
    if simulated:
        status, reason = 'simulation', 'Учебная карточка. Контакт и CRM смоделированы.'
    elif FORBIDDEN.search(p.industry) or re.search(r'ethics_review|неоднознач|этическ.*проверк', p.ethical, re.I):
        status, reason = 'excluded', 'Основная деятельность запрещена либо этический статус требует решения руководителя.'
    elif prior:
        status, reason = 'hold', 'Совпадение с предыдущей подборкой.'
    elif state in {'confirmed_duplicate','possible_match'}:
        status = 'account' if crm.get('relationship') in {'current_customer','former_customer','existing_customer'} else 'hold'
        reason = 'Найдена история. Решение передать владельцу отношений, новые предложения не сформированы.'
    elif state != 'no_match_accessible':
        status, reason = 'hold', 'Полная READ ONLY сверка совпадений не завершена.'
    else:
        checked = crm.get('checked_at')
        fresh_crm = checked and timedelta(0) <= now() - datetime.fromisoformat(checked) <= timedelta(hours=24)
        has_ethics = re.search(r'не обнаружен\w*|допустим|не выявлен|не установлено запрещ', p.ethical, re.I)
        if not fresh_crm:
            status, reason = 'hold', 'Снимок CRM устарел, нужна новая проверка.'
        elif p.product == 'B' and not sourced_bitrix:
            reason = 'Источник не подтверждает работу в Битрикс24.'
        elif p.product == 'A' and B24.search(p.crm_status) and 'не установ' not in p.crm_status.casefold():
            reason = 'Для рабочего Битрикс24 нужен сегмент доработки.'
        elif p.product == 'C' and not any(re.search(r'whats\s*app|ватсап', s.fact, re.I) for s in current):
            reason = 'Уточнить публичный канал WhatsApp и контекст звонков.'
        elif re.search(r'не следует использовать как универсальный канал', p.watch, re.I):
            reason = 'Опубликованный контакт предназначен для другой цели. Нужно подтвердить общий деловой канал.'
        elif len(set(evidence)) >= min_sources and contact and has_ethics and p.process and p.outcome and len(p.visual_steps) >= min_steps:
            status, reason = 'qualified', 'Подтверждены коммерческий повод, рабочий канал и подходящий процесс.'
    return {'status':status, 'reason':reason, 'product':p.product, 'sources':evidence, 'contact_source':contact_source, 'contact_values':list(contact),
            'purchase_intent':'unknown', 'contact_permission':'draft_only' if status == 'qualified' else 'blocked',
            'fact':p.fact, 'hypothesis':p.hypothesis, 'offer':p.offer, 'next_step':p.next_step,
            'crm_state':state, 'crm_checked_at':crm.get('checked_at'), 'crm_owner':crm.get('owner'),
            'crm_history':crm.get('history_status'), 'crm_matches':crm.get('matches',[]),
            'first_question':('Какой шаг после поступления заявки сегодня требует проверки или повторного согласования?')}


def recognition(p):
    host = urlsplit(p.site or '').hostname or ''
    return {'title':p.process, 'brand':p.name, 'public_context':host.removeprefix('www.'),
            'steps':p.visual_steps, 'benefit':p.outcome or p.offer,
            'basis':[p.sources[0].url], 'data_mode':'synthetic'}


def drafts(p, *, model=None):
    cta_ru = 'Удобно посмотреть пример за 12 минут?'
    cta_kk = 'Осы үлгіні 12 минутта қарап шығуға ыңғайлы ма?'
    offer = p.offer.strip().rstrip('.') + '.'
    if model:
        result = model.model_dump()
        content = ' '.join((result['subject'], result['email'], result['call']))
        if (_BANNED_COPY.search(content) or result['email'].count('?') != 1 or result['call'].count('?') != 1
                or 'CRM Consulting' not in result['email'] or 'CRM Consulting' not in result['call']
                or not any(s in result['call'].casefold() for s in ('ai', 'ии', 'жасанды интеллект'))
                or (p.language == 'kk' and not _KK.search(result['email']))):
            raise CopyModelError('Черновик модели не прошёл проверку текста')
        return {**result,'language':p.language, 'mode':'codex', 'cta':cta_kk if p.language=='kk' else cta_ru,
                'basis':[p.sources[result['evidence_index']].url]}
    # Each fallback carries the account-specific process and scenario, in the
    # language in which its source-backed description is available.
    language = 'kk' if p.language == 'kk' and _KK.search(p.offer) and _KK.search(p.outcome) else 'ru'
    if language == 'kk':
        return {'subject':f'{p.name} · {p.process}'[:95],
                'email':f'Сәлеметсіз бе! {offer} {p.outcome} {cta_kk}\n\nГайни, CRM Consulting',
                'call':f'Сәлеметсіз бе! Мен CRM Consulting компаниясының AI көмекшісімін. {offer} {cta_kk}',
                'mode':'rules', 'language':'kk', 'cta':cta_kk, 'offer_kk':offer, 'basis':[p.sources[0].url]}
    return {'subject':f'{p.name} · {p.process}'[:95],
            'email':f'Здравствуйте! {offer} {p.outcome} {cta_ru}\n\nГайни, CRM Consulting',
            'call':f'Здравствуйте! Я AI-помощник CRM Consulting. {offer} {cta_ru}',
            'mode':'rules', 'language':'ru', 'cta':cta_ru, 'offer_ru':offer, 'basis':[p.sources[0].url],
            'language_pending':p.language if p.language!='ru' else None}


def visual_page(p, concept, copy):
    kazakh = copy['language']=='kk'
    stage = 'Кезең' if kazakh else 'Этап'
    sample = 'Жұмыс үлгісі' if kazakh else 'Пример работы'
    synthetic = 'Оқу деректері мен үлгі.' if kazakh else 'Учебные данные и демонстрационный макет.'
    steps = ''.join(f'<li style="--delay:{i * .55}s"><span>{stage} {i+1}</span><b>{escape(step)}</b></li>'
                    for i,step in enumerate(concept['steps']))
    labels = {'A':'Битрикс24 енгізу','B':'Битрикс24 жетілдіру','C':'WhatsApp қоңырауларын тіркеу'}
    label = labels[p.product] if kazakh else PRODUCTS[p.product]
    guard = ('Нөмір, провайдер және CRM үйлесімділігін тексеру қажет. ' if kazakh
             else 'Совместимость номера, провайдера и CRM требуется проверить. ') if p.product=='C' else ''
    return f"""<!doctype html>
<html lang="{copy['language']}"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{escape(p.name)} · CRM Consulting</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#edf3f6;color:#142d3b;font:16px/1.5 Arial,sans-serif}}
main{{max-width:820px;margin:32px auto;padding:0 18px 30px}}header{{display:flex;justify-content:space-between;gap:10px;padding:0 0 15px}}
header span{{background:#dce7ec;padding:2px 10px;border-radius:13px}}
.hero{{padding:28px;background:linear-gradient(105deg,#102b3b,{THEMES[p.product]});color:#fff;border-radius:18px 18px 0 0}}
.hero small{{color:#d6e6ef}}h1{{font:700 clamp(30px,5vw,46px)/1.12 Georgia,serif;margin:10px 0}}
section{{background:white;padding:22px;border-radius:0 0 18px 18px}}ol{{list-style:none;padding:0;display:flex;gap:10px}}
li{{flex:1;padding:12px;border:1px solid #c9d7de;border-radius:10px;animation:walk 1.5s both;animation-delay:var(--delay);overflow-wrap:anywhere}}
li span,li b{{display:block}}li span{{font-size:12px;color:#426074}}.benefit{{background:#e9f2f6;padding:13px;border-radius:9px}}small{{color:#465d6d}}
@keyframes walk{{0%,100%{{background:#f7fafb}}50%{{background:#cce3ee;transform:translateY(-3px)}}}}
@media(max-width:580px){{main{{margin:16px auto}}ol{{flex-direction:column}}}}
@media(prefers-reduced-motion:reduce){{li{{animation:none}}}}
</style>
<main><header><b>CRM Consulting</b><span>{sample}</span></header>
<div class="hero"><small>{escape(concept['public_context'])} · {escape(label)}</small>
<h1>{escape(p.name)}</h1><p>{escape(concept['title'])}</p></div>
<section><ol>{steps}</ol><p class="benefit">{escape(concept['benefit'])}</p>
<p>{escape(copy['cta'])}</p><small>{escape(guard)}{synthetic}</small></section></main>"""


def communication(p, copy):
    return {'language':copy['language'], 'preferred_language':p.language, 'call_opening':copy['call'],
            'email_channel':bool(p.emails), 'phone_channel':bool(p.phones), 'permission':'draft_only',
            'handoff_to':'Гайни', 'switch_language':'По запросу или по языку ответа',
            'recording':'Требует отдельного решения', 'source_urls':copy['basis']}


def learning_evaluation(metrics, min_sample=30):
    # Use per-account observed outcomes, not raw events or simulated transcripts.
    # A descriptive row is not a recommendation to change production rules.
    views = {}
    for observed in metrics:
        key = (observed['variant'], observed['language'], observed['segment'])
        views.setdefault(key, {})[observed['kind']] = observed['n']
    comparisons = []
    for (variant, language, segment), counts in sorted(views.items()):
        denominator = counts.get('email_delivered', 0) if variant.startswith('email') else counts.get('call_connected',0)
        meetings = counts.get('meeting_booked',0)
        comparisons.append({'variant':variant,'language':language,'segment':segment,
                            'reached':denominator,'meetings':meetings,'sales':counts.get('sale',0),
                            'meeting_rate': round(meetings / denominator, 3) if denominator and meetings <= denominator else None,
                            'review':'low_sample' if denominator < min_sample else 'descriptive_requires_review'})
    return {'observed_segments':comparisons,'rule_change':'none','recommended_review':
            'Сравнить долю назначенных встреч на сопоставимых сегментах после накопления наблюдений.',
            'simulated_outcomes_counted':False}


def _deduplicate(prospects):
    groups = []
    for candidate in prospects:
        keys = set(identity_keys(candidate))
        # Domain and shared switchboards can span franchisees or public institutions.
        # Only exact names/aliases collapse research before CRM.
        names = {key for key in keys if key.startswith('name:')}
        if any(names & seen for seen in groups):
            continue
        groups.append(names)
        yield candidate


def _report(destination, run, qualified, blocked, evidence, learning, audit, candidates=()):
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = destination / 'summary.json'
    full = {'run_id':run['id'], 'origin':run['origin'], 'researched':len(qualified)+len(blocked),
            'qualified':0 if run['origin']=='simulation' else len(qualified), 'prepared':len(qualified), 'needs_review':len(blocked),
            'groups':dict(Counter(d['product'] for d in qualified)),
            'sources':evidence,'qualification':qualified,'review':blocked,'learning':learning,'crm_read_audit':audit,
            'outbound_enabled':False, 'source_details':[
                {'company':p.name, **s.model_dump(mode='json')} for p in candidates for s in p.sources]}
    _write(path,json.dumps(full,ensure_ascii=False,indent=2))
    from app.services.sales_workbook import export_sales_workbook
    export_sales_workbook(destination / 'leads.xlsx', full)
    fields = ('name','city','product','language','contact','crm_state','fact','sources','offer','next_step',
              'subject','email','call','visual','permission','copy_mode')
    import io
    output = io.StringIO()
    writer = csv.DictWriter(output,fieldnames=fields,extrasaction='ignore')
    writer.writeheader()
    from app.services.lead_hunter import safe_cell
    for row in qualified:
        writer.writerow({key:safe_cell(value) if isinstance(value,str) else value for key,value in row.items()})
    _write(destination / 'leads.csv', '\ufeff'+output.getvalue())
    rows = ''.join('<tr><td>'+escape(row['name'])+'</td><td>'+escape(row['offer'])+
                   f'</td><td><a href="{escape(row["visual"],quote=True)}">Показ</a></td></tr>' for row in qualified)
    _write(destination / 'index.html', '<!doctype html><html lang="ru"><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Материалы Гайни</title>'
           '<style>body{font:16px/1.5 Arial;color:#123145;margin:32px auto;max-width:900px;padding:0 16px}'
           'table{border-collapse:collapse;width:100%}td,th{padding:11px;border-bottom:1px solid #dce5ea;text-align:left}'
           'a{color:#115988}</style><h1>Материалы Гайни</h1><p><a href="leads.xlsx">Скачать Excel</a> · <a href="leads.csv">CSV</a></p>'
           '<table><thead><tr><th>Компания</th><th>Коммерческое предложение</th><th>Материал</th></tr></thead>'
           f'<tbody>{rows}</tbody></table></html>')
    return full


async def process_run(store, run, *, output_root=OUT_DIR, crm_reader=check_sales_crm,
                      discover=discover_national, model_runner=codex_copy, sources=(), previous_keys=()):
    skills = load_skills()
    min_sources = skills['qualification_analyst'].bound('minimum_sources',2,2,10)
    min_steps = skills['visual_creative'].bound('minimum_steps',3,3,6)
    min_sample = skills['sales_learning_analyst'].bound('minimum_reached_for_descriptive_review',30,15,500)
    run_id = run['id']
    mode = run['origin']
    simulated = mode == 'simulation'
    directory = Path(output_root) / str(run_id)

    def check_stop():
        if store.stopped():
            raise Stopped()

    def stage(company, role, operation):
        check_stop()
        if (saved := store.step(run_id, company, role)) is not None:
            return saved
        result = operation()
        store.save(run_id, company, role, {**result, 'skill':skills[role].reference()})
        return result

    payload = json.loads(run['payload']) or {}
    checkpoint = store.step(run_id,'','discovery')
    if checkpoint is None:
        if mode == 'automatic':
            found, provider_log = await discover(stopped=store.stopped, sources=sources)
            check_stop()
            if provider_log and not any(r.get('status')=='available' for r in provider_log):
                store.finish(run_id, 'sources_unavailable','source_error')
                return _report(directory,run,[],[],provider_log,learning_evaluation([]),{})
        else:
            found = [Prospect.model_validate(c) for c in payload['prospects']]
            provider_log = [{'source':mode, 'status':'validated', 'results':len(found)}]
        checkpoint = {'prospects':[x.model_dump(mode='json') for x in _deduplicate(found)],'sources':provider_log}
        store.save(run_id,'','discovery',checkpoint)
    candidates = [Prospect.model_validate(c) for c in checkpoint['prospects']]
    for p in candidates:
        stage(p.name,'commercial_researcher',lambda:{'name':p.name,'fact':p.fact,'city':p.city,
              'sources':[s.model_dump(mode='json') for s in p.sources], 'input':mode})
    check_stop()
    snapshot = store.step(run_id,'','crm')
    if snapshot is None:
        if simulated:
            crm, audit = {p.name:{'state':'no_match_accessible','checked_at':now().isoformat()} for p in candidates}, {'simulated':True}
        else:
            crm, audit = await crm_reader(candidates,stopped=store.stopped)
        check_stop()
        snapshot = {'results':crm,'audit':audit}
        store.save(run_id,'','crm',snapshot)
    reviewed, blocked, voice = [], [], []
    original = set(previous_keys)
    for p in candidates:
        keys = identity_keys(p)
        prior = bool(original.intersection(keys) or store.prior(run_id,keys))
        q = stage(p.name,'qualification_analyst',lambda:qualify(p,snapshot['results'].get(p.name,{}),
                  prior=prior,simulated=simulated,min_sources=min_sources,min_steps=min_steps))
        if q['status'] not in {'qualified','simulation'}:
            blocked.append({'name':p.name,'city':p.city,**q})
            continue
        if len(reviewed) >= payload.get('limit',30):
            blocked.append({'name':p.name,'city':p.city,**q,'status':'reserve','reason':'Лимит отчёта'})
            continue
        psych = stage(p.name,'buyer_psychologist',lambda:recognition(p))
        def marketing():
            if payload.get('model') == 'codex' and len(reviewed) < payload.get('model_limit',1):
                policy = [skills[role].text for role in ('buyer_psychologist','direct_response_marketer','communication_specialist')]
                try:
                    return drafts(p,model=model_runner(p,policy))
                except CopyModelError:
                    return {**drafts(p), 'model_status':'unavailable_or_invalid'}
            return drafts(p)
        copy = stage(p.name,'direct_response_marketer',marketing)
        tag = sha256(p.name.encode()).hexdigest()[:12]
        material = f'demo-{tag}.html'
        stage(p.name,'visual_creative',lambda:{'file':material,'basis':psych['basis'],'data_mode':'synthetic'})
        _write(directory / material,visual_page(p,psych,copy))
        stage(p.name,'communication_specialist',lambda:communication(p,copy))
        def sdr():
            variants = {language:copy[f'offer_{language}'] for language in ('ru','kk') if copy.get(f'offer_{language}')}
            if simulated:
                # Test input may explicitly provide an equivalent translation.
                variants.update(payload.get('voice_variants',{}))
            dossier = VoiceDossier(company=p.name,offer=variants.get(copy['language'],p.offer),language=copy['language'],
                                   product=PRODUCTS[p.product],language_variants=variants,source_urls=copy['basis'])
            from app.integrations.sales_robovoice import prepare_robovoice_handoff
            try:
                preview = prepare_robovoice_handoff(dossier).model_dump(mode='json')
            except ValueError:
                preview = {'mode':'blocked','reason':'Из клиентского сценария требуется убрать контактные данные или ссылки.'}
            if simulated:
                return {**simulate_voice_dialogue(dossier,payload['voice_turns']).model_dump(mode='json'),
                        'robovoice_preview':preview}
            return {'status':'blocked_pending_approval','mode':'handoff_dossier',
                    'dossier':dossier.model_dump(mode='json'),'robovoice_preview':preview}
        sdr_state = stage(p.name,'ai_voice_sdr',sdr)
        if simulated:
            voice.append({'name':p.name,**sdr_state})
        else:
            store.register(run_id,p.name,keys)
        reviewed.append({'name':p.name,'city':p.city,'product':p.product,'contact':' / '.join(q.get('contact_values',[])),
                         'crm_state':q['crm_state'],'fact':p.fact,'sources':' / '.join(q['sources']),
                         'offer':p.offer,'next_step':p.next_step,'permission':q['contact_permission'],
                         'subject':copy['subject'],'email':copy['email'],'call':copy['call'],'language':copy['language'],
                         'visual':material,'copy_mode':copy['mode'],'model_status':copy.get('model_status'),
                         'language_pending':copy.get('language_pending'),'qualification':q})
    checked = stage('','sales_learning_analyst',lambda:learning_evaluation(store.stats()['metrics'],min_sample))
    if voice:
        _write(directory / 'simulation.json',json.dumps(voice,ensure_ascii=False,indent=2))
        trace = [{'role':role,'result':store.step(run_id,candidates[0].name if role!='sales_learning_analyst' else '',role)}
                 for role in ROLES]
        _write(directory / 'workflow.json',json.dumps(trace,ensure_ascii=False,indent=2))
    report = _report(directory,run,reviewed,blocked,checkpoint['sources'],checked,snapshot['audit'],candidates)
    partial = not simulated and any(q['crm_state'] in {'not_checked','unavailable','incomplete'} for q in blocked)
    store.finish(run_id, 'partial_crm' if partial else 'completed')
    return report
