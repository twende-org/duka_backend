"""Port of the legacy ``functions/src/sync/deliverySync.js`` listeners.

The Delivery App is a *separate* Firebase project (``fast-tz``) that mirrors
published products for a courier marketplace. The Cloud Function kept the two
projects in sync with Firestore triggers; here the same translation runs on
Django signals and the remote writes happen in Celery, so an unreachable remote
project can never stall an API request.

Everything that leaves this deployment sits behind ``DELIVERY_APP_SYNC_ENABLED``
(default off) plus a service-account file: with either missing the push
functions raise ``DeliverySyncDisabled`` and no external call is made.

Two deliberate deviations from the Cloud Function, both about *deletes*:

* ``publish_to_delivery_app`` is a Django boolean, so "never set" and "explicitly
  off" are the same value. The legacy update trigger deleted the remote document
  whenever the flag read false, which here would delete products that were
  merely imported without the toggle. The port deletes only on a true -> false
  transition; a false -> false save is a no-op.
* The legacy create trigger overwrote the whole remote document (``set`` without
  merge), which wiped delivery-app-only keys such as ``fav``. The port always
  merges; for a document that does not exist yet the two are equivalent.
"""
import logging
import os
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

# pyrefly: ignore [missing-import]
from apps.products.models import Inventory

logger = logging.getLogger(__name__)

# The Delivery App files every marketplace product under one top category and
# carries the merchant's own category in ``subCat`` (legacy behaviour).
DELIVERY_CATEGORY = 'Product'
SUBCAT_FALLBACK = 'General'
STORE_FALLBACK = 'Unknown Store'

# A Firestore batch accepts 500 writes; the legacy migration script used 400.
DEFAULT_BATCH_SIZE = 400

_client = None


class DeliverySyncDisabled(RuntimeError):
    """Raised when a remote write is attempted while the sync is switched off."""


def get_delivery_client():
    """Firestore client for the Delivery App project, or raise when disabled.

    The named ``firebase_admin`` app is created lazily so an installation that
    never enables the sync never loads the service account.
    """
    if not settings.DELIVERY_APP_SYNC_ENABLED:
        raise DeliverySyncDisabled(
            'DELIVERY_APP_SYNC_ENABLED is off; refusing to write to the Delivery App.',
        )

    global _client
    if _client is None:
        import firebase_admin
        from firebase_admin import credentials, firestore

        app_name = 'delivery-app-sync'
        try:
            app = firebase_admin.get_app(app_name)
        except ValueError:
            path = settings.DELIVERY_APP_CREDENTIALS
            if not path or not os.path.exists(path):
                raise DeliverySyncDisabled(
                    f'Delivery App credentials not found at {path!r}.',
                )
            app = firebase_admin.initialize_app(
                credentials.Certificate(path),
                options={'projectId': settings.DELIVERY_APP_PROJECT_ID},
                name=app_name,
            )
        _client = firestore.client(app=app)
    return _client


def products_collection():
    return get_delivery_client().collection(settings.DELIVERY_APP_COLLECTION)


def document_key(row) -> str:
    """Firestore document id for a shop or product row."""
    return str(row.legacy_id or row.pk)


def shop_location(shop) -> str:
    """Legacy ``location`` field: ``"lat,lon"`` when both coordinates exist."""
    if shop.lat is None or shop.lon is None:
        return ''
    return f'{shop.lat},{shop.lon}'


def shop_owner_uid(shop) -> str:
    """Firebase uid of the shop owner (legacy ``shopData.ownerId``)."""
    role = shop.user_roles.filter(role='owner').select_related('user').first()
    if role is None:
        return ''
    user = role.user
    return str(user.firebase_uid or user.pk)


def product_quantity(product) -> int:
    """Shop-wide stock for the remote ``quantity`` field.

    Firestore kept one inventory document per shop; Django tracks stock per
    branch, so the shop total is their sum.
    """
    total = Inventory.objects.filter(product=product).aggregate(total=Sum('quantity'))['total']
    return int(total or 0)


def _first_string(values) -> str:
    if isinstance(values, (list, tuple)) and values and isinstance(values[0], str):
        return values[0].strip()
    return ''


def translate_product(product, shop=None) -> dict:
    """Map a Django product onto the Delivery App document (legacy schema)."""
    shop = shop or product.shop

    subcat = product.category.name if product.category_id and product.category else ''
    if not subcat:
        subcat = _first_string(product.marketplace_categories)
    if not subcat:
        subcat = (product.marketplace_category_id or '').strip()
    if not subcat:
        subcat = SUBCAT_FALLBACK

    image = (product.image_url or '').strip()
    if not image:
        image = _first_string(product.image_urls)

    cat = _first_string(shop.productCategories) or DELIVERY_CATEGORY
    selling_price = Decimal(product.selling_price or 0)

    return {
        'availability': product.status == 'active',
        'brand': product.brand or '',
        'cat': cat,
        'category': DELIVERY_CATEGORY,
        'description': product.description or '',
        'fav': False,
        'foodId': document_key(product),
        'imgURL': [image] if image else [],
        'location': shop_location(shop),
        'name': product.name or '',
        'price': float(selling_price),
        'quantity': product_quantity(product),
        'rate': [float(product.rating or 0)],
        'store': shop.name or STORE_FALLBACK,
        'subCat': subcat,
        'time': timezone.now().isoformat(),
        'uid': shop_owner_uid(shop),
        'shopId': document_key(shop),
        'oldprice': float(selling_price + Decimal(product.discount or 0)),
    }


def push_product(product, shop=None) -> dict:
    """Mirror one product document into the Delivery App (merge)."""
    payload = translate_product(product, shop=shop)
    products_collection().document(document_key(product)).set(payload, merge=True)
    return payload


def remove_product(product_key) -> None:
    """Drop a mirrored product document (legacy product-delete trigger)."""
    products_collection().document(str(product_key)).delete()


def push_stock(product) -> int:
    """Mirror only the quantity field, like the legacy inventory triggers."""
    quantity = product_quantity(product)
    products_collection().document(document_key(product)).set({'quantity': quantity}, merge=True)
    return quantity


def push_shop_identity(shop, updates) -> int:
    """Re-stamp ``updates`` on every mirrored product of ``shop``.

    Mirrors ``syncShopOnUpdate``: only the fields that actually changed are
    written, in batches that respect the Firestore write limit.
    """
    if not updates:
        return 0

    shop_key = document_key(shop)
    docs = list(products_collection().where('shopId', '==', shop_key).stream())
    if not docs:
        return 0

    batch_size = max(1, int(getattr(settings, 'DELIVERY_APP_BATCH_SIZE', DEFAULT_BATCH_SIZE)))
    client = get_delivery_client()
    written = 0
    for start in range(0, len(docs), batch_size):
        batch = client.batch()
        for doc in docs[start:start + batch_size]:
            batch.set(doc.reference, updates, merge=True)
        batch.commit()
        written += len(docs[start:start + batch_size])
    return written
