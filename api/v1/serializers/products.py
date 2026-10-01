from rest_framework import serializers
from apps.products.models import Product, Category, MerchantCategory, Inventory, InventoryMovement, StockTransfer
from apps.shops.models import Shop, Branch
from apps.core.legacy import LegacyPrimaryKeyRelatedField, app_id
from django.contrib.auth import get_user_model
from django.utils.text import slugify

User = get_user_model()


class BlankableDateField(serializers.DateField):
    """Accepts "" the way Firestore did: the product form posts an empty date."""

    def validate_empty_values(self, data):
        if data == '' and not self.read_only:
            return (True, None)
        return super().validate_empty_values(data)


class CategorySerializer(serializers.ModelSerializer):
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Category
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {'shop': {'required': False}}
        # ``shopId`` and the auto-generated ``shop`` field map to the same
        # source, which breaks DRF's unique-together validator; the (shop, name)
        # rule is enforced in validate() instead.
        validators = []

    def validate(self, attrs):
        if self.instance is None and 'shop' not in attrs:
            raise serializers.ValidationError({'shopId': 'This field is required.'})
        shop = attrs.get('shop', getattr(self.instance, 'shop', None))
        name = attrs.get('name', getattr(self.instance, 'name', None))
        if shop and name:
            clash = Category.objects.filter(shop=shop, name__iexact=name)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {'name': 'A category with this name already exists for this shop.'})
        return attrs


class MerchantCategorySerializer(serializers.ModelSerializer):
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all())
    parentId = LegacyPrimaryKeyRelatedField(
        source='parent', queryset=MerchantCategory.objects.all(), required=False, allow_null=True
    )
    sortOrder = serializers.IntegerField(source='sort_order', required=False)
    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = MerchantCategory
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {'shop': {'required': False}}
        # ``parentId``/``parent`` and ``shopId``/``shop`` each map to one model
        # field, which breaks DRF's constraint-derived UniqueTogetherValidator;
        # the (shop, parent, name) rule is enforced in validate() instead.
        validators = []

    def validate(self, attrs):
        if self.instance is None:
            if 'shop' not in attrs:
                raise serializers.ValidationError({'shopId': 'This field is required.'})
            if not attrs.get('slug'):
                name = attrs.get('name') or 'category'
                attrs['slug'] = slugify(name)[:255] or 'category'

        shop = attrs.get('shop', getattr(self.instance, 'shop', None))
        name = attrs.get('name', getattr(self.instance, 'name', None))
        parent = attrs.get('parent', getattr(self.instance, 'parent', None))
        if shop and name:
            clash = MerchantCategory.objects.filter(shop=shop, parent=parent, name__iexact=name)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {'name': 'A category with this name already exists at this level of the tree.'})

        # Max 3 levels; also rejects a parent that points back into this node's
        # own subtree (self-parenting and cycles) when moving a category.
        node, seen = parent, set()
        for _ in range(5):
            if node is None:
                break
            if self.instance is not None and node.pk == self.instance.pk:
                raise serializers.ValidationError(
                    {'parentId': 'A category cannot be nested inside itself or its own children.'})
            if node.pk in seen:
                break
            seen.add(node.pk)
            node = node.parent
        if len(seen) >= 3:
            raise serializers.ValidationError(
                {'parentId': 'Categories can only be nested 3 levels deep.'})
        return attrs


class ProductSerializer(serializers.ModelSerializer):
    # camelCase aliases accept the frontend payload and mirror it back in responses;
    # snake_case keys stay writable/readable for internal callers (lossless parity).
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    categoryId = LegacyPrimaryKeyRelatedField(source='category', queryset=Category.objects.all(), required=False, allow_null=True)
    branchId = LegacyPrimaryKeyRelatedField(source='branch', queryset=Branch.objects.all(), required=False, allow_null=True)
    # Read-only display name: the app shows product.category as text, while the
    # writable relation travels as categoryId.
    category = serializers.SerializerMethodField()
    categories = serializers.JSONField(source='marketplace_categories', required=False, allow_null=True)
    marketplaceCategoryId = serializers.CharField(source='marketplace_category_id', required=False, allow_blank=True, allow_null=True)
    merchantCategoryId = serializers.CharField(source='merchant_category_id', required=False, allow_blank=True, allow_null=True)
    buyingPrice = serializers.DecimalField(source='buying_price', max_digits=12, decimal_places=2, required=False)
    sellingPrice = serializers.DecimalField(source='selling_price', max_digits=12, decimal_places=2, required=False)
    wholesalePrice = serializers.DecimalField(source='wholesale_price', max_digits=12, decimal_places=2, required=False)
    taxRate = serializers.DecimalField(source='tax_rate', max_digits=5, decimal_places=2, required=False)
    imageUrl = serializers.CharField(source='image_url', required=False, allow_blank=True, allow_null=True)
    imageUrls = serializers.JSONField(source='image_urls', required=False, allow_null=True)
    expiryDate = BlankableDateField(source='expiry_date', required=False, allow_null=True)
    storeLocation = serializers.CharField(source='store_location', required=False, allow_blank=True, allow_null=True)
    reviewCount = serializers.IntegerField(source='review_count', required=False)
    publishToFacebook = serializers.BooleanField(source='publish_to_facebook', required=False)
    publishToDirectory = serializers.BooleanField(source='publish_to_directory', required=False)
    publishToDeliveryApp = serializers.BooleanField(source='publish_to_delivery_app', required=False)
    sourceProductId = serializers.CharField(source='source_product_id', required=False, allow_blank=True, allow_null=True)
    supplierShopId = serializers.CharField(source='supplier_shop_id', required=False, allow_blank=True, allow_null=True)
    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Product
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {'shop': {'required': False}}

    def get_category(self, obj):
        return obj.category.name if obj.category else None

    def validate(self, attrs):
        if self.instance is None and 'shop' not in attrs:
            raise serializers.ValidationError({'shopId': 'This field is required.'})
        return attrs


class ProductBulkImportSerializer(serializers.Serializer):
    """Excel-import payload: the browser parses the file and posts plain rows.

    Rows carry the export's column keys (``name``/``barcode``/``sku``/
    ``category``/``unit``/``buyingPrice``/``sellingPrice``/``quantity``);
    matching, field updates and the stock ledger happen in
    ``apps.products.services.bulk_import_products``.
    """

    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all())
    branchId = LegacyPrimaryKeyRelatedField(
        source='branch', queryset=Branch.objects.all(), required=False, allow_null=True
    )
    rows = serializers.ListField(
        child=serializers.DictField(), allow_empty=False, min_length=1, max_length=500
    )


class InventorySerializer(serializers.ModelSerializer):
    # camelCase aliases accept the frontend payload and mirror it back in responses;
    # snake_case keys stay writable/readable for internal callers (lossless parity).
    productId = LegacyPrimaryKeyRelatedField(source='product', queryset=Product.objects.all())
    branchId = LegacyPrimaryKeyRelatedField(source='branch', queryset=Branch.objects.all())
    # The app keys stock rows by shop + product; the row itself only links to a branch.
    shopId = serializers.SerializerMethodField()
    minStock = serializers.IntegerField(source='low_stock_threshold', required=False)
    allocatedQty = serializers.IntegerField(source='allocated_qty', required=False)
    lastUpdated = serializers.DateTimeField(source='updated_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Inventory
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        # ``productId`` shadows ``product`` (same source), which breaks DRF's
        # unique-together validator; the (product, branch) rule lives in the DB.
        validators = []

    def get_shopId(self, obj):
        return app_id(obj.branch.shop)


class InventoryMovementSerializer(serializers.ModelSerializer):
    productId = LegacyPrimaryKeyRelatedField(source='product', queryset=Product.objects.all())
    productName = serializers.CharField(source='product.name', read_only=True)
    branchId = LegacyPrimaryKeyRelatedField(source='branch', queryset=Branch.objects.all())
    shopId = serializers.SerializerMethodField()
    type = serializers.ChoiceField(source='movement_type', choices=InventoryMovement.MOVEMENT_TYPES, required=False)
    quantityChanged = serializers.IntegerField(source='quantity_changed', required=False)
    previousQty = serializers.IntegerField(source='previous_qty', required=False)
    newQty = serializers.IntegerField(source='new_qty', required=False)
    userId = serializers.PrimaryKeyRelatedField(source='user', read_only=True)
    userName = serializers.SerializerMethodField()
    # Firestore movements carried a `date`; the app still reads it for history rows.
    date = serializers.DateTimeField(source='created_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = InventoryMovement
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {'product': {'required': False}, 'branch': {'required': False}}

    def get_shopId(self, obj):
        return app_id(obj.branch.shop)

    def get_userName(self, obj):
        if obj.user:
            return obj.user.get_full_name() or obj.user.username
        return None


class InventoryAdjustmentSerializer(serializers.Serializer):
    """Accepts the app's camelCase adjust payload and the internal snake_case one.

    Ids may be Django UUIDs or Firestore legacy ids; ``branchId`` is optional so
    the app's product-only callers (initial stock, sold-out dialog) keep working.
    """

    product_id = serializers.CharField(required=False)
    productId = serializers.CharField(source='product_id', required=False)
    branch_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    branchId = serializers.CharField(source='branch_id', required=False, allow_blank=True, allow_null=True)
    movement_type = serializers.ChoiceField(
        choices=InventoryMovement.MOVEMENT_TYPES, default='adjustment', required=False
    )
    movementType = serializers.ChoiceField(
        source='movement_type', choices=InventoryMovement.MOVEMENT_TYPES, required=False
    )
    quantity = serializers.IntegerField()
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        # ``product_id``/``productId`` are two spellings of one required field.
        if not attrs.get('product_id'):
            raise serializers.ValidationError({'productId': 'This field is required.'})
        return attrs


class StockTransferSerializer(serializers.ModelSerializer):
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    fromBranchId = LegacyPrimaryKeyRelatedField(source='from_branch', queryset=Branch.objects.all(), required=False)
    toBranchId = LegacyPrimaryKeyRelatedField(source='to_branch', queryset=Branch.objects.all(), required=False)
    productId = LegacyPrimaryKeyRelatedField(source='product', queryset=Product.objects.all(), required=False)
    createdBy = serializers.PrimaryKeyRelatedField(
        source='created_by', queryset=User.objects.all(), required=False, allow_null=True
    )
    productName = serializers.CharField(source='product.name', read_only=True)
    createdByName = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    completedAt = serializers.DateTimeField(source='completed_at', read_only=True)

    class Meta:
        model = StockTransfer
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'completed_at')
        extra_kwargs = {
            'shop': {'required': False},
            'from_branch': {'required': False},
            'to_branch': {'required': False},
            'product': {'required': False},
        }

    def validate(self, attrs):
        if self.instance is None:
            pairs = {
                'shopId': 'shop', 'productId': 'product',
                'fromBranchId': 'from_branch', 'toBranchId': 'to_branch',
            }
            missing = {alias: ['This field is required.'] for alias, source in pairs.items() if source not in attrs}
            if missing:
                raise serializers.ValidationError(missing)
        return attrs

    def get_createdByName(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.username
        return None
