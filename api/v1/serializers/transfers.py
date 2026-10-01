from rest_framework import serializers

from apps.core.legacy import LegacyPrimaryKeyRelatedField
from apps.products.b2b_services import suggest_product_for_item
from apps.products.models import B2BStockTransfer, B2BStockTransferItem, Branch, Product
from apps.shops.models import Shop, UserRole


class B2BStockTransferItemSerializer(serializers.ModelSerializer):
    # productName/sku/unit are snapshotted from the product by the service,
    # so they stay optional on input and read-only-ish on output.
    productId = LegacyPrimaryKeyRelatedField(source='product', queryset=Product.objects.all())
    productName = serializers.CharField(source='product_name', required=False, max_length=255)
    unitCost = serializers.DecimalField(source='unit_cost', max_digits=12, decimal_places=2, required=False)
    sku = serializers.CharField(required=False, allow_blank=True, max_length=100)
    barcode = serializers.CharField(read_only=True)
    unit = serializers.CharField(required=False, allow_blank=True, max_length=100)
    mappedProductId = serializers.PrimaryKeyRelatedField(source='mapped_product', read_only=True)
    mappedProductName = serializers.SerializerMethodField()
    suggestedProductId = serializers.SerializerMethodField()
    suggestedProductName = serializers.SerializerMethodField()
    receivedProductId = serializers.PrimaryKeyRelatedField(source='received_product', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = B2BStockTransferItem
        fields = (
            'id', 'productId', 'productName', 'sku', 'barcode', 'unit', 'quantity',
            'unitCost', 'mappedProductId', 'mappedProductName',
            'suggestedProductId', 'suggestedProductName',
            'receivedProductId', 'createdAt', 'updatedAt',
        )
        read_only_fields = ('id', 'createdAt', 'updatedAt')

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError('Quantity must be at least 1.')
        return value

    def get_mappedProductName(self, obj):
        return obj.mapped_product.name if obj.mapped_product else None

    def get_suggestedProductId(self, obj):
        product = self._suggestion(obj)
        return product.pk if product else None

    def get_suggestedProductName(self, obj):
        product = self._suggestion(obj)
        return product.name if product else None

    def _suggestion(self, obj):
        """SKU/name prefill, only for the buyer on a pending, unmapped line."""
        if obj.mapped_product_id is not None or obj.transfer.status != 'pending':
            return None
        cache = self.context.setdefault('_b2b_suggestions', {})
        if obj.id not in cache:
            cache[obj.id] = _buyer_suggestion(self.context.get('request'), obj)
        return cache[obj.id]


def _buyer_suggestion(request, item):
    """The mapping suggestion leaks receiver-catalogue names, so compute it
    only for viewers holding a role in the receiving shop."""
    user = getattr(request, 'user', None)
    transfer = item.transfer
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    if not user.is_superuser and not UserRole.objects.filter(
            user=user, shop_id=transfer.to_shop_id).exists():
        return None
    return suggest_product_for_item(transfer.to_shop, item)


class B2BTransferMapItemSerializer(serializers.Serializer):
    itemId = LegacyPrimaryKeyRelatedField(
        source='item', queryset=B2BStockTransferItem.objects.all())
    productId = LegacyPrimaryKeyRelatedField(
        source='product', queryset=Product.objects.all(), allow_null=True)


class B2BTransferMapSerializer(serializers.Serializer):
    items = B2BTransferMapItemSerializer(many=True)


class B2BStockTransferSerializer(serializers.ModelSerializer):
    fromShopId = LegacyPrimaryKeyRelatedField(source='from_shop', queryset=Shop.objects.all())
    toShopId = LegacyPrimaryKeyRelatedField(source='to_shop', queryset=Shop.objects.all())
    fromBranchId = LegacyPrimaryKeyRelatedField(source='from_branch', queryset=Branch.objects.all())
    toBranchId = LegacyPrimaryKeyRelatedField(
        source='to_branch', queryset=Branch.objects.all(), required=False, allow_null=True)
    createdById = serializers.PrimaryKeyRelatedField(source='created_by', read_only=True)
    completedById = serializers.PrimaryKeyRelatedField(source='completed_by', read_only=True)
    status = serializers.CharField(read_only=True)
    source = serializers.CharField(read_only=True)
    items = B2BStockTransferItemSerializer(many=True)
    lineCount = serializers.SerializerMethodField()
    totalQuantity = serializers.SerializerMethodField()
    completedAt = serializers.DateTimeField(source='completed_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = B2BStockTransfer
        fields = (
            'id', 'fromShopId', 'toShopId', 'fromBranchId', 'toBranchId',
            'status', 'source', 'reference', 'note', 'createdById', 'completedById',
            'completedAt', 'items', 'lineCount', 'totalQuantity',
            'createdAt', 'updatedAt',
        )

    def get_lineCount(self, obj):
        return obj.items.count() if hasattr(obj, 'items') else 0

    def get_totalQuantity(self, obj):
        if hasattr(obj, 'items'):
            return sum(item.quantity for item in obj.items.all())
        return 0

    def validate(self, attrs):
        if self.instance is None:
            missing = {
                alias: ['This field is required.']
                for alias, source in (
                    ('fromShopId', 'from_shop'), ('toShopId', 'to_shop'),
                    ('fromBranchId', 'from_branch'),
                ) if source not in attrs
            }
            if missing:
                raise serializers.ValidationError(missing)
            if not attrs.get('items'):
                raise serializers.ValidationError({'items': ['Add at least one item.']})
        return attrs

    def create(self, validated_data):
        from apps.products.b2b_services import create_b2b_transfer

        item_rows = validated_data.pop('items')
        return create_b2b_transfer(
            validated_data['from_shop'],
            validated_data['to_shop'],
            validated_data['from_branch'],
            items=[
                {
                    'product': row['product'],
                    'quantity': row['quantity'],
                    'unit_cost': row.get('unit_cost'),
                }
                for row in item_rows
            ],
            created_by=self.context['request'].user,
            to_branch=validated_data.get('to_branch'),
            note=validated_data.get('note', ''),
            reference=validated_data.get('reference', ''),
        )
