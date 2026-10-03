from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.core.exceptions import ValidationError
from django.db.models import F, Q

from apps.products.models import Product, Category, MerchantCategory, Inventory, InventoryMovement, StockTransfer
from api.v1.serializers.products import (
    ProductSerializer, CategorySerializer, MerchantCategorySerializer,
    InventorySerializer, InventoryMovementSerializer, StockTransferSerializer
)
from apps.products.services import adjust_inventory, bulk_import_products
from apps.shops.models import Branch, Shop
from apps.shops.services import ensure_main_branch
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access
from apps.core.legacy import LegacyLookupMixin, filter_by_ref, resolve_legacy_pk
from apps.core.utils import first_param

LOW_STOCK_TRUTHY = {'1', 'true', 'yes'}


class ProductViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = Product.objects.all().order_by('-created_at')
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]
    shop_paths = ('shop_id',)

    def get_queryset(self):
        queryset = self.scope_queryset(super().get_queryset())
        params = self.request.query_params

        shop_id = first_param(params, 'shop_id', 'shopId')
        category_id = first_param(params, 'category_id', 'categoryId')
        branch_id = first_param(params, 'branch_id', 'branchId')
        status_filter = params.get('status')
        search = params.get('search')

        if shop_id:
            queryset = filter_by_ref(queryset, 'shop_id', shop_id, Shop)
        if category_id:
            queryset = filter_by_ref(queryset, 'category_id', category_id, Category)
        if branch_id:
            queryset = filter_by_ref(queryset, 'branch_id', branch_id, Branch)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(sku__icontains=search)
                | Q(barcode__icontains=search) | Q(brand__icontains=search)
            )
        if (params.get('low_stock') or params.get('lowStock') or '').lower() in LOW_STOCK_TRUTHY:
            queryset = queryset.filter(
                inventory__quantity__lte=F('inventory__low_stock_threshold')
            ).distinct()

        return queryset

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['shop'].id)
        serializer.save()

    def perform_update(self, serializer):
        shop = serializer.validated_data.get('shop', serializer.instance.shop)
        assert_shop_access(self.request.user, shop.id)
        serializer.save()

    @action(detail=False, methods=['post'], url_path='bulk_import')
    def bulk_import(self, request):
        """
        Bulk product import (the Excel round-trip's write side).

        The browser parses the spreadsheet and posts the plain rows; each row
        is matched to an existing product (barcode, then case-insensitive
        name) and updated, or created when unknown. A row's ``quantity`` is
        the counted stock and lands through ``adjust_inventory``, so every
        change gets a movement-ledger record. Rows are independent: one bad
        row never rolls back the others.
        """
        from api.v1.serializers.products import ProductBulkImportSerializer
        from rest_framework.exceptions import ValidationError as DRFValidationError

        serializer = ProductBulkImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        shop = serializer.validated_data['shop']
        assert_shop_access(request.user, shop.id)

        try:
            summary, results = bulk_import_products(
                shop,
                serializer.validated_data['rows'],
                user=request.user,
                branch=serializer.validated_data.get('branch'),
            )
        except ValidationError as e:
            raise DRFValidationError({"detail": list(e.messages) if hasattr(e, 'messages') else str(e)})

        return Response({'summary': summary, 'results': results})


class CategoryViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = Category.objects.all().order_by('name')
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated]
    shop_paths = ('shop_id',)

    def get_queryset(self):
        queryset = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            queryset = filter_by_ref(queryset, 'shop_id', shop_id, Shop)
        return queryset

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['shop'].id)
        serializer.save()

    def perform_update(self, serializer):
        shop = serializer.validated_data.get('shop', serializer.instance.shop)
        assert_shop_access(self.request.user, shop.id)
        serializer.save()


class MerchantCategoryViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    """Merchant-defined categories from the setup wizard (Firestore subcollection parity)."""

    queryset = MerchantCategory.objects.all()
    serializer_class = MerchantCategorySerializer
    permission_classes = [IsAuthenticated]
    shop_paths = ('shop_id',)

    def get_queryset(self):
        queryset = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            queryset = filter_by_ref(queryset, 'shop_id', shop_id, Shop)
        return queryset

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['shop'].id)
        serializer.save()

    def perform_update(self, serializer):
        shop = serializer.validated_data.get('shop', serializer.instance.shop)
        assert_shop_access(self.request.user, shop.id)
        serializer.save()


class InventoryViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = Inventory.objects.all()
    serializer_class = InventorySerializer
    permission_classes = [IsAuthenticated]
    shop_paths = ('branch__shop_id',)

    def get_queryset(self):
        # Allow filtering by shop, branch or product (uuid or Firestore id)
        queryset = self.scope_queryset(super().get_queryset()).select_related('branch__shop', 'product')
        params = self.request.query_params
        shop_id = first_param(params, 'shop_id', 'shopId')
        branch_id = first_param(params, 'branch_id', 'branchId')
        product_id = first_param(params, 'product_id', 'productId')

        if shop_id:
            queryset = filter_by_ref(queryset, 'branch__shop_id', shop_id, Shop)
        if branch_id:
            queryset = filter_by_ref(queryset, 'branch_id', branch_id, Branch)
        if product_id:
            queryset = filter_by_ref(queryset, 'product_id', product_id, Product)

        return queryset

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['branch'].shop_id)
        serializer.save()

    def perform_update(self, serializer):
        branch = serializer.validated_data.get('branch', serializer.instance.branch)
        assert_shop_access(self.request.user, branch.shop_id)
        serializer.save()


class InventoryMovementViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = InventoryMovement.objects.all().order_by('-created_at')
    serializer_class = InventoryMovementSerializer
    permission_classes = [IsAuthenticated]
    shop_paths = ('branch__shop_id',)

    def get_queryset(self):
        queryset = self.scope_queryset(super().get_queryset()).select_related(
            'product', 'branch__shop', 'user'
        )
        params = self.request.query_params
        branch_id = first_param(params, 'branch_id', 'branchId')
        product_id = first_param(params, 'product_id', 'productId')

        if branch_id:
            queryset = filter_by_ref(queryset, 'branch_id', branch_id, Branch)
        if product_id:
            queryset = filter_by_ref(queryset, 'product_id', product_id, Product)

        return queryset

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['branch'].shop_id)
        serializer.save(user=self.request.user)

    @action(detail=False, methods=['post'])
    def adjust(self, request):
        """
        Custom endpoint to trigger the atomic inventory adjustment service.

        Accepts the app's camelCase payload (``productId`` + signed ``quantity``,
        ``branchId`` optional) and the internal snake_case one:
        { 'product_id': 'uuid', 'branch_id': 'uuid', 'movement_type': 'adjustment', 'quantity': 10, 'reason': 'Manual count' }
        """
        from api.v1.serializers.products import InventoryAdjustmentSerializer
        from rest_framework.exceptions import NotFound, ValidationError as DRFValidationError

        serializer = InventoryAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        product = Product.objects.filter(id=resolve_legacy_pk(Product, data['product_id'])).first()
        if product is None:
            raise NotFound({"detail": f"Product with ID {data['product_id']} not found."})

        branch_ref = data.get('branch_id')
        if branch_ref:
            branch = Branch.objects.filter(id=resolve_legacy_pk(Branch, branch_ref)).first()
            if branch is None:
                raise NotFound({"detail": f"Branch with ID {branch_ref} not found."})
        else:
            # The app's product-only callers omit the branch: stock the product's
            # own branch, else the shop's main (or first) branch — creating a
            # Main Branch for Firestore-imported shops that never had one, so
            # initial stock is not silently dropped.
            branch = (
                product.branch
                or Branch.objects.filter(shop_id=product.shop_id, is_main=True).first()
                or Branch.objects.filter(shop_id=product.shop_id).order_by('created_at').first()
                or ensure_main_branch(product.shop)
            )

        assert_shop_access(request.user, branch.shop_id)

        try:
            inventory, movement = adjust_inventory(
                product=product,
                branch=branch,
                movement_type=data.get('movement_type') or 'adjustment',
                quantity_change=data['quantity'],
                user=request.user,
                reason=data.get('reason', '')
            )

            return Response({
                'inventory': InventorySerializer(inventory).data,
                'movement': InventoryMovementSerializer(movement).data
            }, status=status.HTTP_201_CREATED)

        except ValidationError as e:
            # Map Django core ValidationError to DRF ValidationError
            raise DRFValidationError({"detail": list(e.messages) if hasattr(e, 'messages') else str(e)})

class StockTransferViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = StockTransfer.objects.all()
    serializer_class = StockTransferSerializer
    permission_classes = [IsAuthenticated]
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        return qs

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['shop'].id)
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        transfer = self.get_object()
        from apps.products.services import process_stock_transfer
        from django.core.exceptions import ValidationError
        
        try:
            transfer = process_stock_transfer(transfer, user=request.user)
            return Response(StockTransferSerializer(transfer).data)
        except ValidationError as e:
            from rest_framework.exceptions import ValidationError as DRFValidationError
            raise DRFValidationError({"detail": str(e)})

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        transfer = self.get_object()
        
        if transfer.status != 'pending':
            from rest_framework.exceptions import ValidationError as DRFValidationError
            raise DRFValidationError({"detail": f"Cannot cancel transfer {transfer.id} with status {transfer.status}."})
            
        transfer.status = 'cancelled'
        transfer.save(update_fields=['status', 'updated_at'])
        
        return Response(StockTransferSerializer(transfer).data)
