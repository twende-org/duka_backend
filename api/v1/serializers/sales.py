from django.utils import timezone
from rest_framework import serializers
from apps.sales.models import Sale, SaleItem, Order, OrderItem
from apps.products.models import Product
from apps.shops.models import Shop, Branch
from apps.core.legacy import LegacyPrimaryKeyRelatedField
from apps.sales.services import display_payment_method, normalize_payment_method
from api.v1.serializers.products import BlankableDateField


def _user_display_name(user):
    if user is None:
        return ''
    return getattr(user, 'display_name', '') or user.get_full_name() or user.username


class SaleItemSerializer(serializers.ModelSerializer):
    # camelCase aliases mirror the Firestore cart lines the app reads: ``price``
    # is the list selling price and ``subtotal`` the discounted line total.
    productId = LegacyPrimaryKeyRelatedField(source='product', read_only=True)
    productName = serializers.CharField(source='product_name', read_only=True)
    unitPrice = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, read_only=True)
    price = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, read_only=True)
    subtotal = serializers.DecimalField(source='total_price', max_digits=12, decimal_places=2, read_only=True)
    totalPrice = serializers.DecimalField(source='total_price', max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = SaleItem
        fields = '__all__'


class SaleSerializer(serializers.ModelSerializer):
    items = SaleItemSerializer(many=True, read_only=True)
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    branchId = LegacyPrimaryKeyRelatedField(source='branch', queryset=Branch.objects.all(), required=False)
    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    #: Shifts carry no Firestore id, so this mirrors the uuid the app opened them with.
    shiftId = LegacyPrimaryKeyRelatedField(source='shift', read_only=True)
    totalAmount = serializers.DecimalField(source='total_amount', max_digits=12, decimal_places=2, read_only=True)
    #: The app's name for the sale total; Firestore stored it as ``totalPrice``.
    totalPrice = serializers.DecimalField(source='total_amount', max_digits=12, decimal_places=2, read_only=True)
    discountAmount = serializers.DecimalField(source='discount_amount', max_digits=12, decimal_places=2, read_only=True)
    #: Cashiers must see the label they tapped, not the stored slug.
    paymentMethod = serializers.SerializerMethodField()
    #: Firestore stored the day on the document; here it is derived from ``createdAt``.
    date = serializers.SerializerMethodField()
    customerId = serializers.CharField(source='customer_id', read_only=True)
    customerName = serializers.CharField(source='customer_name', read_only=True)
    customerPhone = serializers.CharField(source='customer_phone', read_only=True)
    createdBy = LegacyPrimaryKeyRelatedField(source='attendant', read_only=True)
    createdByName = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Sale
        fields = '__all__'
        extra_kwargs = {'shop': {'required': False}, 'branch': {'required': False}}

    def get_paymentMethod(self, obj):
        return display_payment_method(obj.payment_method)

    def get_date(self, obj):
        if obj.created_at is None:
            return None
        return timezone.localtime(obj.created_at).date().isoformat()

    def get_createdByName(self, obj):
        return _user_display_name(obj.attendant)


class POSSaleItemInputSerializer(serializers.Serializer):
    """camelCase aliases accept the POS cart payload; snake_case keys stay writable for internal callers."""

    product_id = serializers.CharField(required=False, allow_blank=True)
    productId = serializers.CharField(source='product_id', required=False, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    unitPrice = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, required=False)
    price = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, required=False)
    #: The cart's discounted line total (``lineTotal``); missing means no discount.
    subtotal = serializers.DecimalField(source='total_price', max_digits=12, decimal_places=2, required=False)
    totalPrice = serializers.DecimalField(source='total_price', max_digits=12, decimal_places=2, required=False)

    def validate(self, attrs):
        errors = {}
        if not attrs.get('product_id'):
            errors['productId'] = 'This field is required.'
        if attrs.get('unit_price') is None:
            errors['price'] = 'This field is required.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

class POSSaleInputSerializer(serializers.Serializer):
    """camelCase aliases accept the POS payload; snake_case keys stay writable for internal callers."""

    shop_id = serializers.CharField(required=False, allow_blank=True)
    shopId = serializers.CharField(source='shop_id', required=False, allow_blank=True)
    #: Sales.tsx posts ``""`` when the shop has no branches picked yet; blanks fall
    #: back to the shop's main branch.
    branch_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    branchId = serializers.CharField(source='branch_id', required=False, allow_blank=True, allow_null=True)

    payment_method = serializers.CharField(max_length=50, required=False, allow_blank=True)
    paymentMethod = serializers.CharField(source='payment_method', max_length=50, required=False, allow_blank=True)

    items = POSSaleItemInputSerializer(many=True, allow_empty=False)

    customer_id = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    customerId = serializers.CharField(source='customer_id', required=False, allow_null=True, allow_blank=True, max_length=255)
    customer_name = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    customerName = serializers.CharField(source='customer_name', required=False, allow_null=True, allow_blank=True, max_length=255)
    customer_phone = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=20)
    customerPhone = serializers.CharField(source='customer_phone', required=False, allow_null=True, allow_blank=True, max_length=20)
    #: Set when the customer is a business on the platform: their delivery
    #: manifest is staged automatically from this sale's lines.
    buyer_shop_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    buyerShopId = serializers.CharField(source='buyer_shop_id', required=False, allow_null=True, allow_blank=True)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    shift_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    shiftId = serializers.CharField(source='shift_id', required=False, allow_null=True, allow_blank=True)

    #: ``addDraftSale`` posted the whole cart with ``status: "draft"``; the same
    #: endpoint serves both, and the view routes drafts to the draft service.
    status = serializers.ChoiceField(choices=[c[0] for c in Sale.SALE_STATUS], required=False, default='completed')

    def validate(self, attrs):
        errors = {}
        if not attrs.get('shop_id') and not attrs.get('branch_id'):
            errors['shopId'] = 'This field is required.'
        if errors:
            raise serializers.ValidationError(errors)
        attrs['payment_method'] = normalize_payment_method(attrs.get('payment_method'))
        return attrs
class OrderItemSerializer(serializers.ModelSerializer):
    # camelCase aliases mirror the Firestore shape the app reads (``productId``
    # keys the picking dialog rows, ``price`` is what the receipt prints).
    productId = LegacyPrimaryKeyRelatedField(source='product', read_only=True)
    productName = serializers.CharField(source='product_name', read_only=True)
    pickedQty = serializers.IntegerField(source='picked_qty', read_only=True)
    unitPrice = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, read_only=True)
    price = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = '__all__'

class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    branchId = LegacyPrimaryKeyRelatedField(source='branch', queryset=Branch.objects.all(), required=False)
    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    idempotencyKey = serializers.CharField(source='idempotency_key', read_only=True)
    customerId = serializers.CharField(source='customer_id', read_only=True)
    customerName = serializers.CharField(source='customer_name', read_only=True)
    customerPhone = serializers.CharField(source='customer_phone', read_only=True)
    customerType = serializers.CharField(source='customer_type', read_only=True)
    customerPoNumber = serializers.CharField(source='customer_po_number', read_only=True)
    requiredDeliveryDate = serializers.DateField(source='required_delivery_date', read_only=True)
    salespersonId = serializers.CharField(source='salesperson_id', read_only=True)
    internalNotes = serializers.CharField(source='internal_notes', read_only=True)
    totalAmount = serializers.DecimalField(source='total_amount', max_digits=12, decimal_places=2, read_only=True)
    profitEstimate = serializers.DecimalField(source='profit_estimate', max_digits=12, decimal_places=2, read_only=True)
    paymentMethod = serializers.CharField(source='payment_method', read_only=True)
    approvalStatus = serializers.CharField(source='approval_status', read_only=True)
    fulfillment = serializers.JSONField(source='fulfillment_details', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)
    paidAt = serializers.DateTimeField(source='paid_at', read_only=True)
    #: Firestore kept the buyer's address at the top level; Django has no column
    #: for it, so it rides in ``fulfillmentDetails`` (the order wizard writes
    #: ``deliveryNotes``, the storefront checkout ``deliveryAddress``) and
    #: Orders.tsx reads the flat key off the order dialog.
    customerAddress = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = '__all__'
        extra_kwargs = {'shop': {'required': False}, 'branch': {'required': False}}

    def get_customerAddress(self, obj):
        details = obj.fulfillment_details or {}
        return details.get('deliveryAddress') or details.get('deliveryNotes') or None

class CreateOrderItemInputSerializer(serializers.Serializer):
    """camelCase aliases accept the order-wizard payload; snake_case keys stay writable for internal callers."""

    product_id = serializers.CharField(required=False, allow_blank=True)
    productId = serializers.CharField(source='product_id', required=False, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1)
    #: The wizard sends the tier-derived price as ``price``; ``unitPrice``/``unit_price``
    #: are kept for internal callers. Legacy ``createOrder`` ignored it entirely and
    #: re-read ``sellingPrice`` — this port honours the caller, per its own TODO.
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    unitPrice = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, required=False)
    price = serializers.DecimalField(source='unit_price', max_digits=12, decimal_places=2, required=False)

    def validate(self, attrs):
        errors = {}
        if not attrs.get('product_id'):
            errors['productId'] = 'This field is required.'
        if attrs.get('unit_price') is None:
            errors['price'] = 'This field is required.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

class CreateB2BOrderInputSerializer(serializers.Serializer):
    """camelCase aliases accept the order-wizard payload; snake_case keys stay writable for internal callers."""

    shop_id = serializers.CharField(required=False, allow_blank=True)
    shopId = serializers.CharField(source='shop_id', required=False, allow_blank=True)
    #: The wizard posts ``""`` when "all branches" is selected, so blanks fall back
    #: to the shop's main branch; legacy stored the blank verbatim.
    branch_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    branchId = serializers.CharField(source='branch_id', required=False, allow_blank=True, allow_null=True)

    idempotency_key = serializers.CharField(max_length=255, required=False, allow_blank=True)
    idempotencyKey = serializers.CharField(source='idempotency_key', max_length=255, required=False, allow_blank=True)
    payment_method = serializers.CharField(max_length=50, required=False, allow_blank=True)
    paymentMethod = serializers.CharField(source='payment_method', max_length=50, required=False, allow_blank=True)

    items = CreateOrderItemInputSerializer(many=True, allow_empty=False)
    approval_status = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=50)
    approvalStatus = serializers.CharField(source='approval_status', required=False, allow_blank=True, allow_null=True, max_length=50)

    customer_id = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    customerId = serializers.CharField(source='customer_id', required=False, allow_null=True, allow_blank=True, max_length=255)
    customer_name = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    customerName = serializers.CharField(source='customer_name', required=False, allow_null=True, allow_blank=True, max_length=255)
    customer_phone = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=20)
    customerPhone = serializers.CharField(source='customer_phone', required=False, allow_null=True, allow_blank=True, max_length=20)
    customer_type = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=50)
    customerType = serializers.CharField(source='customer_type', required=False, allow_null=True, allow_blank=True, max_length=50)
    customer_po_number = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=100)
    customerPoNumber = serializers.CharField(source='customer_po_number', required=False, allow_null=True, allow_blank=True, max_length=100)
    required_delivery_date = BlankableDateField(required=False, allow_null=True)
    requiredDeliveryDate = BlankableDateField(source='required_delivery_date', required=False, allow_null=True)
    salesperson_id = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    salespersonId = serializers.CharField(source='salesperson_id', required=False, allow_null=True, allow_blank=True, max_length=255)
    internal_notes = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    internalNotes = serializers.CharField(source='internal_notes', required=False, allow_null=True, allow_blank=True)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    fulfillment_details = serializers.JSONField(required=False, allow_null=True)
    fulfillment = serializers.JSONField(source='fulfillment_details', required=False, allow_null=True)
    source = serializers.ChoiceField(choices=Order.SOURCE_CHOICES, required=False, default='in_app')

    def validate(self, attrs):
        errors = {}
        if not attrs.get('shop_id') and not attrs.get('branch_id'):
            errors['shopId'] = 'This field is required.'
        if not attrs.get('idempotency_key'):
            errors['idempotencyKey'] = 'This field is required.'
        if errors:
            raise serializers.ValidationError(errors)
        attrs['payment_method'] = attrs.get('payment_method') or 'Cash'
        return attrs

class UpdateFulfillmentItemSerializer(serializers.Serializer):
    #: PickingDialog patches each line by the product it belongs to and sends no
    #: row id; internal callers may target the OrderItem by its own id instead.
    id = serializers.UUIDField(required=False)
    product_id = serializers.CharField(required=False, allow_blank=True)
    productId = serializers.CharField(source='product_id', required=False, allow_blank=True)
    picked_qty = serializers.IntegerField(min_value=0, required=False)
    pickedQty = serializers.IntegerField(source='picked_qty', min_value=0, required=False)

class UpdateFulfillmentStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Order.ORDER_STATUS)
    fulfillment_data = serializers.JSONField(required=False, allow_null=True)
    fulfillmentData = serializers.JSONField(source='fulfillment_data', required=False, allow_null=True)
    items = UpdateFulfillmentItemSerializer(many=True, required=False, allow_empty=True)

class PayOrderInputSerializer(serializers.Serializer):
    """Settles a pending order; Firestore's ``payOrder`` only ever sent these two."""

    payment_method = serializers.CharField(max_length=50, required=False, allow_blank=True)
    paymentMethod = serializers.CharField(source='payment_method', max_length=50, required=False, allow_blank=True)
    shift_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    shiftId = serializers.CharField(source='shift_id', required=False, allow_null=True, allow_blank=True)

    def validate(self, attrs):
        attrs['payment_method'] = attrs.get('payment_method') or 'Cash'
        return attrs
