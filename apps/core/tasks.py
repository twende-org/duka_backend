"""Celery tasks for cross-cutting maintenance jobs."""

import logging

from celery import shared_task

# pyrefly: ignore [missing-import]
from apps.core import delivery_sync
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop

logger = logging.getLogger(__name__)


@shared_task
def ping():
    """Smoke task proving the worker/beat wiring loads a task module."""
    return 'pong'


@shared_task
def sync_product_to_delivery_app(product_id):
    """Mirror one product document into the Delivery App project."""
    try:
        product = (
            Product.objects.select_related('shop', 'category').filter(pk=product_id).first()
        )
        if product is None:
            return 'missing'
        delivery_sync.push_product(product)
    except delivery_sync.DeliverySyncDisabled:
        return 'disabled'
    except Exception:
        # The legacy trigger logged and moved on; a remote failure must never
        # surface as a failed product save.
        logger.exception('Delivery App product sync failed for %s.', product_id)
        return 'error'
    return 'ok'


@shared_task
def remove_product_from_delivery_app(product_key):
    """Drop a mirrored product document (deleted row, or toggle switched off)."""
    try:
        delivery_sync.remove_product(product_key)
    except delivery_sync.DeliverySyncDisabled:
        return 'disabled'
    except Exception:
        logger.exception('Delivery App product removal failed for %s.', product_key)
        return 'error'
    return 'ok'


@shared_task
def sync_stock_to_delivery_app(product_id):
    """Mirror the shop-wide quantity of one product."""
    try:
        product = Product.objects.filter(pk=product_id).first()
        if product is None:
            return 'missing'
        delivery_sync.push_stock(product)
    except delivery_sync.DeliverySyncDisabled:
        return 'disabled'
    except Exception:
        logger.exception('Delivery App stock sync failed for %s.', product_id)
        return 'error'
    return 'ok'


@shared_task
def sync_shop_to_delivery_app(shop_id, updates):
    """Re-stamp the changed shop fields on that shop's mirrored products."""
    try:
        shop = Shop.objects.filter(pk=shop_id).first()
        if shop is None:
            return 'missing'
        return delivery_sync.push_shop_identity(shop, updates)
    except delivery_sync.DeliverySyncDisabled:
        return 'disabled'
    except Exception:
        logger.exception('Delivery App shop sync failed for %s.', shop_id)
        return 'error'
