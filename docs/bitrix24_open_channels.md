# Bitrix24 Open Channels in LeadHunter

The adapter uses existing Bitrix24 conversations. It has no channel registration, reconnection, transfer, session creation, or CRM record write methods.

## Access

In Bitrix24, open **Developer resources → Common use cases → Other → Inbound webhook → Assign permissions** and select **Open Channels (`imopenlines`)**. Use an operator with access to the lines, dialog and CRM item. Store the secret base URL in `BITRIX24_OPEN_CHANNELS_WEBHOOK_URL` via local environment or the hosting secret store. The existing CRM webhook is a read-only fallback for Open Channels. See [Bitrix24 webhooks](https://apidocs.bitrix24.com/local-integrations/local-webhooks.html) and [REST scopes](https://apidocs.bitrix24.com/api-reference/scopes/permissions.html).

Connector status is distinct from line activity. `imconnector.status` requires application context. If an existing authorized Bitrix24 application provides an OAuth access token on this portal, set `BITRIX24_OPEN_CHANNELS_APP_TOKEN` for status calls only. Refresh it through that application's credential management. When unavailable, the API returns null connector status and a reason. Inspect **Contact Center** in Bitrix24 as the fallback. See [line list](https://apidocs.bitrix24.com/api-reference/imopenlines/openlines/imopenlines-config-list-get.html) and [connector status](https://apidocs.bitrix24.com/api-reference/imopenlines/imconnector/imconnector-status.html).

## Operator API

All routes use the existing LeadHunter bearer token under `/api/v1/lead-hunter`. Get `/open-channels/inventory` for Telegram, Instagram Direct and Facebook status per line. For a prospect in a completed or partial non-fixture run, use `/runs/{run_id}/prospects/{prospect_id}/open-channel`:

| Action | Behavior |
|---|---|
| `POST` | Supply `crm_type`, `crm_id`, and `chat_id` after confirming the prospect's identity. Bitrix24 verifies the CRM item/chat association and connector type. The local association is immutable. |
| `GET /history` | Read available text history for the latest session. Attachments are excluded. |
| `POST /poll` | Return incoming messages from Bitrix24 connector participants and deduplicate receipts locally by prospect, chat and message ID. |
| `GET /receipts` | Retrieve the latest 100 receipt records. Message text stays in Bitrix24 unless copied into a local draft. |
| `POST /drafts` | Save operator-written `text` after seeing an inbound message. Review it against the current conversation and any opt-out. |
| `POST /drafts/{draft_id}/send` | Requires `{ "approved": true }`, an active conversation with the same session, and test send authorization. |

`OnImConnectorMessageAdd` is a custom connector outbound event. It does not fire for inbound messages or built-in connectors. Run polling manually or schedule it on an authorized host for linked conversations. No listener or scheduler is installed. The latest-session view can miss replies if sessions turn over between polls. See [event behavior](https://apidocs.bitrix24.com/api-reference/imopenlines/imconnector/events/on-im-connector-message-add.html) and [session history](https://apidocs.bitrix24.com/api-reference/imopenlines/openlines/sessions/imopenlines-session-history-get.html).

Test sending defaults to off. To verify on the portal, use an active, explicitly authorized internal test conversation linked to a CRM item. The webhook user must be a chat participant with access to that item. Set `BITRIX24_OPEN_CHANNELS_TEST_SEND=true` and `BITRIX24_OPEN_CHANNELS_TEST_CHAT_IDS=[123]` using the confirmed test ID and the dedicated webhook. A returned message ID means Bitrix24 accepted the message. Verify delivery at the test recipient. A draft is attempted at most once. On an ambiguous timeout, check the chat before drafting another attempt. See [reply method](https://apidocs.bitrix24.com/api-reference/imopenlines/openlines/messages/imopenlines-crm-message-add.html).

Public-post, profile, and social-network search remain separate capabilities.
