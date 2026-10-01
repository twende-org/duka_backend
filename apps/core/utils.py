from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.core.legacy import resolve_legacy_pk
from apps.shops.models import Shop


def first_param(params, *names):
    """Return the first non-empty value for the given keys.

    Lets API filters accept both snake_case and camelCase query params
    (e.g. shop_id and shopId) without duplicating lookups per view.
    """
    for name in names:
        value = params.get(name)
        if value:
            return value
    return None


def resolve_shop(value, alias='shopId'):
    """Resolve an app-visible shop id (uuid or Firestore id) or 400 with a field error."""
    pk = resolve_legacy_pk(Shop, value)
    shop = Shop.objects.filter(pk=pk).first() if pk else None
    if shop is None:
        raise DRFValidationError({alias: 'Shop not found.'})
    return shop


def drf_validation_error(exc):
    """Translate a service ValidationError into a DRF 400.

    Handles core Django ValidationError (.messages) and DRF ValidationError
    (.detail). Keeps the {"detail": [...]} envelope the API and tests rely on.
    """
    messages = getattr(exc, 'messages', None)
    if messages is None:
        detail = getattr(exc, 'detail', exc)
        messages = detail if isinstance(detail, (list, tuple)) else [detail]
    return DRFValidationError({"detail": list(messages)})
