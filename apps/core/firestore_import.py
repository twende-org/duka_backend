"""Pure Firestore-document -> Django-field mapping used by ``import_firestore``.

Deliberately free of database, settings and Firebase imports: every rule is a
pure function that can be unit tested without a live project. Values are coerced
into what the Django columns accept; anything that cannot be represented is
dropped or clipped instead of aborting the whole import.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.utils import timezone as dj_timezone
from django.utils.text import slugify

# Firestore shops keep these under a single ``legal`` map.
SHOP_LEGAL_FIELDS = {
    'tin': ('tin_number', 50),
    'vrn': ('vrn_number', 50),
    'licenseNumber': ('license_number', 100),
    'registrationNumber': ('registration_number', 100),
}

PRODUCT_CONDITIONS = ('new', 'used', 'refurbished', 'rental', 'digital', 'service')
PRODUCT_STATUSES = ('active', 'inactive', 'discontinued')
MERCHANT_CATEGORY_STATUSES = ('active', 'inactive')
ACCOUNT_TYPES = ('merchant', 'staff', 'customer', 'unassigned')
SHOP_ROLES = ('owner', 'manager', 'attendant')
CUSTOMER_TYPES = ('retail', 'wholesale', 'corporate', 'reseller', 'distributor')
ORDER_SOURCES = ('public_storefront', 'in_app', 'wishlist')

# The Swahili/English spellings the POS has written into ``paymentMethod``.
PAYMENT_METHOD_ALIASES = {
    'taslimu': 'cash',
    'pesa taslimu': 'cash',
    'cash': 'cash',
    'm-pesa': 'mpesa',
    'mpesa': 'mpesa',
    'm pesa': 'mpesa',
    'tigo pesa': 'tigopesa',
    'tigopesa': 'tigopesa',
    'tigo': 'tigopesa',
    'airtel money': 'airtel_money',
    'airtel': 'airtel_money',
    'halopesa': 'halopesa',
    'halo pesa': 'halopesa',
    'halo': 'halopesa',
    'benki': 'bank',
    'bank': 'bank',
    'bank transfer': 'bank',
    'card': 'card',
    'mkopo': 'credit',
    'deni': 'credit',
    'debt': 'credit',
    'credit': 'credit',
    'credit / debt': 'credit',
}

# DecimalField ceilings: 12 digits / 2 places for money, 5 / 2 for rates.
MONEY_CEILING = Decimal('9999999999.99')
RATE_CEILING = Decimal('999.99')


def text(value, max_length=None):
    """Trimmed string for NOT NULL columns; '' when the source is missing."""
    if value is None:
        return ''
    out = value.strip() if isinstance(value, str) else str(value)
    if max_length is not None and len(out) > max_length:
        out = out[:max_length]
    return out


def optional_text(value, max_length=None):
    """Trimmed string for nullable columns; None when the source is empty."""
    out = text(value, max_length)
    return out or None


def decimal_value(value, ceiling=MONEY_CEILING, default=Decimal('0')):
    """Money/rate coercion: clamps to the column ceiling, floors at zero."""
    if value is None or value == '':
        return default
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    if not number.is_finite():
        return default
    number = number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if number < 0:
        return Decimal('0')
    if number > ceiling:
        return ceiling
    return number


def int_value(value, default=0, minimum=None, maximum=None):
    if value is None or value == '' or isinstance(value, bool):
        return default
    try:
        out = int(value)
    except (ValueError, TypeError):
        try:
            out = int(float(value))
        except (ValueError, TypeError):
            return default
    if minimum is not None and out < minimum:
        out = minimum
    if maximum is not None and out > maximum:
        out = maximum
    return out


def float_value(value, default=0.0):
    if value is None or value == '' or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def float_or_none(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def bool_value(value, default=False):
    return value if isinstance(value, bool) else default


def list_value(value):
    return list(value) if isinstance(value, (list, tuple)) else []


def dict_value(value):
    return dict(value) if isinstance(value, dict) else {}


def date_value(value):
    """'' -> None; datetimes keep their date; ISO and d/m/Y strings are accepted."""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def datetime_value(value):
    """Firestore timestamps (aware) pass through; strings are parsed as ISO."""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        out = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            out = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            return None
    if dj_timezone.is_naive(out):
        out = dj_timezone.make_aware(out)
    return out


def choice_value(value, choices, default):
    raw = str(value).strip().lower() if value not in (None, '') else ''
    return raw if raw in choices else default


def unique_slug(value, taken, fallback='shop'):
    """Slugified value that avoids ``taken``; the chosen slug is added to it."""
    base = (slugify(value or '') or fallback)[:200]
    candidate = base
    index = 1
    while candidate in taken:
        index += 1
        candidate = f'{base}-{index}'
    taken.add(candidate)
    return candidate


def shop_kwargs(doc_id, data, taken_slugs=None):
    taken_slugs = set() if taken_slugs is None else taken_slugs
    legal = dict_value(data.get('legal'))
    kwargs = {
        'name': text(data.get('name'), 255) or f'Shop {doc_id[:8]}',
        'description': text(data.get('description')),
        'country': text(data.get('country'), 100),
        'region': text(data.get('region'), 100),
        'district': text(data.get('district'), 100),
        'slug': unique_slug(data.get('slug') or data.get('name'), taken_slugs),
        'phone': optional_text(data.get('phone'), 20),
        'whatsapp': optional_text(data.get('whatsappNumber'), 20),
        'email': optional_text(data.get('email'), 254),
        'website': optional_text(data.get('website'), 200),
        'slogan': optional_text(data.get('slogan'), 255),
        'operatingHours': optional_text(data.get('operatingHours')),
        'businessType': optional_text(data.get('businessType'), 100),
        'productCondition': optional_text(data.get('productCondition'), 50),
        'instagramUrl': optional_text(data.get('instagramUrl'), 200),
        'facebookUrl': optional_text(data.get('facebookUrl'), 200),
        'tiktokUrl': optional_text(data.get('tiktokUrl'), 200),
        'inventoryModel': optional_text(data.get('inventoryModel'), 50),
        'language': optional_text(data.get('language'), 50),
        'keepsStock': bool_value(data.get('keepsStock')),
        'isPublic': bool_value(data.get('isPublic')),
        'trackInventory': bool_value(data.get('trackInventory')),
        'isWholesaleSupplier': bool_value(data.get('isWholesaleSupplier')),
        'address': optional_text(data.get('address')),
        'location': optional_text(data.get('location'), 255),
        'lat': float_or_none(data.get('lat')),
        'lon': float_or_none(data.get('lon')),
        'shopTypes': list_value(data.get('shopTypes')),
        'productCategories': list_value(data.get('productCategories')),
        'customerTypes': list_value(data.get('customerTypes')),
        'salesChannels': list_value(data.get('salesChannels')),
        'pricingModels': list_value(data.get('pricingModels')),
        'stockLocations': list_value(data.get('stockLocations')),
        'fulfillmentMethods': list_value(data.get('fulfillmentMethods')),
        'serviceCoverage': list_value(data.get('serviceCoverage')),
        'businessCategories': list_value(data.get('businessCategories')),
        'productCapabilities': dict_value(data.get('productCapabilities')),
        'online_store_settings': dict_value(data.get('online_store_settings')),
        'store_policies': dict_value(data.get('store_policies')),
        'ai_marketing_settings': dict_value(data.get('ai_marketing_settings')),
        'social_links': dict_value(data.get('social_links')),
        'imageUrl': optional_text(data.get('imageUrl')),
        'coverImage': optional_text(data.get('coverImage')),
        'currency': text(data.get('currency'), 10) or 'TZS',
        'timezone': text(data.get('timezone'), 50) or 'Africa/Dar_es_Salaam',
        'follower_count': int_value(data.get('followerCount'), minimum=0),
    }
    for legacy_key, (field, limit) in SHOP_LEGAL_FIELDS.items():
        kwargs[field] = optional_text(legal.get(legacy_key), limit)
    return kwargs


def category_name(data):
    """The legacy product.category is a display string, not a document id."""
    return optional_text(data.get('category'), 255)


def product_kwargs(doc_id, data, shop, category=None):
    status = choice_value(data.get('status'), PRODUCT_STATUSES, 'active')
    return {
        'shop': shop,
        'category': category,
        'name': text(data.get('name'), 255) or f'Product {doc_id[:8]}',
        'sku': text(data.get('sku'), 100),
        'barcode': text(data.get('barcode'), 100),
        'description': text(data.get('description')),
        'brand': text(data.get('brand'), 100),
        'marketplace_categories': list_value(data.get('categories')),
        'marketplace_category_id': optional_text(data.get('marketplaceCategoryId'), 255),
        'merchant_category_id': optional_text(data.get('merchantCategoryId'), 255),
        'condition': choice_value(data.get('condition'), PRODUCT_CONDITIONS, None),
        'unit': optional_text(data.get('unit'), 100),
        'weight': optional_text(data.get('weight'), 100),
        'size': optional_text(data.get('size'), 100),
        'color': optional_text(data.get('color'), 100),
        'store_location': optional_text(data.get('storeLocation'), 100),
        'moq': int_value(data.get('moq'), default=1, minimum=1),
        'expiry_date': date_value(data.get('expiryDate')),
        'warranty': optional_text(data.get('warranty'), 255),
        'status': status,
        'buying_price': decimal_value(data.get('buyingPrice')),
        'selling_price': decimal_value(data.get('sellingPrice')),
        'wholesale_price': decimal_value(data.get('wholesalePrice')),
        'discount': decimal_value(data.get('discount')),
        'tax_rate': decimal_value(data.get('taxRate'), ceiling=RATE_CEILING),
        'prices': list_value(data.get('prices')),
        'image_url': optional_text(data.get('imageUrl')),
        'image_urls': list_value(data.get('imageUrls')),
        'rating': float_value(data.get('rating')),
        'review_count': int_value(data.get('reviewCount')),
        'publish_to_facebook': bool_value(data.get('publishToFacebook')),
        # The storefront read is ``publishToDirectory !== false`` (getProductsByShop),
        # so an absent field means the product IS in the directory.
        'publish_to_directory': bool_value(data.get('publishToDirectory'), default=True),
        # The legacy sync trigger and the product form both read a missing flag
        # as "published" (deliverySync.js checks `=== false`), so an absent field
        # must not import as opted-out.
        'publish_to_delivery_app': bool_value(data.get('publishToDeliveryApp'), default=True),
        'attributes': dict_value(data.get('attributes')),
        'variants': list_value(data.get('variants')),
        # The form stores '' when untouched and a list when used; keep whichever
        # shape the document has so the UI keeps rendering it the same way.
        'tags': data.get('tags') if data.get('tags') is not None else '',
        'supplier': text(data.get('supplier'), 255),
        'source_product_id': optional_text(data.get('sourceProductId'), 255),
        'supplier_shop_id': optional_text(data.get('supplierShopId'), 255),
        'is_active': status == 'active',
    }


def merchant_category_kwargs(doc_id, data, shop):
    name = text(data.get('name'), 255) or f'Category {doc_id[:8]}'
    return {
        'shop': shop,
        'name': name,
        'slug': text(data.get('slug'), 255) or (slugify(name)[:255] or 'category'),
        'description': optional_text(data.get('description')),
        'sort_order': int_value(data.get('sortOrder')),
        'status': choice_value(data.get('status'), MERCHANT_CATEGORY_STATUSES, 'active'),
    }


def user_kwargs(uid, data, email):
    return {
        'email': email,
        'username': email,
        'display_name': optional_text(data.get('displayName'), 255),
        'phone': optional_text(data.get('phone'), 20),
        'account_type': choice_value(data.get('accountType'), ACCOUNT_TYPES, 'merchant'),
        'firebase_uid': uid,
    }


def role_value(value, default='attendant'):
    return choice_value(value, SHOP_ROLES, default)


def payment_method_value(value, default='cash'):
    """Normalize the POS spellings onto the Django vocabulary.

    Unknown values are kept (lowercased) instead of dropped: a sale that read
    "Split" on Firestore must still say something recognizable after cutover.
    """
    raw = text(value, 50)
    if not raw:
        return default
    key = ' '.join(raw.lower().replace('_', ' ').split())
    return PAYMENT_METHOD_ALIASES.get(key, raw.lower()[:50])


def sale_status_value(value):
    """Firestore sales are either a completed sale or a held draft."""
    return 'draft' if text(value).lower() == 'draft' else 'completed'


def order_status_value(value, default='pending'):
    """Order statuses pass through verbatim.

    The lifecycle has states Django's choices do not hold (``allocated``,
    ``in_transit``, ``paid``) and the portal renders the raw string, so mapping
    them would change what the UI shows after cutover.
    """
    return text(value, 50).lower() or default


def line_items(data):
    """Line items from either shape Firestore holds.

    Modern documents carry an ``items`` array; legacy single-product documents
    keep ``productId``/``quantity``/``totalPrice`` at the top level. Item keys
    vary too (``price``/``unitPrice``, ``subtotal``/``totalPrice``), so each
    value is looked up under both names.
    """
    raw = data.get('items')
    if not isinstance(raw, list) or not raw:
        raw = [data] if (data.get('productId') or data.get('productName')) else []
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        quantity = int_value(entry.get('quantity'), default=1, minimum=1)
        unit = entry.get('price', entry.get('unitPrice'))
        subtotal_raw = entry.get('subtotal', entry.get('totalPrice'))
        if unit in (None, ''):
            unit = decimal_value(subtotal_raw) / quantity
        unit_price = decimal_value(unit)
        subtotal = decimal_value(subtotal_raw) if subtotal_raw not in (None, '') else unit_price * quantity
        items.append({
            'productId': text(entry.get('productId'), 64),
            'productName': text(entry.get('productName'), 255),
            'quantity': quantity,
            'picked_qty': int_value(entry.get('pickedQty'), minimum=0),
            'unit_price': unit_price,
            'subtotal': subtotal,
        })
    return items


def sale_item_kwargs(item, product=None):
    return {
        'product': product,
        'product_name': item['productName'],
        'quantity': item['quantity'],
        'unit_price': item['unit_price'],
        'total_price': item['subtotal'],
    }


def order_item_kwargs(item, product=None):
    return {
        'product': product,
        'product_name': item['productName'],
        'quantity': item['quantity'],
        'picked_qty': item['picked_qty'],
        'unit_price': item['unit_price'],
        'subtotal': item['subtotal'],
    }


def branch_kwargs(doc_id, data, shop, manager=None):
    return {
        'shop': shop,
        'name': text(data.get('name'), 255) or f'Branch {doc_id[:8]}',
        'location': text(data.get('location'), 255),
        'phone': text(data.get('phone'), 20),
        'is_main': bool_value(data.get('isMain')),
        'manager': manager,
        'is_active': bool_value(data.get('isActive'), default=True),
        'branch_type': text(data.get('type'), 100) or 'Storefront',
        'timezone': optional_text(data.get('timezone'), 50),
        'operating_hours': optional_text(data.get('operatingHours')),
        'features': dict_value(data.get('features')),
    }


def customer_kwargs(doc_id, data, shop, balance=None):
    """Customer row; the optional ``customer_balances`` doc wins for money.

    ``outstandingBalance`` is only maintained on the balance document, which
    exists solely once a credit sale happened, so the customer doc is the
    fallback for shops that never used credit.
    """
    balance = dict_value(balance)
    return {
        'shop': shop,
        'name': text(data.get('name'), 255) or f'Customer {doc_id[:8]}',
        'phone': text(data.get('phone'), 20),
        'email': text(data.get('email'), 254),
        'customer_type': choice_value(data.get('customerType'), CUSTOMER_TYPES, 'retail'),
        'credit_limit': decimal_value(data.get('creditLimit')),
        'outstanding_balance': decimal_value(
            balance.get('outstandingBalance', data.get('outstandingBalance'))),
        'business_name': text(data.get('businessName'), 255),
        'contact_person': text(data.get('contactPerson'), 255),
        'registration_number': text(data.get('registrationNumber'), 255),
        'commercial_settings': dict_value(data.get('commercialSettings')),
        'address': text(data.get('address')),
        'notes': text(data.get('notes')),
        'total_purchases': int_value(data.get('totalPurchases'), minimum=0),
        'total_spent': decimal_value(data.get('totalSpent')),
        'last_purchase_date': date_value(data.get('lastPurchaseDate')),
        # Firebase uid of the buyer's account; ``User.firebase_uid`` matches it.
        'user_id': optional_text(data.get('userId'), 255),
        'linked_at': datetime_value(data.get('linkedAt')),
    }


def sale_kwargs(data, shop, branch, attendant=None, customer_user=None):
    """A ``sales_days/*/sales`` document. Per-sale profit is not stored there."""
    return {
        'shop': shop,
        'branch': branch,
        'attendant': attendant,
        'total_amount': decimal_value(data.get('totalPrice')),
        'discount_amount': decimal_value(data.get('discount')),
        'payment_method': payment_method_value(data.get('paymentMethod')),
        'customer_id': optional_text(data.get('customerId'), 255),
        'customer_name': optional_text(data.get('customerName'), 255),
        'customer_phone': optional_text(data.get('customerPhone'), 20),
        'customer_user': customer_user,
        'notes': optional_text(data.get('notes')),
        'status': sale_status_value(data.get('status')),
    }


def order_kwargs(data, shop, branch, customer_user=None):
    return {
        'shop': shop,
        'branch': branch,
        'idempotency_key': optional_text(data.get('idempotencyKey'), 255),
        'subtotal': decimal_value(data.get('subtotal')),
        'tax': decimal_value(data.get('tax')),
        'discount': decimal_value(data.get('discount')),
        'total_amount': decimal_value(data.get('totalAmount')),
        'profit_estimate': decimal_value(data.get('profitEstimate')),
        'payment_method': text(data.get('paymentMethod'), 50) or 'Cash',
        'status': order_status_value(data.get('status')),
        'approval_status': optional_text(data.get('approvalStatus'), 50),
        'source': choice_value(data.get('source'), ORDER_SOURCES, 'in_app'),
        'customer_id': optional_text(data.get('customerId'), 255),
        'customer_name': optional_text(data.get('customerName'), 255),
        'customer_phone': optional_text(data.get('customerPhone'), 20),
        'customer_type': optional_text(data.get('customerType'), 50),
        'customer_po_number': optional_text(data.get('customerPoNumber'), 100),
        'required_delivery_date': date_value(data.get('requiredDeliveryDate')),
        'customer_user': customer_user,
        'salesperson_id': optional_text(data.get('salespersonId'), 255),
        'internal_notes': optional_text(data.get('internalNotes')),
        'notes': optional_text(data.get('notes')),
        'fulfillment_details': dict_value(data.get('fulfillment')),
    }
