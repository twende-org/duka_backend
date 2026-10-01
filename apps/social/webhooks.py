"""Facebook webhook: subscription handshake + comment auto-reply.

Port of ``functions/src/social/facebookWebhook.js``. Facebook calls the GET
handshake once when the page subscription is configured, then POSTs feed
events. A new comment on one of the shop's posts is answered by an AI reply
that funnels the customer to WhatsApp.

Two deliberate deviations from the Cloud Function, both documented in
``config/settings.py``:

- POST payloads are validated against ``X-Hub-Signature-256`` whenever
  ``FACEBOOK_APP_SECRET`` is configured (the legacy function accepted any
  caller, which let third parties trigger AI spend and page replies);
- the verify token is configurable via ``FACEBOOK_WEBHOOK_VERIFY_TOKEN`` and
  defaults to the legacy literal so existing subscriptions keep working.
"""
import hashlib
import hmac
import logging
import re

import requests
from django.conf import settings

# pyrefly: ignore [missing-import]
from apps.core.crypto import decrypt_token
# pyrefly: ignore [missing-import]
from apps.social.models import SocialIntegration
# pyrefly: ignore [missing-import]
from apps.social.services import graph_url

logger = logging.getLogger(__name__)

DEFAULT_VERIFY_TOKEN = 'biashara-connect-webhook-secret-2024'
FALLBACK_REPLY = 'Asante kwa ujumbe wako! Tutawasiliana nawe hivi punde.'
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
OPENROUTER_MODEL = 'google/gemini-2.5-flash-lite'
AI_TIMEOUT = 15
GRAPH_TIMEOUT = 30


def verify_token():
    return getattr(settings, 'FACEBOOK_WEBHOOK_VERIFY_TOKEN', '') or DEFAULT_VERIFY_TOKEN


def signature_is_valid(raw_body, header):
    """True when the payload may be trusted.

    With no app secret configured the check is skipped (legacy parity: the
    Cloud Function never verified); otherwise ``X-Hub-Signature-256`` must
    match the HMAC of the raw body.
    """
    secret = getattr(settings, 'FACEBOOK_APP_SECRET', '') or ''
    if not secret or not getattr(settings, 'FACEBOOK_WEBHOOK_VERIFY_SIGNATURE', True):
        return True
    if not header:
        return False
    expected = 'sha256=' + hmac.new(secret.encode('utf-8'), raw_body or b'', hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(header))


def verify_subscription(params):
    """Answer the ``hub.*`` GET handshake: ``(status, body)``."""
    mode = params.get('hub.mode')
    token = params.get('hub.verify_token')
    challenge = params.get('hub.challenge')
    if not mode or not token:
        return 400, 'Bad Request'
    if mode == 'subscribe' and token == verify_token():
        logger.info('Facebook webhook verified.')
        return 200, challenge or ''
    logger.warning('Facebook webhook verification rejected (mode=%s).', mode)
    return 403, 'Forbidden'


def process_event(payload):
    """Handle one webhook POST body; returns the HTTP status to answer with."""
    if not isinstance(payload, dict) or payload.get('object') != 'page':
        return 404
    try:
        for entry in payload.get('entry') or []:
            if not isinstance(entry, dict):
                continue
            page_id = entry.get('id')
            for change in entry.get('changes') or []:
                if not isinstance(change, dict):
                    continue
                value = change.get('value') or {}
                if not (
                    change.get('field') == 'feed'
                    and value.get('item') == 'comment'
                    and value.get('verb') == 'add'
                ):
                    continue
                sender_id = (value.get('from') or {}).get('id')
                if sender_id and sender_id == page_id:
                    # Our own reply shows up as a comment on the page: never
                    # answer ourselves or the two sides loop forever.
                    continue
                handle_new_comment(page_id, value.get('comment_id'), value.get('message') or '')
    except Exception:
        logger.exception('Facebook webhook event processing failed.')
        return 500
    return 200


def handle_new_comment(page_id, comment_id, message):
    """Generate and post the auto-reply. Platform failures are never raised."""
    integration = SocialIntegration.objects.filter(page_id=str(page_id)).first()
    if integration is None:
        logger.info('Facebook webhook: unregistered page %s.', page_id)
        return None
    if integration.auto_reply_enabled is not True:
        logger.info('Facebook webhook: auto-reply disabled for shop %s.', integration.shop_id)
        return None
    if not comment_id:
        return None
    access_token = decrypt_token(integration.access_token)
    if not access_token:
        logger.warning('Facebook webhook: shop %s has no page token.', integration.shop_id)
        return None

    reply_text = generate_auto_reply(integration.shop, message)
    _post_reply(comment_id, access_token, reply_text)
    return reply_text


def whatsapp_link(shop):
    digits = re.sub(r'\D', '', str(shop.whatsapp or shop.phone or ''))
    return f'https://wa.me/{digits}' if digits else 'DM us!'


def generate_auto_reply(shop, message):
    """Short sales-driven Swahili reply, or the legacy fallback text."""
    api_key = getattr(settings, 'OPENROUTER_API_KEY', '')
    if not api_key:
        return FALLBACK_REPLY

    prompt = f'''Wewe ni mhudumu mzuri wa wateja kwa duka la mtandaoni linaitwa {shop.name}.
Mteja ametoa comment kwenye post ya bidhaa yako Facebook/Instagram.

Comment ya Mteja: "{message}"

Andika reply (jibu) fupi sana, ya kirafiki, na inayoleta mauzo (sales-driven). Jibu kwa Kiswahili.
Usimsalimie (e.g., avoid "Hujambo"). Nenda moja kwa moja kwenye jibu.
Mwishoni, waelekeze kuwasiliana WhatsApp hapa: {whatsapp_link(shop)}

Jibu na MANENO YA REPLY PEKEE. Usimweke "Here is the reply" au alama za nukuu.'''

    try:
        response = requests.post(
            OPENROUTER_URL,
            json={
                'model': OPENROUTER_MODEL,
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.7,
                'max_tokens': 150,
            },
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            timeout=AI_TIMEOUT,
        )
        response.raise_for_status()
        choices = response.json().get('choices') or []
        reply = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
        return reply or FALLBACK_REPLY
    except (requests.RequestException, ValueError, AttributeError, IndexError) as exc:
        logger.warning('Facebook webhook: AI reply failed for shop %s: %s', shop.pk, exc)
        return FALLBACK_REPLY


def _post_reply(comment_id, access_token, message):
    try:
        response = requests.post(
            graph_url(f'{comment_id}/comments'),
            data={'message': message, 'access_token': access_token},
            timeout=GRAPH_TIMEOUT,
        )
        response.raise_for_status()
        logger.info('Facebook webhook: replied to comment %s.', comment_id)
        return True
    except requests.RequestException as exc:
        logger.warning('Facebook webhook: reply to comment %s failed: %s', comment_id, exc)
        return False
