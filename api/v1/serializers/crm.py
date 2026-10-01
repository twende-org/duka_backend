from decimal import Decimal

from rest_framework import serializers
from apps.core.legacy import LegacyPrimaryKeyRelatedField
from apps.crm.models import Customer, CustomerInvoice, CustomerPayment, Supplier, SupplierInvoice, SupplierPayment
# pyrefly: ignore [missing-import]
from apps.purchases.models import PurchaseOrder
# pyrefly: ignore [missing-import]
from apps.sales.models import Order
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop

class CustomerSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore documents the app reads; snake_case keys stay available."""

    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    customerType = serializers.CharField(source='customer_type', read_only=True)
    creditLimit = serializers.DecimalField(source='credit_limit', max_digits=12, decimal_places=2, read_only=True)
    outstandingBalance = serializers.DecimalField(source='outstanding_balance', max_digits=12, decimal_places=2, read_only=True)
    businessName = serializers.CharField(source='business_name', read_only=True)
    contactPerson = serializers.CharField(source='contact_person', read_only=True)
    registrationNumber = serializers.CharField(source='registration_number', read_only=True)
    commercialSettings = serializers.JSONField(source='commercial_settings', read_only=True)
    totalPurchases = serializers.IntegerField(source='total_purchases', read_only=True)
    totalSpent = serializers.DecimalField(source='total_spent', max_digits=12, decimal_places=2, read_only=True)
    lastPurchaseDate = serializers.DateField(source='last_purchase_date', read_only=True)
    userId = serializers.CharField(source='user_id', read_only=True)
    linkedAt = serializers.DateTimeField(source='linked_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Customer
        fields = '__all__'
        extra_kwargs = {'shop': {'required': False}}

class CustomerInvoiceSerializer(serializers.ModelSerializer):
    """camelCase aliases accept the frontend payload; snake_case keys stay writable for internal callers."""

    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    customerId = LegacyPrimaryKeyRelatedField(source='customer', queryset=Customer.objects.all(), required=False)
    orderId = LegacyPrimaryKeyRelatedField(source='order', queryset=Order.objects.all(), required=False, allow_null=True)
    amountDue = serializers.DecimalField(source='amount_due', max_digits=12, decimal_places=2, required=False)
    amountPaid = serializers.DecimalField(source='amount_paid', max_digits=12, decimal_places=2, required=False)
    dueDate = serializers.DateField(source='due_date', required=False, allow_null=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = CustomerInvoice
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {
            'shop': {'required': False},
            'customer': {'required': False},
            'amount_due': {'required': False},
        }

    def validate(self, attrs):
        if self.instance is None:
            missing = {}
            for key, alias in (('shop', 'shopId'), ('customer', 'customerId'), ('amount_due', 'amountDue')):
                if key not in attrs:
                    missing[alias] = 'This field is required.'
            if missing:
                raise serializers.ValidationError(missing)

        shop = attrs.get('shop') or (self.instance.shop if self.instance else None)
        customer = attrs.get('customer') or (self.instance.customer if self.instance else None)
        order = attrs.get('order', self.instance.order if self.instance else None)

        if shop and customer and customer.shop_id != shop.id:
            raise serializers.ValidationError({'customerId': 'Customer does not belong to this shop.'})
        if shop and order and order.shop_id != shop.id:
            raise serializers.ValidationError({'orderId': 'Order does not belong to this shop.'})
        return attrs

class CustomerPaymentSerializer(serializers.ModelSerializer):
    """camelCase aliases accept the frontend payload; snake_case keys stay writable for internal callers."""

    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    customerId = LegacyPrimaryKeyRelatedField(source='customer', queryset=Customer.objects.all(), required=False)
    invoiceIds = LegacyPrimaryKeyRelatedField(source='invoices', queryset=CustomerInvoice.objects.all(), many=True, required=False)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = CustomerPayment
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {
            'shop': {'required': False},
            'customer': {'required': False},
        }

class CreateCustomerInputSerializer(serializers.Serializer):
    """camelCase aliases accept the wizard/POS payload; snake_case keys stay writable for internal callers."""

    shop_id = serializers.CharField(required=False, allow_blank=True)
    shopId = serializers.CharField(source='shop_id', required=False, allow_blank=True)
    name = serializers.CharField(max_length=255)
    #: POS checkout creates walk-in customers with empty contact details.
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    customer_type = serializers.ChoiceField(choices=Customer.CUSTOMER_TYPES, required=False)
    customerType = serializers.ChoiceField(source='customer_type', choices=Customer.CUSTOMER_TYPES, required=False)
    credit_limit = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    creditLimit = serializers.DecimalField(source='credit_limit', max_digits=12, decimal_places=2, required=False)
    business_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    businessName = serializers.CharField(source='business_name', max_length=255, required=False, allow_blank=True)
    contact_person = serializers.CharField(max_length=255, required=False, allow_blank=True)
    contactPerson = serializers.CharField(source='contact_person', max_length=255, required=False, allow_blank=True)
    registration_number = serializers.CharField(max_length=255, required=False, allow_blank=True)
    registrationNumber = serializers.CharField(source='registration_number', max_length=255, required=False, allow_blank=True)
    commercial_settings = serializers.JSONField(required=False)
    commercialSettings = serializers.JSONField(source='commercial_settings', required=False)
    address = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get('shop_id'):
            raise serializers.ValidationError({'shopId': 'This field is required.'})
        return attrs

class UpdateCustomerInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    customer_type = serializers.ChoiceField(choices=Customer.CUSTOMER_TYPES, required=False)
    customerType = serializers.ChoiceField(source='customer_type', choices=Customer.CUSTOMER_TYPES, required=False)
    credit_limit = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    creditLimit = serializers.DecimalField(source='credit_limit', max_digits=12, decimal_places=2, required=False)
    business_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    businessName = serializers.CharField(source='business_name', max_length=255, required=False, allow_blank=True)
    contact_person = serializers.CharField(max_length=255, required=False, allow_blank=True)
    contactPerson = serializers.CharField(source='contact_person', max_length=255, required=False, allow_blank=True)
    registration_number = serializers.CharField(max_length=255, required=False, allow_blank=True)
    registrationNumber = serializers.CharField(source='registration_number', max_length=255, required=False, allow_blank=True)
    commercial_settings = serializers.JSONField(required=False)
    commercialSettings = serializers.JSONField(source='commercial_settings', required=False)
    address = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        # The wizard echoes shopId back on every save; the row keeps its shop.
        attrs.pop('shop_id', None)
        return attrs

class ProcessCustomerPaymentInputSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    method = serializers.ChoiceField(choices=CustomerPayment.PAYMENT_METHODS)
    reference = serializers.CharField(max_length=255, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    invoice_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True
    )
    invoiceIds = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True
    )

    def validate(self, attrs):
        if 'invoice_ids' not in attrs and 'invoiceIds' in attrs:
            attrs['invoice_ids'] = attrs.pop('invoiceIds')
        else:
            attrs.pop('invoiceIds', None)
        return attrs

class SupplierSerializer(serializers.ModelSerializer):
    """camelCase aliases mirror the Firestore documents the app reads; snake_case keys stay available."""

    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    platformShopId = serializers.CharField(source='platform_shop_id', read_only=True)
    ownerId = serializers.CharField(source='owner_id', read_only=True)
    outstandingBalance = serializers.DecimalField(source='outstanding_balance', max_digits=12, decimal_places=2, read_only=True)
    totalPurchases = serializers.IntegerField(source='total_purchases', read_only=True)
    totalSpent = serializers.DecimalField(source='total_spent', max_digits=12, decimal_places=2, read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Supplier
        fields = '__all__'
        extra_kwargs = {'shop': {'required': False}}

class CreateSupplierInputSerializer(serializers.Serializer):
    """camelCase aliases accept the suppliers page payload; snake_case keys stay writable for internal callers."""

    shop_id = serializers.CharField(required=False, allow_blank=True)
    shopId = serializers.CharField(source='shop_id', required=False, allow_blank=True)
    name = serializers.CharField(max_length=255)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    products = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    owner_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    ownerId = serializers.CharField(source='owner_id', required=False, allow_blank=True, allow_null=True)
    platform_shop_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    platformShopId = serializers.CharField(source='platform_shop_id', required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        if not attrs.get('shop_id'):
            raise serializers.ValidationError({'shopId': 'This field is required.'})
        return attrs

class UpdateSupplierInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    products = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    owner_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    ownerId = serializers.CharField(source='owner_id', required=False, allow_blank=True, allow_null=True)
    platform_shop_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    platformShopId = serializers.CharField(source='platform_shop_id', required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        # The suppliers page echoes shopId back on every save; the row keeps its shop.
        attrs.pop('shop_id', None)
        return attrs

class SupplierInvoiceSerializer(serializers.ModelSerializer):
    """camelCase aliases accept the frontend payload; snake_case keys stay writable for internal callers."""

    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    supplierId = LegacyPrimaryKeyRelatedField(source='supplier', queryset=Supplier.objects.all(), required=False)
    purchaseOrderId = LegacyPrimaryKeyRelatedField(source='purchase_order', queryset=PurchaseOrder.objects.all(), required=False, allow_null=True)
    amountDue = serializers.DecimalField(source='amount_due', max_digits=12, decimal_places=2, required=False)
    amountPaid = serializers.DecimalField(source='amount_paid', max_digits=12, decimal_places=2, required=False)
    dueDate = serializers.DateField(source='due_date', required=False, allow_null=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = SupplierInvoice
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {
            'shop': {'required': False},
            'supplier': {'required': False},
            'amount_due': {'required': False},
        }

    def validate(self, attrs):
        if self.instance is None:
            missing = {}
            for key, alias in (('shop', 'shopId'), ('supplier', 'supplierId'), ('amount_due', 'amountDue')):
                if key not in attrs:
                    missing[alias] = 'This field is required.'
            if missing:
                raise serializers.ValidationError(missing)

        shop = attrs.get('shop') or (self.instance.shop if self.instance else None)
        supplier = attrs.get('supplier') or (self.instance.supplier if self.instance else None)
        purchase_order = attrs.get('purchase_order', self.instance.purchase_order if self.instance else None)

        if shop and supplier and supplier.shop_id != shop.id:
            raise serializers.ValidationError({'supplierId': 'Supplier does not belong to this shop.'})
        if shop and purchase_order and purchase_order.buyer_shop_id != shop.id:
            raise serializers.ValidationError({'purchaseOrderId': 'Purchase order does not belong to this shop.'})
        return attrs

class SupplierPaymentSerializer(serializers.ModelSerializer):
    """camelCase aliases accept the frontend payload; snake_case keys stay writable for internal callers."""

    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    supplierId = LegacyPrimaryKeyRelatedField(source='supplier', queryset=Supplier.objects.all(), required=False)
    invoiceIds = LegacyPrimaryKeyRelatedField(source='invoices', queryset=SupplierInvoice.objects.all(), many=True, required=False)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = SupplierPayment
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {
            'shop': {'required': False},
            'supplier': {'required': False},
        }

class ProcessSupplierPaymentInputSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    method = serializers.ChoiceField(choices=SupplierPayment.PAYMENT_METHODS)
    reference = serializers.CharField(max_length=255, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    invoice_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True
    )
    invoiceIds = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True
    )

    def validate(self, attrs):
        if 'invoice_ids' not in attrs and 'invoiceIds' in attrs:
            attrs['invoice_ids'] = attrs.pop('invoiceIds')
        else:
            attrs.pop('invoiceIds', None)
        return attrs
