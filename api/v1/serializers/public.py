"""Serializers for the unauthenticated storefront read surface.

Two rules drive the field choices here:

* The payload mirrors the Firestore document keys the public pages already read
  (camelCase), so the frontend adapters stay thin.
* Merchant-confidential fields never leave these serializers. The manager-facing
  ProductSerializer uses ``fields = '__all__'``, so buying price, supplier
  identity and sourcing links would leak to a public reader if it were reused.
"""
from rest_framework import serializers

from apps.core.legacy import LegacyPrimaryKeyRelatedField
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.sales.models import Order, OrderItem
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop


class PublicShopSerializer(serializers.ModelSerializer):
    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    whatsappNumber = serializers.CharField(source='whatsapp', read_only=True)
    followerCount = serializers.IntegerField(source='follower_count', read_only=True)
    # The storefront pages read these two merchant-configured blobs directly off
    # the shop document on Firestore; they carry no cost or customer data.
    storePolicies = serializers.JSONField(source='store_policies', read_only=True)
    onlineStore = serializers.JSONField(source='online_store_settings', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Shop
        fields = (
            'id', 'legacyId', 'name', 'slug', 'description',
            # country/region/district feed the address fallback in
            # src/lib/supplierMapping.ts (DiscoverSuppliers "link as supplier").
            'location', 'lat', 'lon', 'address', 'country', 'region', 'district',
            'phone', 'whatsappNumber', 'email', 'website', 'slogan',
            'operatingHours', 'businessType', 'productCondition',
            'facebookUrl', 'instagramUrl', 'tiktokUrl',
            'imageUrl', 'coverImage', 'followerCount',
            'isPublic', 'isWholesaleSupplier', 'keepsStock', 'trackInventory',
            'productCategories', 'businessCategories', 'shopTypes', 'customerTypes',
            'salesChannels', 'pricingModels', 'stockLocations', 'fulfillmentMethods',
            'serviceCoverage', 'inventoryModel', 'language',
            'currency', 'timezone', 'storePolicies', 'onlineStore',
            'createdAt', 'updatedAt',
        )


class PublicProductSerializer(serializers.ModelSerializer):
    """Catalog shape for storefront readers; cost and sourcing fields are omitted."""

    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    shopId = LegacyPrimaryKeyRelatedField(source='shop', read_only=True)
    categoryId = LegacyPrimaryKeyRelatedField(source='category', read_only=True)
    marketplaceCategoryId = serializers.CharField(source='marketplace_category_id', read_only=True)
    merchantCategoryId = serializers.CharField(source='merchant_category_id', read_only=True)
    # The storefront renders product.category as display text, like the manager UI.
    category = serializers.SerializerMethodField()
    categories = serializers.JSONField(source='marketplace_categories', read_only=True)
    sellingPrice = serializers.DecimalField(source='selling_price', max_digits=12, decimal_places=2, read_only=True)
    wholesalePrice = serializers.DecimalField(source='wholesale_price', max_digits=12, decimal_places=2, read_only=True)
    discount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    imageUrl = serializers.CharField(source='image_url', read_only=True)
    imageUrls = serializers.JSONField(source='image_urls', read_only=True)
    storeLocation = serializers.CharField(source='store_location', read_only=True)
    expiryDate = serializers.DateField(source='expiry_date', read_only=True)
    reviewCount = serializers.IntegerField(source='review_count', read_only=True)
    publishToDirectory = serializers.BooleanField(source='publish_to_directory', read_only=True)
    # Supplied by ``get_public_products``; they replace the separate inventory
    # read the storefront pages used to merge in client-side.
    stock = serializers.IntegerField(read_only=True)
    minStock = serializers.IntegerField(source='min_stock', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Product
        fields = (
            'id', 'legacyId', 'shopId', 'categoryId', 'category', 'categories',
            'marketplaceCategoryId', 'merchantCategoryId',
            'name', 'sku', 'barcode', 'description', 'brand',
            'condition', 'unit', 'weight', 'size', 'color', 'storeLocation',
            'moq', 'expiryDate', 'warranty', 'status',
            'sellingPrice', 'wholesalePrice', 'discount', 'prices',
            'imageUrl', 'imageUrls', 'rating', 'reviewCount',
            'publishToDirectory', 'tags', 'stock', 'minStock',
            'createdAt', 'updatedAt',
        )

    def get_category(self, obj):
        return obj.category.name if obj.category else None


class PublicWishlistOrderItemSerializer(serializers.Serializer):
    """One wishlist line. Only the quantity is honoured; the price is a fallback
    for products deleted since they were wishlisted (see ``create_storefront_order``),
    so the server re-prices every line that still has a product row.

    Both spellings are ``required=False`` and presence is checked in ``validate``
    because the camel and snake aliases share a source, exactly like
    ``CreateOrderItemInputSerializer`` in the sales serializers.
    """

    product_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    productId = serializers.CharField(source='product_id', max_length=255, required=False, allow_blank=True)
    product_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    productName = serializers.CharField(source='product_name', max_length=255, required=False, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    unitPrice = serializers.DecimalField(source='price', max_digits=12, decimal_places=2, required=False)

    def validate(self, attrs):
        if not attrs.get('product_id'):
            raise serializers.ValidationError({'productId': 'This field is required.'})
        return attrs


class PublicWishlistOrderInputSerializer(serializers.Serializer):
    """The wishlist-checkout payload, camelCase as the modal builds it.

    ``orderId`` is required and doubles as the idempotency key, mirroring the
    ``ORD-######`` the modal generated and stored on the Firestore document.
    """

    order_id = serializers.CharField(max_length=64, required=False, allow_blank=True)
    orderId = serializers.CharField(source='order_id', max_length=64, required=False, allow_blank=True)
    customer_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    customerName = serializers.CharField(source='customer_name', max_length=255, required=False, allow_blank=True)
    customer_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    customerPhone = serializers.CharField(source='customer_phone', max_length=20, required=False, allow_blank=True)
    customer_address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    customerAddress = serializers.CharField(source='customer_address', max_length=500, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    payment_method = serializers.CharField(max_length=50, required=False, allow_blank=True)
    paymentMethod = serializers.CharField(source='payment_method', max_length=50, required=False, allow_blank=True)
    customer_type = serializers.CharField(max_length=50, required=False, allow_blank=True)
    customerType = serializers.CharField(source='customer_type', max_length=50, required=False, allow_blank=True)
    items = PublicWishlistOrderItemSerializer(many=True, allow_empty=False)

    def validate(self, attrs):
        errors = {}
        for field, label in (('order_id', 'orderId'), ('customer_name', 'customerName'),
                             ('customer_phone', 'customerPhone')):
            if not attrs.get(field):
                errors[label] = 'This field is required.'
        if errors:
            raise serializers.ValidationError(errors)
        attrs['payment_method'] = attrs.get('payment_method') or 'Cash / WhatsApp'
        attrs['customer_type'] = attrs.get('customer_type') or 'retail'
        return attrs


class PublicOrderItemSerializer(serializers.ModelSerializer):
    """A priced order line; costs never travel with it."""

    productId = LegacyPrimaryKeyRelatedField(source='product', read_only=True)
    productName = serializers.CharField(source='product_name', read_only=True)
    unitPrice = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, read_only=True)
    price = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = ('id', 'productId', 'productName', 'quantity', 'unitPrice', 'price', 'subtotal')


class PublicOrderSerializer(serializers.ModelSerializer):
    """The storefront's own order, read back so the caller can build its receipt.

    Deliberately not ``sales.OrderSerializer``: that one carries the merchant's
    profit estimate, internal notes and cost-bearing item rows.
    """

    items = PublicOrderItemSerializer(many=True, read_only=True)
    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    orderId = serializers.CharField(source='legacy_id', read_only=True)
    shopId = LegacyPrimaryKeyRelatedField(source='shop', read_only=True)
    totalAmount = serializers.DecimalField(source='total_amount', max_digits=12, decimal_places=2, read_only=True)
    paymentMethod = serializers.CharField(source='payment_method', read_only=True)
    customerName = serializers.CharField(source='customer_name', read_only=True)
    customerPhone = serializers.CharField(source='customer_phone', read_only=True)
    customerType = serializers.CharField(source='customer_type', read_only=True)
    customerAddress = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = Order
        fields = (
            'id', 'legacyId', 'orderId', 'shopId', 'status', 'source',
            'subtotal', 'totalAmount', 'paymentMethod',
            'customerName', 'customerPhone', 'customerAddress', 'customerType',
            'notes', 'createdAt', 'items',
        )

    def get_customerAddress(self, obj):
        return (obj.fulfillment_details or {}).get('deliveryAddress') or None

