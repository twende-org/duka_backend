from django.db.models import Min, QuerySet, Sum, Value
from django.db.models.functions import Coalesce

# pyrefly: ignore [missing-import]
from apps.core.search import SearchField, search_queryset
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop

# Weights mirror the client-side rankBySearch field map the ranking primitive
# ports. SKU/barcode outweigh the product name so an exact code lookup lands on
# the first page ("barcode-first"); the relation paths require select_related.
PRODUCT_SEARCH_FIELDS = (
    SearchField('name', 10),
    SearchField('sku', 12),
    SearchField('barcode', 12),
    SearchField('brand', 7),
    SearchField('category__name', 6),
    SearchField('shop__name', 5),
    SearchField('shop__location', 3),
    SearchField('description', 2),
)


def get_public_products(*, shop: Shop) -> QuerySet[Product]:
    """Products a storefront may show for this shop.

    Mirrors ``getProductsByShop`` on Firestore exactly: every product with
    ``publishToDirectory !== false``, with no status filter — merchant surfaces
    (ShopCard, ShopProductGrid) show inactive rows and consumer pages filter
    ``status === 'active'`` client-side, so filtering here would change what
    those pages render after cutover.

    Admin shop approval is the gate: a shop the platform admin has not approved
    (``isPublic`` false — the AdminShops "Approve" toggle) serves nothing here,
    so its products never reach the marketplace home.

    ``stock``/``min_stock`` replace the separate ``shops/{id}/inventory`` read the
    storefront used to make: the pages merged the two client-side and hid rows at
    zero stock, so a product with no inventory row still reads as out of stock.
    """
    if not shop.isPublic:
        return Product.objects.none()
    return (
        Product.objects.filter(shop=shop, publish_to_directory=True)
        .select_related('category', 'shop')
        .annotate(
            stock=Sum('inventory__quantity', default=0),
            min_stock=Coalesce(Min('inventory__low_stock_threshold'), Value(5)),
        )
        .order_by('-created_at')
    )


def search_public_products(*, query: str = '', category: str = None,
                           shop: Shop = None):
    """Cross-shop storefront search over approved shops' directory products.

    Shares every gate with ``get_public_products`` (the shop is platform-approved
    and the product is published to the directory), so search can never surface a
    row the storefront itself would hide. Meaningful queries come back as a
    ranked list; an empty query falls through to browse order (newest first).
    """
    products = (
        Product.objects.filter(shop__isPublic=True, publish_to_directory=True)
        .select_related('category', 'shop')
        .annotate(
            stock=Sum('inventory__quantity', default=0),
            min_stock=Coalesce(Min('inventory__low_stock_threshold'), Value(5)),
        )
        .order_by('-created_at')
    )
    if shop is not None:
        products = products.filter(shop=shop)
    if category:
        products = products.filter(category__name__iexact=category)
    return search_queryset(products, query, PRODUCT_SEARCH_FIELDS)


def search_products(queryset, query: str):
    """Rank an already-gated product queryset for a ``?search=`` read."""
    return search_queryset(queryset, query, PRODUCT_SEARCH_FIELDS)
