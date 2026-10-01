from django.db.models import Min, QuerySet, Sum, Value
from django.db.models.functions import Coalesce

# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop


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
