from decimal import Decimal

from rest_framework import serializers

from apps.core.legacy import LegacyPrimaryKeyRelatedField
# pyrefly: ignore [missing-import]
from apps.purchases.models import (
    B2BConnection,
    B2BSupplierBalance,
    B2BSupplierInvoice,
    B2BSupplierPayment,
    PurchaseOrder,
)
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop


class B2BConnectionSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore documents the app reads; snake_case keys stay available."""

    buyerShopId = LegacyPrimaryKeyRelatedField(source='buyer_shop', queryset=Shop.objects.all(), required=False)
    supplierShopId = LegacyPrimaryKeyRelatedField(source='supplier_shop', queryset=Shop.objects.all(), required=False)
    requestedBy = serializers.CharField(source='requested_by', read_only=True)
    approvedBy = serializers.CharField(source='approved_by', read_only=True)
    pricingTier = serializers.CharField(source='pricing_tier', read_only=True)
    paymentType = serializers.CharField(source='payment_type', read_only=True)
    creditLimit = serializers.DecimalField(source='credit_limit', max_digits=14, decimal_places=2, read_only=True)
    creditDays = serializers.IntegerField(source='credit_days', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = B2BConnection
        fields = '__all__'
        extra_kwargs = {
            'buyer_shop': {'required': False},
            'supplier_shop': {'required': False},
        }


class B2BSupplierBalanceSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore documents the app reads; snake_case keys stay available."""

    buyerShopId = LegacyPrimaryKeyRelatedField(source='buyer_shop', queryset=Shop.objects.all(), required=False)
    supplierShopId = LegacyPrimaryKeyRelatedField(source='supplier_shop', queryset=Shop.objects.all(), required=False)
    supplierName = serializers.CharField(source='supplier_name', read_only=True)
    totalPurchases = serializers.IntegerField(source='total_purchases', read_only=True)
    receivedGoodsValue = serializers.DecimalField(source='received_goods_value', max_digits=14, decimal_places=2, read_only=True)
    outstandingBalance = serializers.DecimalField(source='outstanding_balance', max_digits=14, decimal_places=2, read_only=True)
    paidAmount = serializers.DecimalField(source='paid_amount', max_digits=14, decimal_places=2, read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = B2BSupplierBalance
        fields = '__all__'
        extra_kwargs = {
            'buyer_shop': {'required': False},
            'supplier_shop': {'required': False},
        }


class B2BSupplierInvoiceSerializer(serializers.ModelSerializer):
    """camelCase aliases accept the frontend payload; snake_case keys stay writable for internal callers."""

    buyerShopId = LegacyPrimaryKeyRelatedField(source='buyer_shop', queryset=Shop.objects.all(), required=False)
    supplierShopId = LegacyPrimaryKeyRelatedField(source='supplier_shop', queryset=Shop.objects.all(), required=False)
    purchaseOrderId = LegacyPrimaryKeyRelatedField(source='purchase_order', queryset=PurchaseOrder.objects.all(), required=False, allow_null=True)
    grnIds = serializers.JSONField(source='grn_ids', required=False)
    invoiceNumber = serializers.CharField(source='invoice_number', max_length=255, required=False)
    invoiceDate = serializers.DateField(source='invoice_date', required=False, allow_null=True)
    dueDate = serializers.DateField(source='due_date', required=False, allow_null=True)
    attachmentUrl = serializers.CharField(source='attachment_url', max_length=500, required=False, allow_blank=True, allow_null=True)
    taxAmount = serializers.DecimalField(source='tax_amount', max_digits=14, decimal_places=2, required=False)
    discountAmount = serializers.DecimalField(source='discount_amount', max_digits=14, decimal_places=2, required=False)
    totalAmount = serializers.DecimalField(source='total_amount', max_digits=14, decimal_places=2, required=False)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = B2BSupplierInvoice
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {
            'buyer_shop': {'required': False},
            'supplier_shop': {'required': False},
        }

    def validate(self, attrs):
        if self.instance is None:
            missing = {}
            for key, alias in (('buyer_shop', 'buyerShopId'), ('supplier_shop', 'supplierShopId'), ('invoice_number', 'invoiceNumber')):
                if key not in attrs:
                    missing[alias] = 'This field is required.'
            if missing:
                raise serializers.ValidationError(missing)
        return attrs


class B2BSupplierPaymentSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore documents the app reads; snake_case keys stay available."""

    buyerShopId = LegacyPrimaryKeyRelatedField(source='buyer_shop', queryset=Shop.objects.all(), required=False)
    supplierShopId = LegacyPrimaryKeyRelatedField(source='supplier_shop', queryset=Shop.objects.all(), required=False)
    invoiceIds = serializers.JSONField(source='invoice_ids', required=False)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = B2BSupplierPayment
        fields = '__all__'
        extra_kwargs = {
            'buyer_shop': {'required': False},
            'supplier_shop': {'required': False},
        }


class RequestConnectionInputSerializer(serializers.Serializer):
    """camelCase aliases accept the app payload; snake_case keys stay writable for internal callers."""

    buyer_shop_id = serializers.CharField(required=False)
    buyerShopId = serializers.CharField(source='buyer_shop_id', required=False)
    supplier_shop_id = serializers.CharField(required=False)
    supplierShopId = serializers.CharField(source='supplier_shop_id', required=False)
    requested_by = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    requestedBy = serializers.CharField(source='requested_by', required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        missing = {}
        if not attrs.get('buyer_shop_id'):
            missing['buyerShopId'] = 'This field is required.'
        if not attrs.get('supplier_shop_id'):
            missing['supplierShopId'] = 'This field is required.'
        if missing:
            raise serializers.ValidationError(missing)
        return attrs


class ApproveConnectionInputSerializer(serializers.Serializer):
    pricing_tier = serializers.ChoiceField(
        choices=[choice[0] for choice in B2BConnection.PRICING_TIER_CHOICES],
        required=False,
    )
    pricingTier = serializers.ChoiceField(
        source='pricing_tier',
        choices=[choice[0] for choice in B2BConnection.PRICING_TIER_CHOICES],
        required=False,
    )
    approved_by = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    approvedBy = serializers.CharField(source='approved_by', required=False, allow_blank=True, allow_null=True)


class CreateSupplierInvoiceInputSerializer(serializers.Serializer):
    """camelCase aliases accept the CreateInvoiceDialog payload; snake_case keys stay writable."""

    buyer_shop_id = serializers.CharField(required=False)
    buyerShopId = serializers.CharField(source='buyer_shop_id', required=False)
    supplier_shop_id = serializers.CharField(required=False)
    supplierShopId = serializers.CharField(source='supplier_shop_id', required=False)
    purchase_order_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    purchaseOrderId = serializers.CharField(source='purchase_order_id', required=False, allow_blank=True, allow_null=True)
    grn_ids = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)
    grnIds = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)
    invoice_number = serializers.CharField(max_length=255, required=False)
    invoiceNumber = serializers.CharField(source='invoice_number', max_length=255, required=False)
    invoice_date = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    invoiceDate = serializers.CharField(source='invoice_date', required=False, allow_blank=True, allow_null=True)
    due_date = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    dueDate = serializers.CharField(source='due_date', required=False, allow_blank=True, allow_null=True)
    currency = serializers.CharField(max_length=8, required=False, allow_blank=True)
    subtotal = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    tax_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    taxAmount = serializers.DecimalField(source='tax_amount', max_digits=14, decimal_places=2, required=False)
    discount_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    discountAmount = serializers.DecimalField(source='discount_amount', max_digits=14, decimal_places=2, required=False)
    total_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    totalAmount = serializers.DecimalField(source='total_amount', max_digits=14, decimal_places=2, required=False)
    attachment_url = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    attachmentUrl = serializers.CharField(source='attachment_url', required=False, allow_blank=True, allow_null=True)
    status = serializers.ChoiceField(choices=[choice[0] for choice in B2BSupplierInvoice.STATUS_CHOICES], required=False)

    def validate(self, attrs):
        missing = {}
        for key, alias in (
            ('buyer_shop_id', 'buyerShopId'),
            ('supplier_shop_id', 'supplierShopId'),
            ('invoice_number', 'invoiceNumber'),
        ):
            if not attrs.get(key):
                missing[alias] = 'This field is required.'
        if missing:
            raise serializers.ValidationError(missing)
        if 'grn_ids' not in attrs and 'grnIds' in attrs:
            attrs['grn_ids'] = attrs.pop('grnIds')
        else:
            attrs.pop('grnIds', None)
        attrs['grn_ids'] = [str(value) for value in attrs.get('grn_ids') or []]
        return attrs


class UpdateSupplierInvoiceStatusInputSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[choice[0] for choice in B2BSupplierInvoice.STATUS_CHOICES])
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class ProcessSupplierPaymentInputSerializer(serializers.Serializer):
    """camelCase aliases accept the RecordPaymentDialog payload; snake_case keys stay writable."""

    buyer_shop_id = serializers.CharField(required=False)
    buyerShopId = serializers.CharField(source='buyer_shop_id', required=False)
    supplier_shop_id = serializers.CharField(required=False)
    supplierShopId = serializers.CharField(source='supplier_shop_id', required=False)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    method = serializers.ChoiceField(choices=B2BSupplierPayment.PAYMENT_METHODS)
    reference = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    invoice_ids = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)
    invoiceIds = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)
    date = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    allow_overpayment = serializers.BooleanField(required=False, default=False)
    allowOverpayment = serializers.BooleanField(source='allow_overpayment', required=False, default=False)

    def validate(self, attrs):
        missing = {}
        for key, alias in (
            ('buyer_shop_id', 'buyerShopId'),
            ('supplier_shop_id', 'supplierShopId'),
        ):
            if not attrs.get(key):
                missing[alias] = 'This field is required.'
        if missing:
            raise serializers.ValidationError(missing)
        if 'invoice_ids' not in attrs and 'invoiceIds' in attrs:
            attrs['invoice_ids'] = attrs.pop('invoiceIds')
        else:
            attrs.pop('invoiceIds', None)
        attrs['invoice_ids'] = [str(value) for value in attrs.get('invoice_ids') or []]
        return attrs
