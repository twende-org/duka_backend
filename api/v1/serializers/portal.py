"""Customer-portal payloads, shaped like the Firestore documents the pages read.

The portal pages were written against Firestore documents, so these serializers
keep the legacy field names (``orderId``, ``totalAmount``, ``itemsCount``, …) and
the legacy app-visible ids (``legacyId`` first, Django uuid as fallback).
"""
from django.utils import timezone
from rest_framework import serializers

# pyrefly: ignore [missing-import]
from apps.sales.models import Order, OrderItem, Sale, SaleItem

MONEY = dict(max_digits=12, decimal_places=2, coerce_to_string=False)


def app_id(instance) -> str:
    """The id the app already knows the row by: legacy Firestore id, else uuid."""
    return instance.legacy_id or str(instance.pk)


class PortalItemBaseSerializer(serializers.Serializer):
    """A receipt/order line; the product name is a snapshot, product optional."""

    productId = serializers.SerializerMethodField()
    productName = serializers.SerializerMethodField()
    price = serializers.DecimalField(source='unit_price', **MONEY)

    def get_productId(self, obj):
        return app_id(obj.product) if obj.product_id else None

    def get_productName(self, obj):
        return obj.product_name or (obj.product.name if obj.product_id else '')


class PortalReceiptItemSerializer(PortalItemBaseSerializer, serializers.ModelSerializer):
    subtotal = serializers.DecimalField(source='total_price', **MONEY)

    class Meta:
        model = SaleItem
        fields = ('productId', 'productName', 'quantity', 'price', 'subtotal')


class PortalOrderItemSerializer(PortalItemBaseSerializer, serializers.ModelSerializer):
    subtotal = serializers.DecimalField(**MONEY)

    class Meta:
        model = OrderItem
        fields = ('productId', 'productName', 'quantity', 'price', 'subtotal')


class PortalReceiptSerializer(serializers.ModelSerializer):
    """A sale as the customer sees it: ``getCustomerReceipts`` document shape."""

    id = serializers.SerializerMethodField()
    shopId = serializers.SerializerMethodField()
    shopName = serializers.CharField(source='shop.name')
    date = serializers.SerializerMethodField()
    total = serializers.DecimalField(source='total_amount', **MONEY)
    paymentMethod = serializers.CharField(source='payment_method')
    itemsCount = serializers.SerializerMethodField()
    items = PortalReceiptItemSerializer(many=True, read_only=True)
    createdAt = serializers.DateTimeField(source='created_at')

    class Meta:
        model = Sale
        fields = ('id', 'shopId', 'shopName', 'date', 'total', 'paymentMethod',
                  'itemsCount', 'items', 'createdAt')

    def get_id(self, obj):
        return app_id(obj)

    def get_shopId(self, obj):
        return app_id(obj.shop)

    def get_date(self, obj):
        """The day the receipt belongs to, in the active timezone."""
        if not obj.created_at:
            return ''
        return timezone.localtime(obj.created_at).date().isoformat()

    def get_itemsCount(self, obj):
        return len(obj.items.all())


class PortalOrderSerializer(serializers.ModelSerializer):
    """An order as the customer's "my orders" page reads it."""

    id = serializers.SerializerMethodField()
    orderId = serializers.SerializerMethodField()
    shopId = serializers.SerializerMethodField()
    shopName = serializers.CharField(source='shop.name')
    totalAmount = serializers.DecimalField(source='total_amount', **MONEY)
    fulfillment = serializers.JSONField(source='fulfillment_details')
    items = PortalOrderItemSerializer(many=True, read_only=True)
    createdAt = serializers.DateTimeField(source='created_at')

    class Meta:
        model = Order
        fields = ('id', 'orderId', 'shopId', 'shopName', 'createdAt', 'totalAmount',
                  'status', 'source', 'fulfillment', 'items')

    def get_id(self, obj):
        return app_id(obj)

    def get_orderId(self, obj):
        """The human order number (``ORD-123456``) when the row carries one."""
        return app_id(obj)

    def get_shopId(self, obj):
        return app_id(obj.shop)
