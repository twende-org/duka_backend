"""Facebook / Instagram posting pipeline.

Ported from ``functions/src/social/facebookPost.js``: caption generation via
OpenRouter (cheapest-tier model, bounded tokens, per-process cache), image or
reel publishing (Graph v18.0), exponential backoff on rate-limit / server
errors, the Instagram reel polling loop with single-image fallback, and the
``social_logs`` / campaign bookkeeping.

Execution contracts that must not drift:
- Tests patch ``apps.social.posting.requests.post`` / ``.get`` and
  ``time.sleep``: every outbound call goes through the module-level
  ``requests`` attribute with the kwargs shapes the suite asserts on.
- ``perform_facebook_post`` raises ``FacebookNotConnected`` /
  ``ProductNotFound`` before any Graph call and ``SocialPostFailed`` after a
  recorded failure; Instagram failures never fail the post (legacy).
"""
import json
import logging
import re
import time
from datetime import timezone as dt_timezone
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

# pyrefly: ignore [missing-import]
from apps.core.crypto import decrypt_token
# pyrefly: ignore [missing-import]
from apps.core.legacy import resolve_legacy_pk
# pyrefly: ignore [missing-import]
from apps.marketing.models import Campaign
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.social.models import SocialIntegration, SocialLog
# pyrefly: ignore [missing-import]
from apps.social import reels
from apps.social.reels import ReelGenerationError, generate_reel_from_images
# pyrefly: ignore [missing-import]
from apps.social.services import graph_url
# pyrefly: ignore [missing-import]
from apps.social import tiktok as tiktok_api

logger = logging.getLogger(__name__)

# --- isolated client configuration -----------------------------------------
# Hard 10-second connect/read frames on every outbound call. Graph bodies are
# tiny (URLs, not uploads) so a 10 s read frame is generous; a hung socket
# must never park a Celery worker.
GRAPH_CONNECT_TIMEOUT = 10
GRAPH_READ_TIMEOUT = 10
GRAPH_TIMEOUT = (GRAPH_CONNECT_TIMEOUT, GRAPH_READ_TIMEOUT)
AI_TIMEOUT = (10, 10)

# Realistic desktop-browser UA so outbound calls don't pick up datacenter bot
# classification on either Meta or the LLM gateway.
BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
)
BASE_HEADERS = {'User-Agent': BROWSER_USER_AGENT, 'Accept': 'application/json'}

# Graph retry ladder: 429 (rate limited) and 5xx are transient; honour
# Retry-After when Meta sends it, otherwise exponential 2s/4s/8s capped at 30s.
GRAPH_MAX_RETRIES = 3
GRAPH_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
GRAPH_BACKOFF_BASE_SECONDS = 2
GRAPH_BACKOFF_CAP_SECONDS = 30

# Reel container polling ceiling: 36 x 5s = 3 minutes, as in legacy.
REEL_POLL_ATTEMPTS = 36
REEL_POLL_INTERVAL = 5

# --- caption synthesis (OpenRouter, cost-minimised) --------------------------
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
# flash-lite is the cheapest capable tier and matches the webhook auto-reply,
# insights and assistant models — one model family to reason about.
OPENROUTER_MODEL = 'google/gemini-2.5-flash-lite'
AI_MAX_TOKENS = 250
AI_ATTEMPTS = 3
# Per-process caption cache: retries, re-posts and drip retries of the same
# product inside one window reuse the caption instead of a paid LLM call.
CAPTION_CACHE_TTL_SECONDS = 6 * 3600
CAPTION_CACHE_MAX_ENTRIES = 256

STOREFRONT_BASE_URL = 'https://duka.twendedigital.tech'

STRATEGY_DEFAULT = 'promotional na ya kuvutia'
STRATEGY_MORNING = (
    'kutoa elimu, kueleza faida kuu ya bidhaa hii, na kuwapa watu hamasa ya kuanza siku '
    '(Educational & High Energy)'
)
STRATEGY_MIDDAY = (
    'kuonyesha uharaka (Urgency/FOMO), kana kwamba ni ofa ya mchana au mzigo unakaribia '
    'kuisha, ili wafanye maamuzi haraka'
)
STRATEGY_EVENING = (
    'kusimulia hadithi fupi (Storytelling), kuonyesha jinsi bidhaa hii inavyorahisisha maisha '
    'au kuleta ufahari, na kuwashawishi kuweka oda kabla ya kulala'
)


class SocialPostError(Exception):
    """Base class for the user-visible posting failures."""


class FacebookNotConnected(SocialPostError):
    def __init__(self, message='facebook-not-connected'):
        super().__init__(message)


class TikTokNotConnected(SocialPostError):
    def __init__(self, message='tiktok-not-connected'):
        super().__init__(message)


class ProductNotFound(SocialPostError):
    def __init__(self, message='product-not-found'):
        super().__init__(message)


class GraphApiError(SocialPostError):
    """A Graph API call failed; ``payload`` keeps Facebook's error body."""

    def __init__(self, message, payload=None):
        super().__init__(message)
        self.payload = payload


class SocialPostFailed(SocialPostError):
    """The post did not complete; already recorded in ``social_logs``."""


class TikTokVideoTooLong(SocialPostError):
    """The generated video exceeds the account's ``max_video_post_duration_sec``."""

    def __init__(self, message='tiktok-video-too-long'):
        super().__init__(message)


class TikTokPrivacyUnavailable(SocialPostError):
    """The chosen privacy level is not in the account's ``privacy_level_options``."""

    def __init__(self, message='tiktok-privacy-unavailable'):
        super().__init__(message)


# --- HTTP plumbing ------------------------------------------------------------

def _retry_delay(response, attempt):
    retry_after = None
    if response is not None:
        retry_after = (response.headers or {}).get('Retry-After')
    if retry_after and str(retry_after).isdigit():
        return min(int(retry_after), GRAPH_BACKOFF_CAP_SECONDS)
    return min(GRAPH_BACKOFF_BASE_SECONDS ** attempt, GRAPH_BACKOFF_CAP_SECONDS)


def _request_with_retries(method, url, **kwargs):
    """Single outbound frame with backoff on 429/5xx.

    Returns the decoded JSON object. Raises the underlying
    ``requests.HTTPError`` / ``requests.RequestException`` so the Graph
    wrappers keep their exact error-classification contract.
    """
    request_fn = requests.post if method == 'post' else requests.get
    kwargs.setdefault('headers', dict(BASE_HEADERS))
    kwargs.setdefault('timeout', GRAPH_TIMEOUT)
    for attempt in range(GRAPH_MAX_RETRIES + 1):
        try:
            response = request_fn(url, **kwargs)
            status = response.status_code
            if status in GRAPH_RETRY_STATUSES and attempt < GRAPH_MAX_RETRIES:
                delay = _retry_delay(response, attempt)
                logger.warning(
                    'Graph %s %s returned %s; retrying in %ss (attempt %s/%s).',
                    method.upper(), url, status, delay, attempt + 1, GRAPH_MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            body = response.json()
            return body if isinstance(body, dict) else {}
        except requests.RequestException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in GRAPH_RETRY_STATUSES and attempt < GRAPH_MAX_RETRIES:
                delay = _retry_delay(exc.response, attempt)
                logger.warning(
                    'Graph %s %s failed (%s); retrying in %ss (attempt %s/%s).',
                    method.upper(), url, status, delay, attempt + 1, GRAPH_MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            raise
    raise GraphApiError(f'Graph {method} {url} failed after {GRAPH_MAX_RETRIES} retries.')  # pragma: no cover


def _graph_post(path, payload):
    try:
        return _request_with_retries('post', graph_url(path), data=payload)
    except requests.HTTPError as exc:
        detail = None
        if exc.response is not None:
            try:
                detail = exc.response.json()
            except ValueError:
                detail = (exc.response.text or '').strip()[:500] or None
        raise GraphApiError(str(exc), payload=detail) from exc
    except requests.RequestException as exc:
        raise GraphApiError(str(exc)) from exc
    except ValueError as exc:
        raise GraphApiError('Invalid JSON from Facebook.') from exc


def _graph_get(path, params):
    try:
        return _request_with_retries('get', graph_url(path), params=params)
    except requests.RequestException as exc:
        raise GraphApiError(str(exc)) from exc
    except ValueError as exc:
        raise GraphApiError('Invalid JSON from Facebook.') from exc


def _error_text(exc):
    if isinstance(exc, GraphApiError) and exc.payload is not None:
        return json.dumps(exc.payload)
    return str(exc)


# --- caption synthesis ---------------------------------------------------------

def format_price(value):
    """``Decimal('1200.00')`` -> ``'1,200'`` (JS ``toLocaleString`` parity)."""
    text = f'{Decimal(value).quantize(Decimal("0.01")):,f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text


def caption_strategy(eat_hour):
    """Time-of-day flavour the AI is asked to use (EAT = UTC + 3, as in legacy)."""
    if 6 <= eat_hour <= 10:
        return STRATEGY_MORNING
    if 11 <= eat_hour <= 15:
        return STRATEGY_MIDDAY
    if eat_hour >= 16:
        return STRATEGY_EVENING
    return STRATEGY_DEFAULT


# Per-process caption cache: (key -> (expires_at, caption)). Process-local by
# design — each Celery worker keeps its own window, no shared state to manage.
_caption_cache = {}


def _cache_key(product, price_str, tone, strategy, day):
    # Product identity + commercial inputs + strategy window: a price change,
    # rename, tone switch or new day invalidates the entry naturally.
    return (str(product.pk), product.name, price_str, tone or '', strategy, day)


def _cache_get(key):
    entry = _caption_cache.get(key)
    if not entry:
        return None
    expires_at, caption = entry
    if expires_at < time.time():
        _caption_cache.pop(key, None)
        return None
    return caption


def _cache_set(key, caption):
    if len(_caption_cache) >= CAPTION_CACHE_MAX_ENTRIES:
        _caption_cache.pop(next(iter(_caption_cache)), None)  # FIFO eviction
    _caption_cache[key] = (time.time() + CAPTION_CACHE_TTL_SECONDS, caption)


def generate_ai_caption(product, shop, price_str, tone=None, now=None):
    """Caption from OpenRouter, or ``None`` when it cannot be produced.

    Cost ladder: per-process cache first, then a 3-attempt call ladder with
    hard 10 s frames; a total failure only means the caller falls back to the
    plain-text template. Markdown fences (incl. ``json``) are stripped.
    """
    api_key = getattr(settings, 'OPENROUTER_API_KEY', '')
    if not api_key:
        return None

    utc_now = (now or timezone.now()).astimezone(dt_timezone.utc)
    strategy = caption_strategy(utc_now.hour + 3)
    key = _cache_key(product, price_str, tone, strategy, utc_now.date().isoformat())
    cached = _cache_get(key)
    if cached:
        logger.info('Reusing cached AI caption for product %s.', product.pk)
        return cached

    tone_instructions = ''
    if tone:
        tone_instructions = (
            f'\nSauti ya post (Tone): {tone}. Tafadhali hakikisha ujumbe wako unaakisi sauti hii!'
        )

    prompt = f'''Wewe ni mtaalamu wa masoko ya kidijitali anayebobea katika soko la Afrika Mashariki na kimataifa.
Tengeneza caption fupi ya kuvutia sana kwa ajili ya Facebook/Instagram kwa bidhaa hii. Tumia emoji vizuri.

MUHIMU SANA: Mkakati wa post hii ni {strategy}. Badilisha lugha na mtiririko wako kuendana na mkakati huu!{tone_instructions}

Bidhaa: {product.name}
Bei: {price_str}
Maelezo: {product.description or "Hakuna maelezo. Buni maelezo mafupi kulingana na jina la bidhaa."}
Duka: {shop.name} (Simu: {shop.phone or "DM kuweka oda"})

Jibu na MANENO YA CAPTION PEKEE. Usimsalimie mtu. Mwishoni weka hashtags zinazovuma zinazoendana na bidhaa (ZISIZIDI 4).
SHERIA KALI: USITUMIE markdown. Usiweke json block. Rudisha maandishi ya kawaida tu. Usiandike "Here is the caption:".'''

    for attempt in range(1, AI_ATTEMPTS + 1):
        try:
            logger.info('Generating AI caption (attempt %s/%s).', attempt, AI_ATTEMPTS)
            response = requests.post(
                OPENROUTER_URL,
                json={
                    'model': OPENROUTER_MODEL,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.7,
                    'max_tokens': AI_MAX_TOKENS,
                },
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                    'User-Agent': BROWSER_USER_AGENT,
                },
                timeout=AI_TIMEOUT,
            )
            response.raise_for_status()
            choices = response.json().get('choices') or []
            caption = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
            if caption:
                caption = re.sub(r'```(?:json)?', '', caption, flags=re.IGNORECASE).strip()
                _cache_set(key, caption)
                return caption
        except (requests.RequestException, ValueError, AttributeError, IndexError) as exc:
            logger.warning('AI caption attempt %s failed: %s', attempt, exc)
    logger.error('All %s AI caption attempts failed; using the fallback caption.', AI_ATTEMPTS)
    return None


# --- product resolution + payload assembly ------------------------------------

def _collect_image_urls(products):
    raw = []
    if len(products) == 1:
        product = products[0]
        if isinstance(product.image_urls, list) and product.image_urls:
            raw = product.image_urls
        elif product.image_url:
            raw = [product.image_url]
    else:
        for product in products:
            urls = product.image_urls if isinstance(product.image_urls, list) else []
            raw.append(product.image_url or (urls[0] if urls else None))
    return [url for url in raw if isinstance(url, str) and url.startswith('http')]


def _append_contact_details(message, shop, post_format):
    phone = shop.whatsapp or shop.phone or ''
    if post_format == 'reel':
        # Reels avoid outbound links to protect organic reach.
        message += '\n\n💬 DM us to order!'
        if phone:
            message += f'\n📞 Contact: {phone}'
        return message
    if phone:
        clean_phone = re.sub(r'\D', '', phone)
        message += f'\n\n📲 WhatsApp: https://wa.me/{clean_phone}?text=Nahitaji%20kununua%20bidhaa'
    if shop.slug:
        message += f'\n🛒 Duka mtandaoni: {STOREFRONT_BASE_URL}/shop/{shop.slug}'
    return message


def _resolve_shop(shop_ref):
    pk = resolve_legacy_pk(Shop, shop_ref)
    return Shop.objects.filter(pk=pk).first() if pk is not None else None


def _resolve_products(product_refs):
    products = []
    for ref in product_refs:
        pk = resolve_legacy_pk(Product, ref)
        product = Product.objects.filter(pk=pk).first() if pk is not None else None
        if product is not None:
            products.append(product)
    return products


# --- publishers -----------------------------------------------------------------

def _post_to_facebook(integration, access_token, message, image_urls, video_url, include_image):
    page_id = integration.page_id
    if video_url:
        response = _graph_post(f'{page_id}/videos', {
            'file_url': video_url,
            'description': message,
            'access_token': access_token,
        })
        return response.get('id')
    if include_image and image_urls:
        # Single photo: the carousel variant silently dropped images on v18.0.
        response = _graph_post(f'{page_id}/photos', {
            'url': image_urls[0],
            'message': message,
            'access_token': access_token,
        })
        return response.get('id') or response.get('post_id')
    response = _graph_post(f'{page_id}/feed', {
        'message': message,
        'access_token': access_token,
    })
    return response.get('id')


def _wait_for_instagram_container(container_id, access_token):
    for _ in range(REEL_POLL_ATTEMPTS):
        time.sleep(REEL_POLL_INTERVAL)
        try:
            status_body = _graph_get(container_id, {
                'fields': 'status_code',
                'access_token': access_token,
            })
        except GraphApiError as exc:
            logger.warning('Failed to poll Instagram container status: %s', exc)
            continue
        status_code = status_body.get('status_code')
        logger.info('Instagram container %s status: %s', container_id, status_code)
        if status_code == 'FINISHED':
            return True
        if status_code in ('ERROR', 'EXPIRED'):
            raise GraphApiError(f'Instagram container processing failed with status: {status_code}')
    return False


def _post_to_instagram(integration, access_token, message, image_urls, video_url, post_format,
                       include_image):
    """Publish to Instagram.

    Returns ``(post_id, video_fallback_reason, instagram_error)``. Instagram
    failures never fail the whole post (legacy behaviour): the shop still gets
    the Facebook post, and the reason is recorded on the log.
    """
    instagram_id = integration.instagram_id
    if not instagram_id or not include_image or not image_urls:
        return None, None, None

    video_fallback_reason = None
    if post_format == 'reel' and video_url:
        try:
            logger.info('Sending Reel to Instagram.')
            media = _graph_post(f'{instagram_id}/media', {
                'media_type': 'REELS',
                'video_url': video_url,
                'caption': message,
                'access_token': access_token,
            })
            container_id = media.get('id')
            if not container_id or not _wait_for_instagram_container(container_id, access_token):
                video_fallback_reason = (
                    'Instagram video container processing timed out after '
                    f'{REEL_POLL_ATTEMPTS * REEL_POLL_INTERVAL // 60} minutes.'
                )
                logger.warning(video_fallback_reason)
            else:
                published = _graph_post(f'{instagram_id}/media_publish', {
                    'creation_id': container_id,
                    'access_token': access_token,
                })
                return published.get('id'), None, None
        except GraphApiError as exc:
            video_fallback_reason = str(exc)
            logger.error('Instagram Reel posting failed, falling back to an image post: %s', exc)

    try:
        media = _graph_post(f'{instagram_id}/media', {
            'image_url': image_urls[0],
            'caption': message,
            'access_token': access_token,
        })
        container_id = media.get('id')
        if not container_id:
            return None, video_fallback_reason, None
        published = _graph_post(f'{instagram_id}/media_publish', {
            'creation_id': container_id,
            'access_token': access_token,
        })
        return published.get('id'), video_fallback_reason, None
    except GraphApiError as exc:
        logger.error('Instagram posting failed: %s', exc)
        return None, video_fallback_reason, _error_text(exc)


# --- bookkeeping ----------------------------------------------------------------

def _record_log(shop, product_ids, **fields):
    try:
        return SocialLog.objects.create(
            shop=shop, type='social_post', action='post', product_ids=product_ids, **fields,
        )
    except Exception:
        # Bookkeeping must never turn a published post into a retry.
        logger.exception('Failed to write the social log for shop %s.', shop.pk)
        return None


def _record_campaign(shop, main_product, product_count, instagram_post_id, ai_marketing_config,
                     platform=None):
    try:
        # The Firestore campaign doc also carried budget/engagement counters
        # that have no column on this model yet (marketing cutoff owns that).
        Campaign.objects.create(
            shop=shop,
            name=f'Social Post: {main_product.name}' + (' & others' if product_count > 1 else ''),
            source='ai_auto_pilot' if ai_marketing_config else 'manual',
            platform=platform or ('facebook_instagram' if instagram_post_id else 'facebook'),
            status='running',
            start_date=timezone.now(),
        )
    except Exception:
        logger.exception('Failed to write the campaign record for shop %s.', shop.pk)


def perform_facebook_post(
    shop_ref, product_refs, message=None, include_image=True, post_format='feed',
    ai_marketing_config=None, now=None,
):
    """Publish ``product_refs`` to the shop's Facebook page (and Instagram).

    Returns ``{'success': True, 'facebookPostId': ..., 'instagramPostId': ...}``
    (plus ``'fallback_triggered': True`` when a reel degraded to a photo).
    Raises :class:`FacebookNotConnected` / :class:`ProductNotFound` before any
    Graph call, and :class:`SocialPostFailed` when publishing failed (the
    failure is recorded in ``social_logs`` first).
    """
    if isinstance(product_refs, (str, bytes)):
        product_refs = [product_refs]

    shop = _resolve_shop(shop_ref)
    integration = None
    if shop is not None:
        integration = SocialIntegration.objects.filter(
            shop=shop, platform='facebook', is_connected=True,
        ).first()
    access_token = decrypt_token(integration.access_token) if integration else None
    if integration is None or not access_token:
        raise FacebookNotConnected()

    products = _resolve_products(product_refs)
    if not products:
        raise ProductNotFound()

    product_ids = [str(product.legacy_id or product.pk) for product in products]
    main_product = products[0]
    price_str = (
        f'TZS {format_price(main_product.selling_price)}'
        if main_product.selling_price else 'Bei Nafuu'
    )

    post_message = message
    if not post_message:
        tone = (ai_marketing_config or {}).get('tone')
        post_message = generate_ai_caption(main_product, shop, price_str, tone=tone, now=now)
    if not post_message:
        post_message = f'{main_product.name} - {price_str}'
        if main_product.description:
            post_message += f'\n\n{main_product.description}'
    post_message = _append_contact_details(post_message, shop, post_format)

    image_urls = _collect_image_urls(products)

    video_url = None
    video_fallback_reason = None
    reel_fallback = False
    instagram_skip_reason = None
    if post_format == 'reel' and include_image and image_urls:
        try:
            # Reels are fetched over the public internet; without the
            # absolute URL map there is nothing to attach to the Graph call.
            # Raised here — before generate_reel_from_images — so no image or
            # audio bytes are ever downloaded, and caught below so the post
            # degrades to a single photo instead of crashing the worker.
            if not getattr(settings, 'REEL_PUBLIC_BASE_URL', ''):
                raise ReelGenerationError(
                    'Environment config missing mandatory REEL_PUBLIC_BASE_URL parameter map'
                )
            logger.info('Generating Reel for shop %s.', shop.pk)
            # The legacy generator also accepted a backgroundMusicUrl from the
            # shop/integration docs; no such field exists on the Django models.
            video_url = generate_reel_from_images(image_urls, None)
        except ReelGenerationError as exc:
            logger.error('Reel generation failed, falling back to a single photo: %s', exc)
            video_fallback_reason = str(exc)
            reel_fallback = True
            instagram_skip_reason = (
                'Skipped Instagram upload due to Reel format generation fallback requirements'
            )
            video_url = None

    try:
        facebook_post_id = _post_to_facebook(
            integration, access_token, post_message, image_urls, video_url, include_image,
        )
    except GraphApiError as exc:
        _record_log(shop, product_ids, status='failure', error=_error_text(exc))
        raise SocialPostFailed(f'Social posting failed: {_error_text(exc)}') from exc

    instagram_post_id = None
    instagram_error = None
    if reel_fallback:
        # Single-photo fallback workflow is Facebook-only; Instagram skipped.
        instagram_error = instagram_skip_reason
    else:
        instagram_post_id, ig_fallback, instagram_error = _post_to_instagram(
            integration, access_token, post_message, image_urls, video_url,
            post_format, include_image,
        )
        video_fallback_reason = video_fallback_reason or ig_fallback

    _record_log(
        shop, product_ids, status='success',
        facebook_post_id=facebook_post_id,
        instagram_post_id=instagram_post_id,
        video_fallback_reason=video_fallback_reason,
        instagram_error=instagram_error,
    )
    _record_campaign(shop, main_product, len(products), instagram_post_id, ai_marketing_config)
    result = {
        'success': True,
        'facebookPostId': facebook_post_id,
        'instagramPostId': instagram_post_id,
    }
    if reel_fallback:
        result['fallback_triggered'] = True
    return result


def perform_tiktok_post(
    shop_ref, product_refs, message=None, include_image=True, post_format='reel',
    ai_marketing_config=None, now=None, post_options=None,
):
    """Publish ``product_refs`` to the shop's TikTok account as a video.

    TikTok's Content Posting API is video-only, so the single path is the same
    reel pipeline the Facebook reel format uses (images -> MP4 -> PULL_FROM_URL).
    Unlike Facebook there is no photo fallback: a reel generation failure is
    recorded in ``social_logs`` and surfaces as :class:`SocialPostFailed`.

    ``post_options`` carries the user's pre-publish choices (privacy_level,
    disable_comment/disable_duet/disable_stitch, brand_content/brand_organic)
    straight through to the video init call — they are never defaulted here.

    Content Sharing Guidelines enforcement before the publish call fires:
    the caption is posted exactly as the merchant wrote it (no appended
    preset text), the chosen privacy level must be one of the account's
    ``creator_info.privacy_level_options``, and the reel's estimated duration
    must fit the account's ``max_video_post_duration_sec``. A failed
    creator-info lookup only skips these checks — it never blocks a post.

    Returns ``{'success': True, 'publishId': ...}``. Raises
    :class:`TikTokNotConnected` / :class:`ProductNotFound` before any TikTok
    call, :class:`TikTokVideoTooLong` / :class:`TikTokPrivacyUnavailable`
    when the account cannot take the post, and :class:`SocialPostFailed` when
    publishing failed.
    """
    if isinstance(product_refs, (str, bytes)):
        product_refs = [product_refs]

    shop = _resolve_shop(shop_ref)
    integration = None
    if shop is not None:
        integration = SocialIntegration.objects.filter(
            shop=shop, platform='tiktok', is_connected=True,
        ).first()
    if integration is None:
        raise TikTokNotConnected()

    products = _resolve_products(product_refs)
    if not products:
        raise ProductNotFound()

    product_ids = [str(product.legacy_id or product.pk) for product in products]
    main_product = products[0]
    price_str = (
        f'TZS {format_price(main_product.selling_price)}'
        if main_product.selling_price else 'Bei Nafuu'
    )

    post_message = message
    if not post_message:
        tone = (ai_marketing_config or {}).get('tone')
        post_message = generate_ai_caption(main_product, shop, price_str, tone=tone, now=now)
    if not post_message:
        post_message = f'{main_product.name} - {price_str}'
        if main_product.description:
            post_message += f'\n\n{main_product.description}'
    # No _append_contact_details here: TikTok's guidelines forbid preset text
    # the creator cannot edit — the caption ships exactly as written above.

    image_urls = _collect_image_urls(products)
    video_url = None
    try:
        if not getattr(settings, 'REEL_PUBLIC_BASE_URL', ''):
            raise ReelGenerationError(
                'Environment config missing mandatory REEL_PUBLIC_BASE_URL parameter map'
            )
        if not image_urls:
            raise ReelGenerationError('No usable product images to build the TikTok video.')
        logger.info('Generating TikTok video for shop %s.', shop.pk)
        video_url = generate_reel_from_images(image_urls, None)
    except ReelGenerationError as exc:
        logger.error('TikTok video generation failed: %s', exc)
        _record_log(shop, product_ids, status='failure', error=str(exc))
        raise SocialPostFailed(f'TikTok posting failed: {exc}') from exc

    try:
        access_token = tiktok_api.ensure_fresh_access_token(integration)
    except tiktok_api.TikTokApiError as exc:
        _record_log(shop, product_ids, status='failure', error=str(exc))
        raise SocialPostFailed(f'TikTok posting failed: {exc}') from exc

    try:
        creator_info = tiktok_api.fetch_creator_info(access_token)
    except tiktok_api.TikTokApiError as exc:
        # Metadata lookup only; a transient failure must not block publishing.
        logger.warning('TikTok creator info unavailable for shop %s: %s', shop.pk, exc)
        creator_info = None
    if creator_info:
        options = post_options or {}
        privacy_level = options.get('privacy_level')
        allowed = creator_info.get('privacyLevelOptions') or []
        if privacy_level and allowed and privacy_level not in allowed:
            detail = (
                f'Privacy level {privacy_level} is not available on this TikTok account.'
            )
            _record_log(shop, product_ids, status='failure', error=detail)
            raise TikTokPrivacyUnavailable(detail)
        max_seconds = creator_info.get('maxVideoPostDurationSec') or 0
        if max_seconds:
            expected = reels.estimate_reel_duration_seconds(len(image_urls))
            if expected > max_seconds:
                detail = (
                    f'Video would be about {expected}s but this TikTok account '
                    f'allows at most {max_seconds}s. Use fewer product photos and try again.'
                )
                _record_log(shop, product_ids, status='failure', error=detail)
                raise TikTokVideoTooLong(detail)

    try:
        publish_id = tiktok_api.initialize_video_post(
            access_token, post_message, video_url, **(post_options or {}),
        )
    except tiktok_api.TikTokApiError as exc:
        _record_log(shop, product_ids, status='failure', error=str(exc))
        raise SocialPostFailed(f'TikTok posting failed: {exc}') from exc

    log = _record_log(shop, product_ids, status='success', tiktok_publish_id=publish_id)
    if log is not None:
        _schedule_tiktok_status_poll(log)
    _record_campaign(
        shop, main_product, len(products), None, ai_marketing_config, platform='tiktok',
    )
    return {'success': True, 'publishId': publish_id}


def _schedule_tiktok_status_poll(log):
    """Queue the publish-status poll; a down broker must never fail the post."""
    try:
        from apps.social.tasks import poll_tiktok_publish_status

        poll_tiktok_publish_status.apply_async(args=[log.pk], countdown=60)
    except Exception:
        logger.warning('Could not schedule TikTok status poll for log %s.', log.pk, exc_info=True)
