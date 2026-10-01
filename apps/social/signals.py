"""Auto-post triggers, mirroring the ``products/{productId}`` Firestore triggers.

Posting happens on ``transaction.on_commit`` (and then in Celery), so a failed
or rolled-back product write never blocks the request and never posts early.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.social.tasks import post_product_to_facebook

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Product)
def _remember_publish_flag(sender, instance, **kwargs):
    if not instance.pk:
        instance._publish_to_facebook_before = None
        return
    instance._publish_to_facebook_before = (
        Product.objects.filter(pk=instance.pk)
        .values_list('publish_to_facebook', flat=True)
        .first()
    )


def _enqueue_post(shop_id, product_ref):
    try:
        post_product_to_facebook.delay(shop_id, [product_ref])
    except Exception:
        # The legacy Firestore trigger could never fail the product write, and a
        # broker outage must not 500 the create/update that just committed.
        logger.exception('Could not queue Facebook auto-post for product %s.', product_ref)


@receiver(post_save, sender=Product)
def auto_post_product(sender, instance, created, **kwargs):
    if not instance.publish_to_facebook:
        return
    if not created and instance._publish_to_facebook_before is True:
        return
    logger.info('Queueing Facebook auto-post for product %s.', instance.pk)
    transaction.on_commit(
        lambda: _enqueue_post(str(instance.shop_id), str(instance.legacy_id or instance.pk))
    )
