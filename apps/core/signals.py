"""Delivery App sync triggers, mirroring the ``deliverySync.js`` Firestore triggers.

Every receiver queues its remote work on ``transaction.on_commit`` (and then in
Celery), so a rolled-back write never mirrors a half-saved product and a broker
or remote-project outage never fails the request that just committed.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

# pyrefly: ignore [missing-import]
from apps.core import delivery_sync
# pyrefly: ignore [missing-import]
from apps.core.tasks import (
    remove_product_from_delivery_app,
    sync_product_to_delivery_app,
    sync_shop_to_delivery_app,
    sync_stock_to_delivery_app,
)
# pyrefly: ignore [missing-import]
from apps.products.models import Inventory, Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop

logger = logging.getLogger(__name__)


def _enqueue(task, *args):
    try:
        task.delay(*args)
    except Exception:
        logger.exception('Could not queue a Delivery App sync task.')


@receiver(pre_save, sender=Product)
def _remember_delivery_flag(sender, instance, **kwargs):
    if not instance.pk:
        instance._publish_to_delivery_app_before = None
        return
    instance._publish_to_delivery_app_before = (
        Product.objects.filter(pk=instance.pk)
        .values_list('publish_to_delivery_app', flat=True)
        .first()
    )


@receiver(post_save, sender=Product)
def sync_product_on_save(sender, instance, created, **kwargs):
    if instance.publish_to_delivery_app:
        # The legacy update trigger re-pushed the whole document on every change.
        product_id = str(instance.pk)
        transaction.on_commit(lambda: _enqueue(sync_product_to_delivery_app, product_id))
        return

    # Only an explicit true -> false transition removes the mirrored document.
    # Django cannot tell "never set" from "switched off", and imported products
    # start at false, so deleting on every false save would wipe remote rows the
    # merchant never unpublished.
    if not created and instance._publish_to_delivery_app_before is True:
        product_key = delivery_sync.document_key(instance)
        transaction.on_commit(lambda: _enqueue(remove_product_from_delivery_app, product_key))


@receiver(post_delete, sender=Product)
def remove_product_on_delete(sender, instance, **kwargs):
    product_key = delivery_sync.document_key(instance)
    transaction.on_commit(lambda: _enqueue(remove_product_from_delivery_app, product_key))


@receiver(post_save, sender=Inventory)
def sync_stock_on_save(sender, instance, **kwargs):
    product_id = str(instance.product_id)
    transaction.on_commit(lambda: _enqueue(sync_stock_to_delivery_app, product_id))


@receiver(pre_save, sender=Shop)
def _remember_shop_identity(sender, instance, **kwargs):
    if not instance.pk:
        instance._delivery_shop_before = None
        return
    instance._delivery_shop_before = (
        Shop.objects.filter(pk=instance.pk).values('name', 'lat', 'lon').first()
    )


@receiver(post_save, sender=Shop)
def sync_shop_on_save(sender, instance, **kwargs):
    before = getattr(instance, '_delivery_shop_before', None)
    if not before:
        return

    updates = {}
    if before['name'] != instance.name:
        updates['store'] = instance.name or delivery_sync.STORE_FALLBACK
    if before['lat'] != instance.lat or before['lon'] != instance.lon:
        updates['location'] = delivery_sync.shop_location(instance)
    if not updates:
        return

    shop_id = str(instance.pk)
    transaction.on_commit(
        lambda: _enqueue(sync_shop_to_delivery_app, shop_id, updates)
    )
