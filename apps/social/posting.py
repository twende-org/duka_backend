"""Facebook / Instagram posting pipeline.

Ported from ``functions/src/social/facebookPost.js``: caption generation via
OpenRouter, image or reel publishing (Graph v18.0), the Instagram reel polling
loop with image fallback, and the ``social_logs`` / campaign bookkeeping.
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
from apps.social.reels import ReelGenerationError, generate_reel_from_images
# pyrefly: ignore [missing-import]
from apps.social.services import graph_url

logger = logging.getLogger(__name__)

STOREFRONT_BASE_URL = 'https://duka.twendedigital.tech'
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
OPENROUTER_MODEL = 'openai/gpt-4o-mini'
AI_TIMEOUT = 10
AI_ATTEMPTS = 3
GRAPH_TIMEOUT = 30
REEL_POLL_ATTEMPTS = 36
REEL_POLL_INTERVAL = 5

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


def _graph_post(path, payload):
    try:
        response = requests.post(graph_url(path), data=payload, timeout=GRAPH_TIMEOUT)
        response.raise_for_status()
        body = response.json()
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
    return body if isinstance(body, dict) else {}


def _graph_get(path, params):
    try:
        response = requests.get(graph_url(path), params=params, timeout=GRAPH_TIMEOUT)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        raise GraphApiError(str(exc)) from exc
    except ValueError as exc:
        raise GraphApiError('Invalid JSON from Facebook.') from exc
    return body if isinstance(body, dict) else {}


def _error_text(exc):
    if isinstance(exc, GraphApiError) and exc.payload is not None:
        return json.dumps(exc.payload)
    return str(exc)


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


def generate_ai_caption(product, shop, price_str, tone=None, now=None):
    """Caption from OpenRouter, or ``None`` when it cannot be produced.

    Mirrors the legacy retry loop: 3 attempts, 10 s per attempt, markdown
    fences stripped; a total failure only means the caller uses the fallback.
    """
    api_key = getattr(settings, 'OPENROUTER_API_KEY', '')
    if not api_key:
        return None

    strategy = caption_strategy((now or timezone.now()).astimezone(dt_timezone.utc).hour + 3)
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
                },
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                },
                timeout=AI_TIMEOUT,
            )
            response.raise_for_status()
            choices = response.json().get('choices') or []
            caption = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
            if caption:
                return re.sub(r'```(?:json)?', '', caption, flags=re.IGNORECASE).strip()
        except (requests.RequestException, ValueError, AttributeError, IndexError) as exc:
            logger.warning('AI caption attempt %s failed: %s', attempt, exc)
    logger.error('All %s AI caption attempts failed; using the fallback caption.', AI_ATTEMPTS)
    return None


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


def _record_log(shop, product_ids, **fields):
    try:
        SocialLog.objects.create(
            shop=shop, type='social_post', action='post', product_ids=product_ids, **fields,
        )
    except Exception:
        # Bookkeeping must never turn a published post into a retry.
        logger.exception('Failed to write the social log for shop %s.', shop.pk)


def _record_campaign(shop, main_product, product_count, instagram_post_id, ai_marketing_config):
    try:
        # The Firestore campaign doc also carried budget/engagement counters
        # that have no column on this model yet (marketing cutover owns that).
        Campaign.objects.create(
            shop=shop,
            name=f'Social Post: {main_product.name}' + (' & others' if product_count > 1 else ''),
            source='ai_auto_pilot' if ai_marketing_config else 'manual',
            platform='facebook_instagram' if instagram_post_id else 'facebook',
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

    Returns ``{'success': True, 'facebookPostId': ..., 'instagramPostId': ...}``.
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
    if post_format == 'reel' and include_image and image_urls:
        try:
            logger.info('Generating Reel for shop %s.', shop.pk)
            # The legacy generator also accepted a backgroundMusicUrl from the
            # shop/integration docs; no such field exists on the Django models.
            video_url = generate_reel_from_images(image_urls, None)
        except ReelGenerationError as exc:
            logger.error('Reel generation failed, falling back to images: %s', exc)
            video_fallback_reason = str(exc)
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
    instagram_post_id, ig_fallback, instagram_error = _post_to_instagram(
        integration, access_token, post_message, image_urls, video_url, post_format, include_image,
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
    return {
        'success': True,
        'facebookPostId': facebook_post_id,
        'instagramPostId': instagram_post_id,
    }
