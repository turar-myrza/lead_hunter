"""Targeted READ ONLY relationship collection after the duplicate check.

No free-text classifier is allowed to turn a product row, a won stage, a task, or
an ambiguous identity into a fact about delivery, a refusal, or permission to sell.
The collected, dated references support an explicit local analyst review.
"""
from datetime import datetime, timezone
from html import unescape
import re

from app.integrations.lead_hunter_crm import CRMReadError
from app.models.lead_hunter_relationships import (
    CRMInteraction, CRMProduct, CRMReference, RelationshipEntity, RelationshipFact,
    RelationshipReview,
)


BASE_FIELDS = {
    'company': ['ID', 'TITLE', 'ASSIGNED_BY_ID', 'COMPANY_TYPE', 'COMMENTS', 'DATE_CREATE',
                'DATE_MODIFY', 'LAST_ACTIVITY_TIME'],
    'contact': ['ID', 'NAME', 'LAST_NAME', 'SECOND_NAME', 'COMPANY_ID', 'ASSIGNED_BY_ID',
                'TYPE_ID', 'COMMENTS', 'DATE_CREATE', 'DATE_MODIFY', 'LAST_ACTIVITY_TIME'],
    'lead': ['ID', 'TITLE', 'COMPANY_ID', 'CONTACT_ID', 'ASSIGNED_BY_ID', 'STATUS_ID',
             'STATUS_SEMANTIC_ID', 'STATUS_DESCRIPTION', 'COMMENTS', 'SOURCE_DESCRIPTION',
             'DATE_CREATE', 'DATE_MODIFY', 'LAST_ACTIVITY_TIME', 'DATE_CLOSED'],
    'deal': ['ID', 'TITLE', 'COMPANY_ID', 'CONTACT_ID', 'LEAD_ID', 'ASSIGNED_BY_ID', 'STAGE_ID',
             'STAGE_SEMANTIC_ID', 'CATEGORY_ID', 'COMMENTS', 'DATE_CREATE', 'DATE_MODIFY',
             'LAST_ACTIVITY_TIME', 'CLOSEDATE', 'CLOSED'],
    'quote': ['ID', 'TITLE', 'COMPANY_ID', 'CONTACT_ID', 'DEAL_ID', 'LEAD_ID', 'ASSIGNED_BY_ID',
              'STATUS_ID', 'CONTENT', 'COMMENTS', 'DATE_CREATE', 'DATE_MODIFY', 'CLOSEDATE'],
}
OWNER_TYPES = {'lead': 1, 'deal': 2, 'contact': 3, 'company': 4, 'quote': 7}
UF_LABEL = re.compile(r'отказ|причин|выполн|оказан|проект|услуг|потребност|запрос|'
                      r'не\s*(?:звон|пис|контакт)|запрет|reason|refus|deliver|service|do.?not.?contact', re.I)
SAFE_UF_TYPES = {'string', 'enumeration', 'boolean', 'date', 'datetime', 'integer', 'double'}


def identifier(value):
    return int(value) if value is not None and str(value).isdigit() and int(value) > 0 else None


def crm_date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    # Bitrix sometimes returns a date without timezone; do not invent its zone.
    return parsed if parsed.tzinfo is not None else None


def plain_text(value, limit=4000):
    """Retain a bounded text excerpt, never attachments/recording links or HTML code."""
    if value is None or isinstance(value, (dict, list)):
        return ''
    value = str(value)
    value = re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', '', value, flags=re.I | re.S)
    value = re.sub(r'<[^>]+>', ' ', value)
    value = re.sub(r'\[(?:/?(?:url|img|video|file|disk|audio))[^\]]*\]', ' ', value, flags=re.I)
    value = unescape(value)
    # Do not persist accidental REST credentials, file download tokens, or call recordings.
    value = re.sub(r'https?://\S+/(?:rest|disk/downloadFile|telephony/record)\S*', '[private link omitted]', value, flags=re.I)
    return ' '.join(value.split())[:limit]


def semantics(raw, status=None):
    explicit = raw.get('STAGE_SEMANTIC_ID') or raw.get('STATUS_SEMANTIC_ID')
    if explicit in {'S', 'F', 'P'}:
        return {'S': 'success', 'F': 'failure', 'P': 'process'}[explicit]
    extra = (status or {}).get('EXTRA') or {}
    word = extra.get('SEMANTICS') if isinstance(extra, dict) else None
    if word in {'process', 'success', 'failure', 'apology'}:
        return 'failure' if word == 'apology' else word
    return {'S': 'success', 'F': 'failure'}.get((status or {}).get('SEMANTICS'), 'unknown')


class RelationshipService:
    """Read a bounded, explicit graph, not the entire CRM history.

    Limits are per company, not per CRM portal. A reached limit or denied scope
    makes the review partial. Even complete means only all requested visible
    records were returned; unseen records and attachment contents stay unknown.
    """
    def __init__(self, client, *, max_entities=40, max_deals=100, max_leads=100,
                 max_contacts=30, max_interactions=200, max_quotes=50):
        self.client = client
        self.max_entities = max_entities
        self.max_deals = max_deals
        self.max_leads = max_leads
        self.max_contacts = max_contacts
        self.max_interactions = max_interactions
        self.max_quotes = max_quotes
        self._fields = {}
        self._users = {}
        self._statuses = {}
        self._categories = None

    async def _list(self, method, parameters, limit, review, scope):
        rows = []
        seen = set()
        review.coverage[scope] = False
        try:
            async for page in self.client.pages(method, parameters):
                for raw in page:
                    record_id = identifier(raw.get('ID'))
                    if not record_id or record_id in seen:
                        raise CRMReadError('Invalid or repeated record ID in relationship pagination')
                    seen.add(record_id)
                    if len(rows) == limit:
                        review.limitations.append(f'{scope}: достигнут предел {limit}; история неполная.')
                        return rows
                    rows.append(raw)
            review.coverage[scope] = True
        except CRMReadError as exc:
            review.limitations.append(f'{scope}: {exc}')
        return rows

    async def _custom_fields(self, entity, review):
        if entity not in self._fields:
            try:
                response = await self.client.read(f'crm.{entity}.fields', {})
                fields = {}
                for key, spec in response['result'].items():
                    if not re.fullmatch(r'UF_CRM_[A-Z0-9_]+', key):
                        continue
                    label = ' '.join(str(spec.get(k) or '') for k in
                                     ['title', 'formLabel', 'listLabel', 'filterLabel'])
                    if UF_LABEL.search(label) and spec.get('type') in SAFE_UF_TYPES:
                        fields[key] = spec
                self._fields[entity] = fields
            except CRMReadError:
                self._fields[entity] = None
        fields = self._fields[entity]
        review.coverage[f'{entity}:relationship_field_definitions'] = fields is not None
        if fields is None:
            review.limitations.append(f'{entity}: описания полей отказов/работ недоступны; значения не угадывались.')
        return fields or {}

    async def _owner(self, owner_id, review):
        if not owner_id:
            return None
        if owner_id not in self._users:
            try:
                result = await self.client.read('user.get', {'FILTER': {'ID': owner_id}})
                rows = [r for r in result['result'] if str(r.get('ID')) == str(owner_id)]
                self._users[owner_id] = (plain_text(' '.join(str(rows[0].get(k) or '') for k in ['NAME', 'LAST_NAME']), 200)
                                         if rows else None)
            except CRMReadError:
                self._users[owner_id] = None
        if not self._users[owner_id]:
            review.coverage[f'owner:{owner_id}'] = False
            review.limitations.append(f'Ответственный #{owner_id}: имя недоступно, ID сохранён.')
        return self._users[owner_id]

    async def _status(self, group, code, review):
        if group not in self._statuses:
            try:
                result = await self.client.read('crm.status.list', {'filter': {'ENTITY_ID': group}, 'order': {'SORT': 'ASC'}})
                self._statuses[group] = {str(r.get('STATUS_ID')): r for r in result['result']}
                if result.get('next') not in (None, ''):
                    self._statuses[group] = None
            except CRMReadError:
                self._statuses[group] = None
        values = self._statuses[group]
        review.coverage[f'statuses:{group}'] = values is not None
        if values is None:
            review.limitations.append(f'{group}: справочник стадий/семантики недоступен или неполон.')
        return (values or {}).get(str(code), {})

    async def _category(self, category_id, review):
        if self._categories is None:
            try:
                result = await self.client.read('crm.category.list', {'entityTypeId': 2})
                self._categories = {str(r.get('id')): plain_text(r.get('name'), 200)
                                    for r in result['result']['categories']}
                review.coverage['deal:pipelines'] = result.get('next') in (None, '')
            except CRMReadError:
                self._categories = {}
                review.coverage['deal:pipelines'] = False
        if str(category_id) not in self._categories:
            review.coverage['deal:pipelines'] = False
            review.limitations.append(f'Воронка #{category_id}: название недоступно; стадия интерпретируется с ID воронки.')
        return self._categories.get(str(category_id))

    def _ref(self, method, entity, entity_id, field, value, review, date=None):
        return CRMReference(method=method, entity=entity, id=entity_id, field=field,
                            date=date, date_kind=('event' if method in {'crm.activity.list','crm.timeline.comment.list'} else 'card_modified') if date else 'unknown',
                            quote=plain_text(value) or '[empty]', checked_at=review.checked_at)

    async def _entity(self, entity, raw, identity, review, method=None, via=None):
        record_id = identifier(raw.get('ID'))
        if not record_id:
            raise CRMReadError('Relationship object lacks a valid ID')
        method = method or f'crm.{entity}.list'
        title = plain_text(raw.get('TITLE') or ' '.join(str(raw.get(k) or '') for k in ['NAME', 'LAST_NAME', 'SECOND_NAME']), 400)
        category_id = int(raw.get('CATEGORY_ID') or 0) if entity == 'deal' else None
        group = {'company': 'COMPANY_TYPE', 'contact': 'CONTACT_TYPE', 'lead': 'STATUS',
                 'quote': 'QUOTE_STATUS'}.get(entity)
        if entity == 'deal':
            group = 'DEAL_STAGE' + (f'_{category_id}' if category_id else '')
        code = raw.get('STAGE_ID') or raw.get('STATUS_ID') or raw.get('COMPANY_TYPE') or raw.get('TYPE_ID')
        status = await self._status(group, code, review) if group and code else {}
        owner_id = identifier(raw.get('ASSIGNED_BY_ID'))
        item = RelationshipEntity(entity=entity, id=record_id, title=title, identity=identity,
            company_id=identifier(raw.get('COMPANY_ID')), contact_id=identifier(raw.get('CONTACT_ID')),
            owner_id=owner_id, owner_name=await self._owner(owner_id, review),
            status_code=str(code) if code else None, status_name=plain_text(status.get('NAME')) or None,
            status_semantics=semantics(raw, status), category_id=category_id,
            category_name=await self._category(category_id, review) if entity == 'deal' else None,
            created_at=crm_date(raw.get('DATE_CREATE')), modified_at=crm_date(raw.get('DATE_MODIFY')),
            last_activity_at=crm_date(raw.get('LAST_ACTIVITY_TIME')),
            notes=plain_text(raw.get('COMMENTS')), related_via=[via] if via else [])
        for field in BASE_FIELDS[entity]:
            if field in raw and raw[field] not in (None, '', [], {}):
                value = plain_text(raw[field])
                if value:
                    item.references.append(self._ref(method, entity, record_id, field, value, review,
                                                     item.modified_at))
                if isinstance(raw[field], str) and len(plain_text(raw[field], 100000)) > 4000:
                    review.coverage[f'{entity}:{record_id}:text'] = False
                    review.limitations.append(f'{entity} #{record_id}: {field} сокращено до 4000 символов.')
        # Only identifiers needed to resolve an already found identity are retained.
        # They remain private CRM references and must never become cold-outreach contacts.
        for field in ['PHONE', 'WEB']:
            values = raw.get(field) or []
            if not isinstance(values, list):
                review.coverage[f'{entity}:{record_id}:identifiers'] = False
                review.limitations.append(f'{entity} #{record_id}: неверная форма поля {field}.')
                continue
            for value in values[:20]:
                if isinstance(value, dict) and value.get('VALUE'):
                    item.references.append(self._ref(method, entity, record_id, field,
                                                     value['VALUE'], review, item.modified_at))
        if entity != 'quote':
            for field, spec in (await self._custom_fields(entity, review)).items():
                value = raw.get(field)
                if value in (None, '', [], {}):
                    continue
                values = value if isinstance(value, list) else [value]
                enum = {str(x.get('ID')): str(x.get('VALUE')) for x in (spec.get('items') or []) if isinstance(x, dict)}
                text = '; '.join(enum.get(str(v), plain_text(v)) for v in values if not isinstance(v, (dict, list)))
                if text:
                    label = plain_text(spec.get('title') or spec.get('formLabel') or field, 200)
                    item.custom_fields[field] = f'{label}: {text[:2000]}'
                    item.references.append(self._ref(method, entity, record_id, field, text[:2000], review, item.modified_at))
        review.entities.append(item)
        return item

    async def _products(self, item, review):
        method = f'crm.{item.entity}.productrows.get'
        scope = f'{item.entity}:{item.id}:products'
        try:
            response = await self.client.read(method, {'id': item.id})
            rows = response['result']
            review.coverage[scope] = len(rows) <= 100
            for r in rows[:100]:
                name = plain_text(r.get('PRODUCT_NAME'), 400)
                if name:
                    item.products.append(CRMProduct(id=identifier(r.get('PRODUCT_ID')),
                        name=name, quantity=str(r.get('QUANTITY') or ''),
                        reference=self._ref(method, item.entity, item.id, 'PRODUCT_NAME', name, review)))
            if len(rows) > 100:
                review.limitations.append(f'{scope}: показаны только первые 100 позиций.')
        except CRMReadError as exc:
            review.coverage[scope] = False
            review.limitations.append(f'{scope}: {exc}')

    async def _interactions(self, item, review):
        params = {'filter': {'BINDINGS': [{'OWNER_TYPE_ID': OWNER_TYPES[item.entity], 'OWNER_ID': item.id}]},
                  'order': {'ID': 'DESC'}, 'select': ['ID', 'TYPE_ID', 'SUBJECT', 'CREATED', 'LAST_UPDATED',
                  'START_TIME', 'END_TIME', 'COMPLETED', 'RESPONSIBLE_ID', 'DIRECTION', 'DESCRIPTION', 'DESCRIPTION_TYPE']}
        rows = await self._list('crm.activity.list', params, self.max_interactions, review,
                                f'{item.entity}:{item.id}:activities')
        for r in rows:
            # CREATED/LAST_UPDATED describe a record, not a completed conversation.
            date = crm_date(r.get('START_TIME')) or crm_date(r.get('CREATED'))
            subject = plain_text(r.get('SUBJECT'), 500)
            text = plain_text(r.get('DESCRIPTION'))
            if len(plain_text(r.get('DESCRIPTION'), 100000)) > 4000:
                review.coverage[f'{item.entity}:{item.id}:activity_text'] = False
                review.limitations.append(f'activity #{r["ID"]}: описание сокращено до 4000 символов.')
            item.interactions.append(CRMInteraction(id=int(r['ID']), kind='activity', date=date,
                subject=subject, text=text, completed={'Y': True, 'N': False}.get(r.get('COMPLETED')),
                direction=str(r.get('DIRECTION')) if r.get('DIRECTION') is not None else None,
                author_id=identifier(r.get('RESPONSIBLE_ID')),
                reference=self._ref('crm.activity.list', 'activity', int(r['ID']),
                                    'DESCRIPTION' if text else 'SUBJECT', text or subject or '[empty]', review, date)))
        comments = await self._list('crm.timeline.comment.list', {
            'filter': {'ENTITY_TYPE': item.entity, 'ENTITY_ID': item.id}, 'order': {'ID': 'DESC'},
            'select': ['ID', 'CREATED', 'ENTITY_ID', 'ENTITY_TYPE', 'AUTHOR_ID', 'COMMENT']},
            self.max_interactions, review, f'{item.entity}:{item.id}:comments')
        for r in comments:
            text = plain_text(r.get('COMMENT'))
            date = crm_date(r.get('CREATED'))
            if len(plain_text(r.get('COMMENT'), 100000)) > 4000:
                review.coverage[f'{item.entity}:{item.id}:comment_text'] = False
                review.limitations.append(f'comment #{r["ID"]}: текст сокращён до 4000 символов.')
            item.interactions.append(CRMInteraction(id=int(r['ID']), kind='comment', date=date,
                text=text, author_id=identifier(r.get('AUTHOR_ID')),
                reference=self._ref('crm.timeline.comment.list', 'comment', int(r['ID']), 'COMMENT',
                                    text or '[empty]', review, date)))

    async def _get(self, entity, entity_id, identity, review, via=None):
        if any(e.entity == entity and e.id == entity_id for e in review.entities):
            existing = next(e for e in review.entities if e.entity == entity and e.id == entity_id)
            if via and via not in existing.related_via:
                existing.related_via.append(via)
            if identity == 'confirmed':
                existing.identity = 'confirmed'
            return existing
        if len(review.entities) >= self.max_entities:
            review.coverage['entity_graph'] = False
            review.limitations.append(f'Граф CRM ограничен {self.max_entities} объектами; история неполная.')
            return None
        try:
            result = await self.client.read(f'crm.{entity}.get', {'id': entity_id})
            review.coverage[f'{entity}:{entity_id}:details'] = True
            return await self._entity(entity, result['result'], identity, review,
                                      method=f'crm.{entity}.get', via=via)
        except CRMReadError as exc:
            review.coverage[f'{entity}:{entity_id}:details'] = False
            review.limitations.append(f'{entity} #{entity_id}: {exc}')
            return None

    async def enrich(self, batch):
        for candidate in batch.candidates:
            candidate.relationship_review = await self.review(candidate)
        return batch

    async def review(self, candidate):
        check = candidate.crm_check
        review = RelationshipReview(checked_at=datetime.now(timezone.utc),
            limitations=['Только доступные владельцу READ ONLY вебхука записи; скрытые записи неизвестны.',
                         'Вложения, документы КП, записи звонков и полные чаты не скачивались. Отсутствие текста не доказывает отсутствие договорённостей.',
                         'DATE_MODIFY — изменение карточки, не дата разговора. Выполнение активности не доказывает выполнение услуги.'])
        review.coverage['identity_scan'] = (check.state not in {'not_checked', 'incomplete', 'unavailable'}
                                          and all(check.coverage.get(e) for e in ['company', 'contact', 'lead']))
        if not check.matches:
            if check.state == 'no_match_accessible' and review.coverage['identity_scan']:
                review.status, review.relationship_state = 'complete', 'no_history'
            else:
                review.status = 'unavailable'
                review.limitations.append('Существующая история не проверена: нет полного доступного индекса CRM.')
            return review
        review.identity = 'confirmed' if any(m.confidence == 'confirmed' for m in check.matches) else 'possible'
        review.status = 'partial'
        review.coverage['entity_graph'] = len(check.matches) <= self.max_entities
        # All possible matches remain visibly ambiguous; their history cannot establish a customer fact.
        for match in check.matches[:self.max_entities]:
            if match.entity in {'company', 'contact', 'lead'}:
                await self._get(match.entity, match.id, match.confidence, review)
        # Follow explicit COMPANY_ID/CONTACT_ID links, retaining the source reference.
        pending_index = 0
        while pending_index < len(review.entities):
            item = review.entities[pending_index]
            pending_index += 1
            for entity, linked_id, field in [('company', item.company_id, 'COMPANY_ID'),
                                              ('contact', item.contact_id, 'CONTACT_ID')]:
                if linked_id:
                    via = self._ref(f'crm.{item.entity}.get', item.entity, item.id, field,
                                    linked_id, review, item.modified_at)
                    await self._get(entity, linked_id, item.identity, review, via)
        # Explicit company-contact bindings include secondary linked contacts.
        for company in [e for e in list(review.entities) if e.entity == 'company']:
            scope = f'company:{company.id}:contact_bindings'
            try:
                result = await self.client.read('crm.company.contact.items.get', {'id': company.id})
                bindings = result['result']
                review.coverage[scope] = len(bindings) <= self.max_contacts
                if len(bindings) > self.max_contacts:
                    review.limitations.append(f'{scope}: первые {self.max_contacts} контактов; история неполная.')
                for r in bindings[:self.max_contacts]:
                    linked_id = identifier(r.get('CONTACT_ID'))
                    if linked_id:
                        via = self._ref('crm.company.contact.items.get', 'company', company.id,
                                        'CONTACT_ID', linked_id, review)
                        await self._get('contact', linked_id, company.identity, review, via)
            except CRMReadError as exc:
                review.coverage[scope] = False
                review.limitations.append(f'{scope}: {exc}')
        # Related leads and deals are paginated and merged by ID; there is no 5-row cutoff.
        for entity, limit in [('lead', self.max_leads), ('deal', self.max_deals)]:
            uf = await self._custom_fields(entity, review)
            for parent in list(review.entities):
                if parent.entity not in {'company', 'contact'} and not (parent.entity == 'lead' and entity == 'deal'):
                    continue
                field = {'company': 'COMPANY_ID', 'contact': 'CONTACT_ID', 'lead': 'LEAD_ID'}[parent.entity]
                rows = await self._list(f'crm.{entity}.list', {'filter': {field: parent.id},
                    'select': BASE_FIELDS[entity] + list(uf), 'order': {'ID': 'DESC'}}, limit, review,
                    f'{parent.entity}:{parent.id}:related_{entity}')
                for raw in rows:
                    entity_id = identifier(raw.get('ID'))
                    existing = next((e for e in review.entities if e.entity == entity and e.id == entity_id), None)
                    via = self._ref(f'crm.{entity}.list', entity, entity_id, field, parent.id, review)
                    if existing:
                        if via not in existing.related_via:
                            existing.related_via.append(via)
                        if parent.identity == 'confirmed':
                            existing.identity = 'confirmed'
                        continue
                    if len(review.entities) >= self.max_entities:
                        review.coverage['entity_graph'] = False
                        review.limitations.append(f'Граф CRM ограничен {self.max_entities} объектами; история неполная.')
                        break
                    await self._entity(entity, raw, parent.identity, review, via=via)
        # Read proposal metadata/content and product rows without downloading proposal documents.
        seen_quotes = set()
        for parent in list(review.entities):
            field = {'company': 'COMPANY_ID', 'contact': 'CONTACT_ID', 'lead': 'LEAD_ID', 'deal': 'DEAL_ID'}.get(parent.entity)
            if not field:
                continue
            rows = await self._list('crm.quote.list', {'filter': {field: parent.id},
                'select': BASE_FIELDS['quote'], 'order': {'ID': 'DESC'}}, self.max_quotes, review,
                f'{parent.entity}:{parent.id}:quotes')
            for raw in rows:
                if raw['ID'] in seen_quotes:
                    continue
                seen_quotes.add(raw['ID'])
                if len(review.entities) >= self.max_entities:
                    review.coverage['entity_graph'] = False
                    review.limitations.append('Предел графа CRM достигнут при чтении КП.')
                    break
                via = self._ref('crm.quote.list', 'quote', int(raw['ID']), field, parent.id, review)
                proposal = await self._entity('quote', raw, parent.identity, review, via=via)
                await self._products(proposal, review)
        for item in review.entities:
            if item.entity == 'deal':
                await self._products(item, review)
            await self._interactions(item, review)
        self._summarize(review)
        review.limitations = list(dict.fromkeys(review.limitations))
        return review

    def _summarize(self, review):
        confirmed = [e for e in review.entities if e.identity == 'confirmed']
        review.owner_name = next((e.owner_name for e in confirmed if e.entity == 'company' and e.owner_name),
                                 next((e.owner_name for e in confirmed if e.owner_name), None))
        # Use real dated interactions and LAST_ACTIVITY_TIME, never DATE_MODIFY as interaction.
        dates = [i.date for e in confirmed for i in e.interactions if i.date
                 and i.date <= review.checked_at and (i.kind == 'comment' or i.completed is True)]
        dates += [e.last_activity_at for e in confirmed
                  if e.last_activity_at and e.last_activity_at <= review.checked_at]
        review.last_event_at = max(dates) if dates else None
        # A robot error, campaign note or completed internal task is not a
        # conversation with a customer. Only an explicit semantic review may
        # establish last_interaction_at from a cited communication.
        review.last_interaction_at = None
        review.limitations.append('Последнее доступное CRM-событие не равно последнему содержательному контакту с клиентом; дату разговора требуется подтвердить отдельно.')
        if review.last_interaction_at:
            review.limitations.append('Последнее доступное событие включает завершённые активности, комментарии сотрудников и LAST_ACTIVITY_TIME; это не обязательно последний разговор с клиентом.')
        for entity in review.entities:
            ref = next((r for r in entity.references if r.field in {'STAGE_ID', 'STATUS_ID', 'COMPANY_TYPE', 'TYPE_ID'}), None)
            if ref:
                identity_note = '' if entity.identity == 'confirmed' else ' Возможное совпадение; принадлежность компании не доказана.'
                review.facts.append(RelationshipFact(text=f'{entity.entity} #{entity.id} «{entity.title}»: '
                    f'{entity.status_name or entity.status_code}; семантика {entity.status_semantics}.{identity_note}',
                    reference=ref, confidence='explicit' if entity.identity == 'confirmed' else 'requires_review'))
        if review.identity == 'confirmed':
            # Current company type is only a CRM designation, not proof of delivered scope.
            active = any(e.entity == 'company' and e.status_name and
                         re.fullmatch(r'клиент(?:ы)?|customer(?:s)?', e.status_name.casefold()) for e in confirmed)
            former = any(e.entity == 'company' and e.status_name and
                         re.fullmatch(r'бывши[йе] клиент(?:ы)?|former customer(?:s)?', e.status_name.casefold()) for e in confirmed)
            lost = any(e.entity in {'lead', 'deal'} and e.status_semantics == 'failure' for e in confirmed)
            success = any(e.entity == 'deal' and e.status_semantics == 'success' for e in confirmed)
            review.relationship_state = ('active_customer' if active else 'former_customer' if former
                                         else 'lost_prospect' if lost and not success else 'history_only')
            if success:
                review.limitations.append('Есть успешная сделка: это подтверждает результат стадии, но не состав и факт выполнения работ.')
        else:
            review.relationship_state = 'unknown'
            review.limitations.append('История найденных совпадений не приписывается компании до подтверждения идентичности.')
        # Collection completeness and semantic review are separate: delivery/refusal interpretation
        # requires an analyst, even when every requested page was accessible.
        collection_complete = bool(review.coverage) and all(review.coverage.values())
        review.coverage['collection_complete'] = collection_complete
        review.status = 'partial'
        review.limitations.append('Требуется смысловая проверка доступных текстов: выполненные услуги, отклонённые предложения и новые основания не выводятся из названий/стадий автоматически.')


def reviewed_fact(review, *, text, reference, service_key=None, offer_key=None):
    """Create an explicit analyst fact only from a recorded exact evidence excerpt.

    This helper deliberately does not change identity, completeness, or permission
    to contact. A human/agent must resolve those independently and document it.
    """
    reference = CRMReference.model_validate(reference)
    available = [r for e in review.entities for r in e.references + e.related_via]
    available += [i.reference for e in review.entities for i in e.interactions]
    available += [p.reference for e in review.entities for p in e.products]
    match = any(r.method == reference.method and r.entity == reference.entity and r.id == reference.id
                and r.field == reference.field and reference.quote in r.quote for r in available)
    if not match:
        raise ValueError('Reviewed fact must quote a recorded private CRM reference')
    return RelationshipFact(text=text, reference=reference, service_key=service_key,
                            offer_key=offer_key, confidence='explicit')
