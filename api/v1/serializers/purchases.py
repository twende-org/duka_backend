from rest_framework import serializers
# pyrefly: ignore [missing-import]
from apps.core.legacy import LegacyPrimaryKeyRelatedField, app_id
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.purchases.models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseShipment, PurchaseShipmentItem, GRN, GRNItem,
)
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop


def normalize_item(item: dict) -> dict:
    """Accept a camelCase order/shipment/GRN line; snake_case keys win when both exist.

    The legacy documents carried camelCase line fields (``buyingPrice``,
    ``shippedQty``, ``acceptedQty``...), the services below read snake_case.
    """
    def pick(*keys, default=None):
        for key in keys:
            value = item.get(key)
            if value is not None:
                return value
        return default

    return {
        'product_id': pick('product_id', 'productId'),
        'source_product_id': pick('source_product_id', 'sourceProductId'),
        'product_name': pick('product_name', 'productName', default='Unknown Product'),
        'expected_qty': pick('expected_qty', 'expectedQty', default=0),
        'received_qty': pick('received_qty', 'receivedQty'),
        'shipped_qty': pick('shipped_qty', 'shippedQty', default=0),
        'accepted_qty': pick('accepted_qty', 'acceptedQty', default=0),
        'damaged_qty': pick('damaged_qty', 'damagedQty', default=0),
        'missing_qty': pick('missing_qty', 'missingQty', default=0),
        'rejected_qty': pick('rejected_qty', 'rejectedQty', default=0),
        'unit_cost': pick('unit_cost', 'buyingPrice', 'unitCost', default=0),
        'discount_amount': pick('discount_amount', 'discountAmount', default=0),
        'tax_amount': pick('tax_amount', 'taxAmount', default=0),
    }


class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore line shape the app reads; snake_case keys stay available."""

    productId = serializers.SerializerMethodField()
    sourceProductId = serializers.CharField(source='source_product_id', read_only=True)
    productName = serializers.CharField(source='product_name', read_only=True)
    expectedQty = serializers.IntegerField(source='expected_qty', read_only=True)
    receivedQty = serializers.IntegerField(source='received_qty', read_only=True)
    buyingPrice = serializers.DecimalField(source='unit_cost', max_digits=12, decimal_places=2, read_only=True)
    discountAmount = serializers.DecimalField(source='discount_amount', max_digits=12, decimal_places=2, read_only=True)
    taxAmount = serializers.DecimalField(source='tax_amount', max_digits=12, decimal_places=2, read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = PurchaseOrderItem
        fields = '__all__'
        read_only_fields = ('purchase_order',)

    def get_productId(self, obj):
        """The catalog reference the buyer ordered; the FK may be empty for free-text lines."""
        return obj.source_product_id or (str(obj.product_id) if obj.product_id else None)


class PurchaseOrderSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore documents the app reads; snake_case keys stay available."""

    buyerShopId = LegacyPrimaryKeyRelatedField(source='buyer_shop', queryset=Shop.objects.all(), required=False)
    supplierShopId = LegacyPrimaryKeyRelatedField(source='supplier_shop', queryset=Shop.objects.all(), required=False)
    supplierName = serializers.CharField(source='supplier_name', required=False, allow_blank=True, allow_null=True)
    taxAmount = serializers.DecimalField(source='tax_amount', max_digits=12, decimal_places=2, required=False)
    discountAmount = serializers.DecimalField(source='discount_amount', max_digits=12, decimal_places=2, required=False)
    shippingCost = serializers.DecimalField(source='shipping_cost', max_digits=12, decimal_places=2, required=False)
    totalAmount = serializers.DecimalField(source='total_amount', max_digits=12, decimal_places=2, required=False)
    paymentTerms = serializers.CharField(source='payment_terms', max_length=255, required=False, allow_blank=True, allow_null=True)
    items = PurchaseOrderItemSerializer(many=True, read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = '__all__'
        extra_kwargs = {
            'buyer_shop': {'required': False},
            'supplier_shop': {'required': False},
        }


class PurchaseShipmentItemSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore line shape the app reads; snake_case keys stay available."""

    productId = serializers.CharField(source='product_id', read_only=True)
    sourceProductId = serializers.CharField(source='source_product_id', read_only=True)
    productName = serializers.CharField(source='product_name', read_only=True)
    shippedQty = serializers.IntegerField(source='shipped_qty', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = PurchaseShipmentItem
        fields = '__all__'
        read_only_fields = ('shipment',)


class PurchaseShipmentSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore documents the app reads; snake_case keys stay available."""

    poId = LegacyPrimaryKeyRelatedField(source='purchase_order', queryset=PurchaseOrder.objects.all(), required=False)
    buyerShopId = serializers.SerializerMethodField()
    supplierShopId = serializers.SerializerMethodField()
    dispatchDate = serializers.DateTimeField(source='dispatch_date', required=False, allow_null=True)
    expectedArrivalDate = serializers.DateField(source='estimated_delivery', required=False, allow_null=True)
    trackingNumber = serializers.CharField(source='tracking_number', required=False, allow_blank=True, allow_null=True)
    driverName = serializers.CharField(source='driver_name', required=False, allow_blank=True, allow_null=True)
    vehicleNumber = serializers.CharField(source='vehicle_number', required=False, allow_blank=True, allow_null=True)
    supplierNotes = serializers.CharField(source='supplier_notes', required=False, allow_blank=True, allow_null=True)
    items = PurchaseShipmentItemSerializer(many=True, read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = PurchaseShipment
        fields = '__all__'
        extra_kwargs = {'purchase_order': {'required': False}}

    def get_buyerShopId(self, obj):
        return app_id(obj.purchase_order.buyer_shop) if obj.purchase_order_id else None

    def get_supplierShopId(self, obj):
        return app_id(obj.purchase_order.supplier_shop) if obj.purchase_order_id else None


class GRNItemSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore line shape the app reads; snake_case keys stay available."""

    productId = serializers.SerializerMethodField()
    productName = serializers.CharField(source='product_name', read_only=True)
    expectedQty = serializers.IntegerField(source='expected_qty', read_only=True)
    receivedQty = serializers.SerializerMethodField()
    acceptedQty = serializers.IntegerField(source='accepted_qty', read_only=True)
    rejectedQty = serializers.IntegerField(source='rejected_qty', read_only=True)
    unitCost = serializers.DecimalField(source='unit_cost', max_digits=12, decimal_places=2, read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = GRNItem
        fields = '__all__'

    def get_productId(self, obj):
        return str(obj.product_id) if obj.product_id else None

    def get_receivedQty(self, obj):
        # The wizard wrote receivedQty = accepted + damaged; the column pair is stored instead.
        return obj.accepted_qty + obj.damaged_qty


class GRNSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore documents the app reads; snake_case keys stay available."""

    poId = LegacyPrimaryKeyRelatedField(source='purchase_order', queryset=PurchaseOrder.objects.all(), required=False)
    shipmentId = LegacyPrimaryKeyRelatedField(source='shipment', queryset=PurchaseShipment.objects.all(), required=False, allow_null=True)
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    supplierId = LegacyPrimaryKeyRelatedField(source='supplier', queryset=Shop.objects.all(), required=False)
    supplierName = serializers.SerializerMethodField()
    items = GRNItemSerializer(many=True, read_only=True)
    completedAt = serializers.DateTimeField(source='completed_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = GRN
        fields = '__all__'
        extra_kwargs = {
            'purchase_order': {'required': False},
            'shop': {'required': False},
            'supplier': {'required': False},
        }

    def get_supplierName(self, obj):
        return obj.supplier.name if obj.supplier_id else None


class CreatePurchaseOrderInputSerializer(serializers.Serializer):
    """camelCase aliases accept the orders page payload; snake_case keys stay writable for internal callers."""

    buyer_shop_id = serializers.CharField(required=False, allow_blank=True)
    buyerShopId = serializers.CharField(source='buyer_shop_id', required=False, allow_blank=True)
    supplier_shop_id = serializers.CharField(required=False, allow_blank=True)
    supplierShopId = serializers.CharField(source='supplier_shop_id', required=False, allow_blank=True)
    supplier_name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    supplierName = serializers.CharField(source='supplier_name', max_length=255, required=False, allow_blank=True, allow_null=True)
    status = serializers.ChoiceField(choices=PurchaseOrder.STATUS_CHOICES, required=False)
    currency = serializers.CharField(max_length=10, required=False, allow_blank=True)
    payment_terms = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    paymentTerms = serializers.CharField(source='payment_terms', max_length=255, required=False, allow_blank=True, allow_null=True)
    tax_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    taxAmount = serializers.DecimalField(source='tax_amount', max_digits=12, decimal_places=2, required=False)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    discountAmount = serializers.DecimalField(source='discount_amount', max_digits=12, decimal_places=2, required=False)
    shipping_cost = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    shippingCost = serializers.DecimalField(source='shipping_cost', max_digits=12, decimal_places=2, required=False)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    items = serializers.ListField(child=serializers.DictField(), allow_empty=False)

    def validate(self, attrs):
        missing = {}
        if not attrs.get('buyer_shop_id'):
            missing['buyerShopId'] = 'This field is required.'
        if not attrs.get('supplier_shop_id'):
            missing['supplierShopId'] = 'This field is required.'
        if missing:
            raise serializers.ValidationError(missing)
        attrs['items'] = [normalize_item(item) for item in attrs['items']]
        return attrs


class UpdateOrderStatusInputSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=PurchaseOrder.STATUS_CHOICES)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class CreatePurchaseShipmentInputSerializer(serializers.Serializer):
    """camelCase aliases accept the shipment dialog payload; snake_case keys stay writable for internal callers."""

    purchase_order_id = serializers.CharField(required=False, allow_blank=True)
    poId = serializers.CharField(source='purchase_order_id', required=False, allow_blank=True)
    supplier_shop_id = serializers.CharField(required=False, allow_blank=True)
    supplierShopId = serializers.CharField(source='supplier_shop_id', required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=PurchaseShipment.STATUS_CHOICES, required=False)
    carrier = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    tracking_number = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    trackingNumber = serializers.CharField(source='tracking_number', max_length=100, required=False, allow_blank=True, allow_null=True)
    driver_name = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    driverName = serializers.CharField(source='driver_name', max_length=100, required=False, allow_blank=True, allow_null=True)
    vehicle_number = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    vehicleNumber = serializers.CharField(source='vehicle_number', max_length=100, required=False, allow_blank=True, allow_null=True)
    supplier_notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    supplierNotes = serializers.CharField(source='supplier_notes', required=False, allow_blank=True, allow_null=True)
    items = serializers.ListField(child=serializers.DictField(), allow_empty=False)

    def validate(self, attrs):
        if not attrs.get('purchase_order_id'):
            raise serializers.ValidationError({'poId': 'This field is required.'})
        attrs['items'] = [normalize_item(item) for item in attrs['items']]
        return attrs


class UpdateShipmentStatusInputSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=PurchaseShipment.STATUS_CHOICES)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class ProcessGRNInputSerializer(serializers.Serializer):
    """camelCase aliases accept the GRN wizard payload; snake_case keys stay writable for internal callers.

    ``supplierId`` is the platform supplier shop (as the wizard sends it); the
    matching CRM row, when one exists, is resolved from the PO's supplier shop.
    """

    purchase_order_id = serializers.CharField(required=False, allow_blank=True)
    poId = serializers.CharField(source='purchase_order_id', required=False, allow_blank=True)
    buyer_shop_id = serializers.CharField(required=False, allow_blank=True)
    shopId = serializers.CharField(source='buyer_shop_id', required=False, allow_blank=True)
    supplier_shop_id = serializers.CharField(required=False, allow_blank=True)
    supplierId = serializers.CharField(source='supplier_shop_id', required=False, allow_blank=True)
    shipment_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    shipmentId = serializers.CharField(source='shipment_id', required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    items = serializers.ListField(child=serializers.DictField(), allow_empty=False)

    def validate(self, attrs):
        missing = {}
        if not attrs.get('purchase_order_id'):
            missing['poId'] = 'This field is required.'
        if not attrs.get('buyer_shop_id'):
            missing['shopId'] = 'This field is required.'
        if missing:
            raise serializers.ValidationError(missing)
        attrs['items'] = [normalize_item(item) for item in attrs['items']]
        return attrs
