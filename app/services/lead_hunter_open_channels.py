"""Local prospect associations and review-only handling for connected conversations."""

import json
from uuid import uuid4

from app.integrations.bitrix24_open_channels import CONNECTORS, OpenChannelsClient, OpenChannelsError, positive_id


def client_from_settings(settings):
    dedicated = settings.bitrix24_open_channels_webhook_url
    credential = dedicated or settings.bitrix24_webhook_url
    if not credential or not credential.get_secret_value().strip():
        raise OpenChannelsError('credential_not_configured')
    app_token = settings.bitrix24_open_channels_app_token
    test_ids = settings.bitrix24_open_channels_test_chat_ids if dedicated and settings.bitrix24_open_channels_test_send else ()
    return OpenChannelsClient(credential.get_secret_value(), app_token=app_token.get_secret_value() if app_token else None,
                              send_chat_ids=test_ids, timeout=settings.bitrix24_timeout_seconds)


def rows(value):
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise OpenChannelsError('unexpected_response')
    return value


async def inventory(client):
    lines, seen = [], set()
    for offset in range(0, 500, 50):
        page = rows(await client.read('imopenlines.config.list.get', {'PARAMS': {
            'select': ['ID', 'LINE_NAME', 'ACTIVE'], 'order': {'ID': 'ASC'}, 'limit': 50, 'offset': offset}}))
        for line in page:
            line_id = positive_id(line.get('ID'))
            if line_id in seen or line.get('ACTIVE') not in {'Y', 'N'} or not isinstance(line.get('LINE_NAME'), str):
                raise OpenChannelsError('unexpected_response')
            seen.add(line_id)
            lines.append({'line_id': line_id, 'line_name': line['LINE_NAME'], 'line_active': line['ACTIVE'] == 'Y'})
        if len(page) < 50:
            break
    result = []
    for line in lines:
        for connector, kind in CONNECTORS.items():
            active, configured, reason = None, None, None
            try:
                status = await client.read('imconnector.status', {'LINE': line['line_id'], 'CONNECTOR': connector})
                if (not isinstance(status, dict) or positive_id(status.get('LINE')) != line['line_id']
                        or status.get('CONNECTOR') != connector or type(status.get('STATUS')) is not bool
                        or type(status.get('CONFIGURED')) is not bool):
                    raise OpenChannelsError('unexpected_response')
                configured, active = status['CONFIGURED'], status['STATUS'] and line['line_active']
            except OpenChannelsError as exc:
                reason = exc.code
            result.append({**line, 'connector_type': kind, 'configured': configured, 'active': active, 'reason': reason})
    return {'lines_complete': len(lines) < 500, 'connections': result, 'incoming_mode': 'history_polling'}


class ConversationService:
    def __init__(self, application, client, run_id, prospect_id):
        self.store, self.client = application.store, client
        self.run_id, self.prospect_id = run_id, prospect_id
        result = application.result(run_id)
        opportunity = next((row for row in result.opportunities if row.candidate.id == prospect_id), None) if result else None
        if opportunity is None:
            raise OpenChannelsError('prospect_not_found')
        if result.mode == 'fixture':
            raise OpenChannelsError('fixture_cannot_use_live_channels')
        self._blocked = opportunity.engagement.permission == 'blocked' or opportunity.qualification.state == 'excluded'

    def _link(self):
        link = self.store.step(self.run_id, self.prospect_id, 'open_channels_link')
        if link is None:
            raise OpenChannelsError('conversation_not_linked')
        return link

    async def _check_chat(self, link, *, active_only=False):
        chats = rows(await self.client.read('imopenlines.crm.chat.get', {
            **link['target'], 'ACTIVE_ONLY': 'Y' if active_only else 'N'}))
        matches = [row for row in chats if positive_id(row.get('CHAT_ID')) == link['chat_id']]
        if len(matches) != 1 or matches[0].get('CONNECTOR_ID') not in CONNECTORS:
            raise OpenChannelsError('conversation_not_available')
        return matches[0]['CONNECTOR_ID']

    async def link(self, crm_type, crm_id, chat_id):
        proposed = {'chat_id': chat_id, 'target': {'CRM_ENTITY_TYPE': crm_type, 'CRM_ENTITY': crm_id}}
        proposed['connector'] = await self._check_chat(proposed)
        self.store.save(self.run_id, self.prospect_id, 'open_channels_link', proposed)
        if self._link() != proposed:
            raise OpenChannelsError('conversation_already_linked')
        return proposed

    async def history(self):
        link = self._link()
        if await self._check_chat(link) != link['connector']:
            raise OpenChannelsError('conversation_not_available')
        return await self._history(link)

    async def _history(self, link):
        data = await self.client.read('imopenlines.session.history.get', {'CHAT_ID': link['chat_id']})
        if not isinstance(data, dict) or positive_id(data.get('chatId')) != link['chat_id']:
            raise OpenChannelsError('unexpected_response')
        users, messages = data.get('users'), data.get('message')
        if users == []:
            users = {}
        if messages == []:
            messages = {}
        if not isinstance(users, dict) or not isinstance(messages, dict):
            raise OpenChannelsError('unexpected_response')
        result = []
        for key, item in messages.items():
            if not isinstance(item, dict) or positive_id(key) != positive_id(item.get('id')):
                raise OpenChannelsError('unexpected_response')
            if positive_id(item.get('chatid')) != link['chat_id']:
                raise OpenChannelsError('unexpected_response')
            sender = users.get(str(item.get('senderid')))
            inbound = isinstance(sender, dict) and sender.get('connector') is True
            result.append({'id': positive_id(key), 'incoming': inbound,
                           'at': item.get('date') if isinstance(item.get('date'), str) else None,
                           'text': item.get('text') if isinstance(item.get('text'), str) else ''})
        return {'chat_id': link['chat_id'], 'session_id': positive_id(data.get('sessionId')),
                'messages': sorted(result, key=lambda message: message['id'])}

    async def poll(self):
        history = await self.history()
        new = []
        for message in history['messages']:
            if message['incoming'] and self.store.save(self.run_id, self.prospect_id,
                    f"open_incoming_{history['chat_id']}_{message['id']}",
                    {'message_id': message['id'], 'session_id': history['session_id'], 'at': message['at']}):
                new.append(message)
        return {'mode': 'history_polling', 'incoming': new, 'next_action': 'Review incoming replies in the operator queue.'}

    def receipts(self):
        with self.store.connect() as db:
            records = db.execute("SELECT value FROM step WHERE run_id=? AND subject=? AND role GLOB 'open_incoming_*' "
                                 'ORDER BY rowid DESC LIMIT 100', (self.run_id, self.prospect_id))
            return [json.loads(row['value']) for row in records]

    async def draft(self, text):
        if self._blocked:
            raise OpenChannelsError('engagement_blocked')
        history = await self.history()
        if not any(item['incoming'] for item in history['messages']):
            raise OpenChannelsError('no_incoming_message')
        draft = {'id': uuid4().hex, 'text': text, 'session_id': history['session_id'], 'status': 'draft'}
        self.store.save(self.run_id, self.prospect_id, f"open_draft_{draft['id']}", draft)
        return draft

    async def send(self, draft_id):
        if self._blocked:
            raise OpenChannelsError('engagement_blocked')
        draft = self.store.step(self.run_id, self.prospect_id, f'open_draft_{draft_id}')
        if draft is None:
            raise OpenChannelsError('draft_not_found')
        link = self._link()
        self.client.require_send(link['chat_id'])
        if await self._check_chat(link, active_only=True) != link['connector']:
            raise OpenChannelsError('conversation_not_available')
        if (await self._history(link))['session_id'] != draft['session_id']:
            raise OpenChannelsError('session_changed')
        if not self.store.save(self.run_id, self.prospect_id, f'open_attempt_{draft_id}', {'status': 'attempted'}):
            raise OpenChannelsError('draft_already_attempted')
        message_id = await self.client.reply(chat_id=link['chat_id'], target=link['target'], message=draft['text'])
        self.store.save(self.run_id, self.prospect_id, f'open_sent_{draft_id}', {'message_id': message_id})
        return {'message_id': message_id, 'status': 'accepted_by_bitrix24'}
