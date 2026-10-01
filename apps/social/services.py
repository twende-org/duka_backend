"""Facebook OAuth flow: Graph calls, session handoff, token storage.

Mirrors ``functions/src/social/facebookAuth.js`` so the frontend keeps getting
the same payload shapes (bare pages array, ``{success, instagramLinked}``) and
the same user-visible error strings.
"""
import base64
import json
import secrets
from urllib.parse import urlparse

import requests
from django.conf import settings

# pyrefly: ignore [missing-import]
from apps.core.crypto import decrypt_token, encrypt_token
# pyrefly: ignore [missing-import]
from apps.core.legacy import resolve_legacy_pk
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.shops.permissions import MANAGEMENT_ROLES, assert_shop_access
# pyrefly: ignore [missing-import]
from apps.social.models import FacebookOAuthSession, SocialIntegration

DEFAULT_ORIGIN = 'https://duka.twendedigital.tech'
DEFAULT_RETURN_PATH = '/dashboard/shops'
SESSION_EXPIRED_MESSAGE = 'Session expired or invalid. Please reconnect.'
PAGE_NOT_IN_SESSION_MESSAGE = 'Selected page not found in session.'
LOCALHOST_HOSTS = ('localhost', '127.0.0.1')


class FacebookGraphError(Exception):
    """A Graph API call failed or returned an unusable payload."""


class FacebookSessionError(Exception):
    """The OAuth session is missing, expired, or lacks the selected page."""


def graph_url(path):
    base = getattr(settings, 'FACEBOOK_GRAPH_BASE_URL', 'https://graph.facebook.com').rstrip('/')
    version = getattr(settings, 'FACEBOOK_GRAPH_VERSION', 'v18.0')
    return f'{base}/{version}/{str(path).lstrip("/")}'


def _graph_get(path, params):
    try:
        response = requests.get(graph_url(path), params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise FacebookGraphError(str(exc)) from exc
    except ValueError as exc:
        raise FacebookGraphError('Invalid JSON from Facebook.') from exc
    if not isinstance(payload, dict):
        raise FacebookGraphError('Unexpected response from Facebook.')
    return payload


def _allowed_origins():
    configured = getattr(settings, 'FACEBOOK_ALLOWED_ORIGINS', None) or []
    allowed = {DEFAULT_ORIGIN.rstrip('/')}
    allowed.update(str(origin).rstrip('/') for origin in configured)
    return allowed


def _safe_origin(origin):
    """Never redirect the OAuth handoff to an origin we do not control.

    The callback is unauthenticated, so a crafted ``state`` could otherwise
    bounce the session id to an attacker's site (open redirect -> token theft).
    """
    parsed = urlparse(str(origin or ''))
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return DEFAULT_ORIGIN
    normalized = f'{parsed.scheme}://{parsed.netloc}'
    host = parsed.hostname.lower()
    if host in LOCALHOST_HOSTS or host.endswith('.localhost'):
        return normalized
    return normalized if normalized in _allowed_origins() else DEFAULT_ORIGIN


def _safe_return_path(return_path):
    if not isinstance(return_path, str) or not return_path.startswith('/'):
        return DEFAULT_RETURN_PATH
    if return_path.startswith('//') or '://' in return_path or '\\' in return_path:
        return DEFAULT_RETURN_PATH
    return return_path


def parse_oauth_state(state):
    """Decode ``state``: base64 JSON with a shopId, or a raw shop id (legacy)."""
    shop_ref = state
    origin = DEFAULT_ORIGIN
    return_path = DEFAULT_RETURN_PATH
    if state:
        try:
            decoded = base64.b64decode(str(state) + '=' * (-len(str(state)) % 4))
            state_obj = json.loads(decoded.decode('utf-8'))
            if isinstance(state_obj, dict) and state_obj.get('shopId'):
                shop_ref = str(state_obj['shopId'])
                origin = state_obj.get('origin') or origin
                return_path = state_obj.get('returnPath') or return_path
        except Exception:
            pass
    return {
        'shop_id': shop_ref,
        'origin': _safe_origin(origin),
        'return_path': _safe_return_path(return_path),
    }


def exchange_code_for_long_lived_token(code):
    payload = _graph_get('oauth/access_token', {
        'client_id': settings.FACEBOOK_APP_ID,
        'client_secret': settings.FACEBOOK_APP_SECRET,
        'redirect_uri': settings.FACEBOOK_REDIRECT_URI,
        'code': code,
    })
    short_lived = payload.get('access_token')
    if not short_lived:
        raise FacebookGraphError('No access token in Facebook response.')
    payload = _graph_get('oauth/access_token', {
        'grant_type': 'fb_exchange_token',
        'client_id': settings.FACEBOOK_APP_ID,
        'client_secret': settings.FACEBOOK_APP_SECRET,
        'fb_exchange_token': short_lived,
    })
    long_lived = payload.get('access_token')
    if not long_lived:
        raise FacebookGraphError('No long-lived token in Facebook response.')
    return long_lived


def fetch_user_pages(user_token):
    payload = _graph_get('me/accounts', {'access_token': user_token})
    pages = payload.get('data')
    if not isinstance(pages, list):
        raise FacebookGraphError('Unexpected pages payload from Facebook.')
    return pages


def create_oauth_session(shop_ref, user_token, pages):
    shop = None
    if shop_ref:
        pk = resolve_legacy_pk(Shop, shop_ref)
        if pk is not None:
            shop = Shop.objects.filter(pk=pk).first()
    session = FacebookOAuthSession.objects.create(
        id=secrets.token_hex(16),
        shop=shop,
        user_token=encrypt_token(user_token),
        pages=pages,
    )
    return session.id


def get_session_safe_pages(session_id):
    session = FacebookOAuthSession.objects.filter(id=session_id).first()
    if session is None:
        raise FacebookSessionError('Session expired or invalid. Please connect again.')
    return [
        {'id': page.get('id'), 'name': page.get('name'), 'category': page.get('category')}
        for page in (session.pages or [])
        if isinstance(page, dict)
    ]


def _fetch_instagram_id(page_id, page_token):
    """Best effort: the page may have no Instagram business account linked."""
    if not page_token:
        return None
    try:
        payload = _graph_get(str(page_id), {
            'fields': 'instagram_business_account',
            'access_token': page_token,
        })
    except FacebookGraphError:
        return None
    account = payload.get('instagram_business_account') or {}
    return account.get('id') or None


def save_page_connection(user, shop_ref, page_id, session_id):
    """Attach the picked page to the shop, mirroring saveFacebookConnection."""
    shop_pk = resolve_legacy_pk(Shop, shop_ref)
    if shop_pk is None:
        raise FacebookSessionError(SESSION_EXPIRED_MESSAGE)
    shop = Shop.objects.filter(pk=shop_pk).first()
    assert_shop_access(user, shop.id, roles=MANAGEMENT_ROLES)

    session = FacebookOAuthSession.objects.filter(id=session_id).first()
    if session is None:
        raise FacebookSessionError(SESSION_EXPIRED_MESSAGE)
    selected = next(
        (page for page in (session.pages or [])
         if isinstance(page, dict) and str(page.get('id')) == str(page_id)),
        None,
    )
    if selected is None:
        raise FacebookSessionError(PAGE_NOT_IN_SESSION_MESSAGE)

    page_token = selected.get('access_token')
    instagram_id = _fetch_instagram_id(page_id, page_token)
    connected_by = getattr(user, 'firebase_uid', None) or str(user.pk)

    SocialIntegration.objects.update_or_create(
        shop=shop,
        platform='facebook',
        defaults={
            'is_connected': True,
            'page_id': str(page_id),
            'page_name': selected.get('name'),
            'instagram_id': instagram_id,
            'connected_by': connected_by,
            'access_token': encrypt_token(page_token),
            # Re-encrypt so the stored value is ciphertext regardless of how
            # the session row was created (legacy rows held it in plaintext).
            'refresh_token': encrypt_token(decrypt_token(session.user_token)),
            'sync_status': 'idle',
        },
    )
    session.delete()
    return {'success': True, 'instagramLinked': bool(instagram_id)}
