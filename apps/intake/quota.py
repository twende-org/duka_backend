"""Per-shop monthly allowance for AI intake photos.

QR intake is deterministic and free, so it is never gated. The allowance is
counted from what the pipeline already stores — ``ai_usage['images']`` on
``engine_used='ai'`` batches of the current calendar month (completed parses
only; failed batches did not produce drafts and do not burn the allowance) —
and enforced in the worker right before the first vision call, so an
over-limit batch fails with a friendly message instead of spending credits.
"""
from django.conf import settings
from django.utils import timezone

from .models import IntakeBatch


class QuotaExhausted(Exception):
    """The shop has consumed its monthly AI intake photo allowance."""


def get_shop_limit(shop):
    """Effective monthly photo allowance for the shop; ``None`` = unlimited."""
    override = getattr(shop, 'ai_intake_monthly_limit', None)
    if override is not None:
        return override if override > 0 else None
    default = settings.AI_INTAKE_MONTHLY_LIMIT_PER_SHOP
    return default if default > 0 else None


def ai_images_used(shop, *, now=None):
    """AI photos this shop consumed in the current calendar month."""
    moment = timezone.localtime(now or timezone.now())
    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = 0
    usages = IntakeBatch.objects.filter(
        shop=shop, engine_used='ai', created_at__gte=month_start,
    ).values_list('ai_usage', flat=True)
    for usage in usages:
        if isinstance(usage, dict):
            images = usage.get('images')
            if isinstance(images, int):
                total += images
    return total


def remaining_for_shop(shop):
    """Photos left this month; ``None`` when the shop is unlimited."""
    limit = get_shop_limit(shop)
    if limit is None:
        return None
    return max(0, limit - ai_images_used(shop))


def assert_quota(shop, needed):
    """Raise :class:`QuotaExhausted` when ``needed`` photos would cross the limit.

    The whole batch is stopped before any AI call: a partial run would spend
    credits and still leave the merchant without drafts.
    """
    limit = get_shop_limit(shop)
    if limit is None:
        return
    used = ai_images_used(shop)
    if used + needed > limit:
        raise QuotaExhausted(
            f'Kikomo cha picha za AI cha mwezi huu kimefikiwa ({used}/{limit}). '
            'Ingizo la QR bado linaweza kutumika. | Monthly AI photo limit '
            f'reached ({used}/{limit}). QR intake is still available.'
        )
