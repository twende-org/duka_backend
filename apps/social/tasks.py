"""Celery tasks for the social domain (ported Cloud Functions).

- ``post_product_to_facebook``: the ``autoPostProductOnCreate`` /
  ``autoPostProductOnUpdate`` Firestore triggers, and the manual post button.
- ``daily_social_poster``: the ``dailySocialScheduler`` cron (10:00 EAT).
- ``peak_hours_marketing_drip``: the ``peakHoursMarketingDrip`` cron
  (07:30 / 12:30 / 19:30 EAT), one reel per opt-in shop.
"""
import logging
from datetime import datetime
from datetime import timezone as dt_timezone

from celery import shared_task
from django.utils import timezone

# pyrefly: ignore [missing-import]
from apps.products.models import Inventory, Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.social.models import SocialIntegration
# pyrefly: ignore [missing-import]
from apps.social.posting import SocialPostError, perform_facebook_post

logger = logging.getLogger(__name__)

EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
SCHEDULER_BATCH = 50
MARKETING_BATCH = 50


def _has_images(product):
    if product.image_url:
        return True
    return isinstance(product.image_urls, list) and bool(product.image_urls)


def next_product_for_shop(shop):
    """Product to post next: in stock, has images, posted longest ago."""
    rows = (
        Inventory.objects.filter(product__shop=shop, quantity__gt=0)
        .select_related('product')
        .order_by('product_id')[:SCHEDULER_BATCH]
    )
    candidates = {}
    for row in rows:
        if _has_images(row.product):
            candidates.setdefault(row.product_id, row.product)
    if not candidates:
        return None
    return min(candidates.values(), key=lambda product: product.last_posted_at or EPOCH)


@shared_task(name='apps.social.tasks.post_product_to_facebook')
def post_product_to_facebook(shop_id, product_ids, post_format='feed', include_image=True):
    """Publish in the background; failures are recorded, never raised."""
    if isinstance(product_ids, str):
        product_ids = [product_ids]
    try:
        return perform_facebook_post(shop_id, product_ids, None, include_image, post_format)
    except SocialPostError as exc:
        logger.warning('Background Facebook post failed (shop %s): %s', shop_id, exc)
        return {'success': False, 'error': str(exc)}


TIKTOK_STATUS_POLL_INTERVAL_SECONDS = 60
TIKTOK_STATUS_POLL_MAX_ATTEMPTS = 15  # ~15 minutes of SEND_TO_USER_INBOX retries.
TIKTOK_STATUS_PUBLISHED = 'PUBLISH_COMPLETE'
TIKTOK_STATUS_FAILED = 'FAILED'


@shared_task(name='apps.social.tasks.poll_tiktok_publish_status')
def poll_tiktok_publish_status(social_log_id, attempt=0):
    """Resolve a TikTok ``publish_id`` to its final status (Content Sharing
    Guidelines: apps must track post status, not fire-and-forget).

    ``SEND_TO_USER_INBOX`` reschedules itself until the attempt ceiling; a
    terminal status (or the ceiling, or a vanished integration) writes the
    outcome onto the ``social_logs`` row the publish call recorded.
    """
    # pyrefly: ignore [missing-import]
    from apps.social import tiktok as tiktok_api
    # pyrefly: ignore [missing-import]
    from apps.social.models import SocialLog

    log = SocialLog.objects.filter(pk=social_log_id).first()
    if log is None or not log.tiktok_publish_id:
        return {'success': False, 'error': 'log-missing'}
    integration = SocialIntegration.objects.filter(
        shop=log.shop, platform='tiktok', is_connected=True,
    ).first()
    if integration is None:
        return {'success': False, 'error': 'tiktok-not-connected'}

    try:
        access_token = tiktok_api.ensure_fresh_access_token(integration)
        entries = tiktok_api.fetch_publish_status(access_token, [log.tiktok_publish_id])
    except tiktok_api.TikTokApiError as exc:
        if attempt < TIKTOK_STATUS_POLL_MAX_ATTEMPTS:
            poll_tiktok_publish_status.apply_async(
                args=[social_log_id, attempt + 1],
                countdown=TIKTOK_STATUS_POLL_INTERVAL_SECONDS,
            )
            return {'success': False, 'error': str(exc), 'retrying': True}
        log.status = 'failure'
        log.error = f'TikTok status polling gave up: {exc}'
        log.save(update_fields=['status', 'error'])
        return {'success': False, 'error': str(exc)}

    entry = entries[0] if entries else {}
    status = entry.get('status') or ''
    if status == TIKTOK_STATUS_PUBLISHED:
        log.status = 'success'
        log.error = ''
        log.save(update_fields=['status', 'error'])
        return {'success': True, 'publishId': log.tiktok_publish_id}
    if status == TIKTOK_STATUS_FAILED:
        reason = entry.get('failReason') or 'TikTok rejected the video.'
        log.status = 'failure'
        log.error = reason
        log.save(update_fields=['status', 'error'])
        return {'success': False, 'error': reason}
    if attempt < TIKTOK_STATUS_POLL_MAX_ATTEMPTS:
        poll_tiktok_publish_status.apply_async(
            args=[social_log_id, attempt + 1],
            countdown=TIKTOK_STATUS_POLL_INTERVAL_SECONDS,
        )
        return {'success': False, 'status': status or 'unknown', 'retrying': True}
    return {'success': False, 'status': status or 'unknown', 'retrying': False}


@shared_task(name='apps.social.tasks.daily_social_poster')
def daily_social_poster():
    """One reel per connected shop, rotating through the in-stock catalog."""
    results = []
    shops = Shop.objects.filter(
        social_integrations__platform='facebook',
        social_integrations__is_connected=True,
    ).distinct()
    for shop in shops:
        product = next_product_for_shop(shop)
        if product is None:
            logger.info('No in-stock product with images for shop %s.', shop.pk)
            continue
        product_ref = str(product.legacy_id or product.pk)
        try:
            result = perform_facebook_post(shop.pk, [product_ref], None, True, 'reel')
        except SocialPostError as exc:
            logger.warning('Scheduled post failed for shop %s: %s', shop.pk, exc)
            results.append({'shopId': str(shop.pk), 'productId': product_ref, 'success': False, 'error': str(exc)})
            continue
        Product.objects.filter(pk=product.pk).update(last_posted_at=timezone.now())
        results.append({'shopId': str(shop.pk), 'productId': product_ref, 'success': True, **result})
    return results


def next_marketing_product(shop):
    """Product the drip should post: publishToFacebook, has images, posted longest ago."""
    products = list(
        Product.objects.filter(shop=shop, publish_to_facebook=True).order_by('pk')[:MARKETING_BATCH]
    )
    candidates = [product for product in products if _has_images(product)]
    if not candidates:
        return None
    return min(candidates, key=lambda product: product.last_posted_at or EPOCH)


def marketing_drip_for_shop(shop):
    """Post one reel for ``shop``; ``None`` when the shop opted out or has nothing to post."""
    ai_marketing_config = shop.ai_marketing_settings or {}
    if ai_marketing_config.get('enabled') is not True:
        return None
    # Legacy only checked that a facebook integration document existed, so a shop
    # with a revoked token was retried forever; require the connection to be live.
    if not SocialIntegration.objects.filter(
        shop=shop, platform='facebook', is_connected=True,
    ).exists():
        return None
    product = next_marketing_product(shop)
    if product is None:
        return None
    product_ref = str(product.legacy_id or product.pk)
    result = perform_facebook_post(
        shop.pk, [product_ref], None, True, 'reel', ai_marketing_config,
    )
    Product.objects.filter(pk=product.pk).update(last_posted_at=timezone.now())
    return {'shopId': str(shop.pk), 'productId': product_ref, 'success': True, **result}


def run_marketing_drip(shop_ids=None):
    """Peak-hours drip across every eligible shop (port of ``runMarketingDrip``)."""
    shops = Shop.objects.all()
    if shop_ids is not None:
        shops = shops.filter(pk__in=shop_ids)
    results = []
    for shop in shops:
        try:
            result = marketing_drip_for_shop(shop)
        except Exception as exc:
            logger.exception('Marketing drip failed for shop %s.', shop.pk)
            results.append({'shopId': str(shop.pk), 'success': False, 'error': str(exc)})
            continue
        if result is not None:
            results.append(result)
    return results


@shared_task(name='apps.social.tasks.peak_hours_marketing_drip')
def peak_hours_marketing_drip():
    """Cron wrapper for the legacy ``peakHoursMarketingDrip`` (07:30/12:30/19:30 EAT)."""
    return run_marketing_drip()
