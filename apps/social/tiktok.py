"""TikTok OAuth flow + Content Posting API helpers.

Mirrors the Facebook handshake in ``services.py`` (state handoff, encrypted
session row, ``save*Connection``) but talks to ``open.tiktokapis.com/v2``:

- token exchange is a form-encoded POST and answers with an envelope whose
  ``error.code`` is the string ``'ok'`` on success (not an HTTP status alone);
- Login Kit v2 mandates PKCE: the authorize URL carries the S256
  ``code_challenge`` and the exchange needs the matching ``code_verifier``,
  which the frontend round-trips through the OAuth state;
- user "pages" are a single account blob, so the shared
  ``FacebookOAuthSession`` row parks the user blob in ``pages`` and the token
  pair (JSON, encrypted) in ``user_token`` — no new table;
- access tokens live ~24 h, so the posting path refreshes against
  ``SocialIntegration.token_expires_at`` before every publish.

Tests patch ``apps.social.tiktok.requests.post`` / ``.request`` the same way
the Facebook suite patches ``apps.social.services.requests.get``.
"""
import json
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

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

logger = logging.getLogger(__name__)

TIKTOK_API_BASE = 'https://open.tiktokapis.com/v2'
DEFAULT_PRIVACY_LEVEL = 'PUBLIC_TO_EVERYONE'
# Privacy values TikTok accepts for DIRECT_POST video publishing.
ALLOWED_PRIVACY_LEVELS = {
    'PUBLIC_TO_EVERYONE',
    'MUTUAL_FOLLOW_FRIENDS',
    'SELF_ONLY',
}
TIKTOK_TITLE_MAX_LENGTH = 2200
TOKEN_EXPIRY_SKEW = timedelta(minutes=5)
SESSION_EXPIRED_MESSAGE = 'Session expired or invalid. Please reconnect.'
# Scopes the app registration asks for: basic profile + video upload/publish.
SCOPES = ('user.info.basic', 'video.upload', 'video.publish')
USER_INFO_FIELDS = ('open_id', 'username', 'display_name', 'avatar_url')

TOKEN_TIMEOUT = 20


class TikTokApiError(Exception):
    """A TikTok API call failed or returned an unusable payload."""


class TikTokSessionError(Exception):
    """The OAuth session is missing or expired."""


def tiktok_api_url(path):
    return f'{TIKTOK_API_BASE}/{str(path).lstrip("/")}'


def _raise_for_error(payload, context):
    """TikTok answers 200 with an ``error`` envelope; ``code == 'ok'`` is success."""
    error = payload.get('error') if isinstance(payload, dict) else None
    code = error.get('code') if isinstance(error, dict) else None
    if code in (None, 'ok'):
        return
    message = (error or {}).get('message') or 'Unknown TikTok error.'
    raise TikTokApiError(f'{context}: {message}')


def _post_form(path, form):
    try:
        response = requests.post(tiktok_api_url(path), data=form, timeout=TOKEN_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise TikTokApiError(str(exc)) from exc
    except ValueError as exc:
        raise TikTokApiError('Invalid JSON from TikTok.') from exc
    if not isinstance(payload, dict):
        raise TikTokApiError('Unexpected response from TikTok.')
    return payload


def _request_json(method, path, access_token, body=None, params=None):
    headers = {'Authorization': f'Bearer {access_token}'}
    try:
        response = requests.request(
            method, tiktok_api_url(path), json=body, params=params,
            headers=headers, timeout=TOKEN_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise TikTokApiError(str(exc)) from exc
    except ValueError as exc:
        raise TikTokApiError('Invalid JSON from TikTok.') from exc
    if not isinstance(payload, dict):
        raise TikTokApiError('Unexpected response from TikTok.')
    return payload


def exchange_code_for_token(code, code_verifier=''):
    """Exchange the authorize code; ``code_verifier`` completes the PKCE pair."""
    form = {
        'client_key': getattr(settings, 'TIKTOK_CLIENT_KEY', ''),
        'client_secret': getattr(settings, 'TIKTOK_CLIENT_SECRET', ''),
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': getattr(settings, 'TIKTOK_REDIRECT_URI', ''),
    }
    if code_verifier:
        form['code_verifier'] = code_verifier
    payload = _post_form('oauth/token/', form)
    _raise_for_error(payload, 'Token exchange failed')
    data = payload.get('data') or {}
    access_token = data.get('access_token')
    if not access_token:
        logger.error('[TikTok] token response: %s', payload)
        raise TikTokApiError('No access token in TikTok response.')
    return data


def refresh_access_token(refresh_token):
    payload = _post_form('oauth/token/', {
        'client_key': getattr(settings, 'TIKTOK_CLIENT_KEY', ''),
        'client_secret': getattr(settings, 'TIKTOK_CLIENT_SECRET', ''),
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
    })
    _raise_for_error(payload, 'Token refresh failed')
    data = payload.get('data') or {}
    access_token = data.get('access_token')
    if not access_token:
        raise TikTokApiError('No access token in TikTok refresh response.')
    return data


def fetch_tiktok_user(access_token):
    payload = _request_json(
        'GET', 'user/info/', access_token,
        params={'fields': ','.join(USER_INFO_FIELDS)},
    )
    _raise_for_error(payload, 'User info failed')
    user = (payload.get('data') or {}).get('user') or {}
    return {key: user.get(key) for key in USER_INFO_FIELDS}


def fetch_creator_info(access_token):
    """``post/publish/creator_info/query/`` — the posting capabilities of the account.

    The Content Sharing Guidelines make the client render the post page from
    this payload: the creator's nickname, the privacy levels they may pick
    from, which interactions are disabled for them, and the longest video the
    account may post. Returned camelCase for direct serialization.
    """
    payload = _request_json('POST', 'post/publish/creator_info/query/', access_token, body={})
    _raise_for_error(payload, 'Creator info failed')
    data = payload.get('data') or {}
    options = data.get('privacy_level_options')
    return {
        'username': data.get('creator_username') or '',
        'avatarUrl': data.get('creator_avatar_url') or '',
        'privacyLevelOptions': [str(option) for option in options] if isinstance(options, list) else [],
        'commentDisabled': bool(data.get('comment_disabled')),
        'duetDisabled': bool(data.get('duet_disabled')),
        'stitchDisabled': bool(data.get('stitch_disabled')),
        'maxVideoPostDurationSec': data.get('max_video_post_duration_sec') or 0,
    }


def fetch_publish_status(access_token, publish_ids):
    """``post/publish/status/fetch/`` — normalized list of ``{publishId, status, failReason}``."""
    ids = [str(publish_id) for publish_id in publish_ids if publish_id]
    payload = _request_json(
        'POST', 'post/publish/status/fetch/', access_token, body={'publish_ids': ids},
    )
    _raise_for_error(payload, 'Publish status failed')
    rows = (payload.get('data') or {}).get('status_list') or []
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized.append({
            'publishId': row.get('publish_id') or '',
            'status': row.get('status') or '',
            'failReason': row.get('fail_reason') or '',
        })
    return normalized


def _session_user_blob(user):
    """Sanitized account blob stored in the shared session row's ``pages``."""
    return {
        'open_id': user.get('open_id'),
        'username': user.get('username'),
        'display_name': user.get('display_name'),
        'avatar_url': user.get('avatar_url'),
    }


def create_tiktok_session(shop_ref, tokens, user):
    shop = None
    if shop_ref:
        pk = resolve_legacy_pk(Shop, shop_ref)
        if pk is not None:
            shop = Shop.objects.filter(pk=pk).first()
    session = FacebookOAuthSession.objects.create(
        id=secrets.token_hex(16),
        shop=shop,
        user_token=encrypt_token(json.dumps({
            'access_token': tokens.get('access_token'),
            'refresh_token': tokens.get('refresh_token'),
        })),
        pages=[_session_user_blob(user)],
    )
    return session.id


def _session_tokens(session):
    try:
        blob = json.loads(decrypt_token(session.user_token) or '{}')
    except (TypeError, ValueError):
        blob = {}
    return blob if isinstance(blob, dict) else {}


def get_tiktok_session_user(session_id):
    session = FacebookOAuthSession.objects.filter(id=session_id).first()
    if session is None:
        raise TikTokSessionError('Session expired or invalid. Please connect again.')
    for entry in (session.pages or []):
        if isinstance(entry, dict) and entry.get('open_id'):
            return {
                'openId': entry.get('open_id'),
                'username': entry.get('username'),
                'displayName': entry.get('display_name'),
                'avatarUrl': entry.get('avatar_url'),
            }
    raise TikTokSessionError('Session expired or invalid. Please connect again.')


def _token_expiry(expires_in):
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 0
    return timezone.now() + timedelta(seconds=seconds)


def save_tiktok_connection(user, shop_ref, session_id):
    """Attach the TikTok account to the shop, mirroring ``save_page_connection``."""
    shop_pk = resolve_legacy_pk(Shop, shop_ref)
    if shop_pk is None:
        raise TikTokSessionError(SESSION_EXPIRED_MESSAGE)
    shop = Shop.objects.filter(pk=shop_pk).first()
    assert_shop_access(user, shop.id, roles=MANAGEMENT_ROLES)

    session = FacebookOAuthSession.objects.filter(id=session_id).first()
    if session is None:
        raise TikTokSessionError(SESSION_EXPIRED_MESSAGE)
    tokens = _session_tokens(session)
    if not tokens.get('access_token'):
        raise TikTokSessionError(SESSION_EXPIRED_MESSAGE)
    account = get_tiktok_session_user(session_id)
    connected_by = getattr(user, 'firebase_uid', None) or str(user.pk)

    SocialIntegration.objects.update_or_create(
        shop=shop,
        platform='tiktok',
        defaults={
            'is_connected': True,
            'page_id': str(account.get('openId') or ''),
            'page_name': account.get('username') or account.get('displayName'),
            'connected_by': connected_by,
            'access_token': encrypt_token(tokens['access_token']),
            'refresh_token': encrypt_token(tokens.get('refresh_token') or ''),
            'token_expires_at': _token_expiry(tokens.get('expires_in')),
            'sync_status': 'idle',
        },
    )
    session.delete()
    return {'success': True, 'username': account.get('username')}


def ensure_fresh_access_token(integration):
    """Return a usable access token, refreshing when within the skew window."""
    expires_at = integration.token_expires_at
    if expires_at is None or expires_at - TOKEN_EXPIRY_SKEW > timezone.now():
        token = integration.get_access_token()
        if token:
            return token
    refresh_token = integration.get_refresh_token()
    if not refresh_token:
        raise TikTokApiError('TikTok session expired and no refresh token is stored.')
    data = refresh_access_token(refresh_token)
    access_token = data.get('access_token')
    if not access_token:
        raise TikTokApiError('No access token in TikTok refresh response.')
    integration.access_token = encrypt_token(access_token)
    if data.get('refresh_token'):
        integration.refresh_token = encrypt_token(data['refresh_token'])
    integration.token_expires_at = _token_expiry(data.get('expires_in'))
    integration.save(update_fields=['access_token', 'refresh_token', 'token_expires_at'])
    return access_token


def initialize_video_post(
    access_token, title, video_url, privacy_level=None,
    disable_comment=False, disable_duet=False, disable_stitch=False,
    brand_content=False, brand_organic=False,
):
    """Create a PULL_FROM_URL video post; returns the ``publish_id``.

    ``privacy_level`` and the interaction toggles are the user's own choices
    from the pre-publish sheet (Content Sharing Guidelines: no client-side
    presets), so they pass through unvalidated here except the privacy enum.
    """
    if privacy_level and privacy_level not in ALLOWED_PRIVACY_LEVELS:
        raise TikTokApiError(f'Unsupported privacy_level: {privacy_level}')
    payload = _request_json('POST', 'post/publish/video/init/', access_token, body={
        'post_info': {
            'title': (title or '')[:TIKTOK_TITLE_MAX_LENGTH],
            'privacy_level': privacy_level or DEFAULT_PRIVACY_LEVEL,
            'disable_duet': bool(disable_duet),
            'disable_comment': bool(disable_comment),
            'disable_stitch': bool(disable_stitch),
            'brand_content_toggle': bool(brand_content),
            'brand_organic_toggle': bool(brand_organic),
            'video_cover_timestamp_ms': 1000,
        },
        'source_info': {
            'source': 'PULL_FROM_URL',
            'video_url': video_url,
        },
        'post_mode': 'DIRECT_POST',
        'media_type': 'VIDEO',
    })
    _raise_for_error(payload, 'Video init failed')
    publish_id = (payload.get('data') or {}).get('publish_id')
    if not publish_id:
        raise TikTokApiError('No publish_id in TikTok response.')
    return publish_id


def authorize_url(state):
    """Frontend dialog URL; built here so the scope list stays server-side."""
    query = urlencode({
        'client_key': getattr(settings, 'TIKTOK_CLIENT_KEY', ''),
        'response_type': 'code',
        'scope': ','.join(SCOPES),
        'redirect_uri': getattr(settings, 'TIKTOK_REDIRECT_URI', ''),
        'state': state,
    })
    return f'https://www.tiktok.com/v2/auth/authorize/?{query}'
