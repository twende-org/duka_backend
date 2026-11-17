from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.core.exceptions import ValidationError
from django.db.models import F, Q

from apps.products.models import (
    Product, Category, MerchantCategory, Inventory, InventoryMovement, StockTransfer,
    MasterProduct, InventorySerialItem,
)
from api.v1.serializers.products import (
    ProductSerializer, CategorySerializer, MerchantCategorySerializer,
    InventorySerializer, InventoryMovementSerializer, StockTransferSerializer,
    MasterProductSerializer, TapToAddSerializer, SerialItemSerializer,
    SerialBulkAddSerializer,
)
from apps.products.services import (
    adjust_inventory, bulk_import_products, bulk_tap_to_add_products,
    register_serial_items,
)
from apps.products.selectors import search_master_products
from apps.shops.models import Branch, Shop
from apps.shops.services import ensure_main_branch
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access, MANAGEMENT_ROLES
from apps.core.legacy import LegacyLookupMixin, filter_by_ref, resolve_legacy_pk
from apps.core.pagination import StandardPagination
from apps.core.utils import first_param

LOW_STOCK_TRUTHY = {'1', 'true', 'yes'}
CATALOG_PAGE_SIZE = 24


class ProductViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = Product.objects.all().order_by('-created_at')
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]
    shop_paths = ('shop_id',)

    def get_queryset(self):
        # Soft-deleted products (``is_active=False``) leave every merchant and
        # storefront read: the row stays for receipts, sales history and the IMEI
        # register, which is the whole point of not deleting it.
        queryset = self.scope_queryset(super().get_queryset()).filter(is_active=True)
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
        # ``is_active`` is the soft-delete switch, so turning it off is exactly as
        # much a management act as deleting the row — and the app PUTs whole
        # product objects back, so only a real change of value counts.
        if 'is_active' in serializer.validated_data and (
            serializer.validated_data['is_active'] != serializer.instance.is_active
        ):
            assert_shop_access(self.request.user, shop.id, roles=MANAGEMENT_ROLES)
        serializer.save()

    def perform_destroy(self, instance):
        """Soft-delete: take the product off the shelves, keep the audit trail.

        A phone shop's product row is the anchor for every IMEI ever sold under
        it, and ``InventorySerialItem.product`` is ``PROTECT`` precisely so a
        delete cannot erase that history. So the API retires the row instead:
        ``is_active=False`` hides it from the merchant list, the storefront and
        search, while receipts, the movement ledger and the unit register keep
        their link. Django admin can still restore it by ticking the flag back.

        The publish toggles go with it, mirroring the Firestore delete that made
        the document vanish from every channel at once: the delivery-app mirror
        drops through the existing true -> false signal, and nothing auto-posts
        or re-lists a product the merchant removed. A restore re-publishes
        deliberately rather than resurrecting listings the merchant thought gone.

        Owner and manager only — an attendant can still sell the product, they
        just cannot make it disappear for everyone.
        """
        assert_shop_access(self.request.user, instance.shop_id, roles=MANAGEMENT_ROLES)
        instance.is_active = False
        instance.publish_to_directory = False
        instance.publish_to_facebook = False
        instance.publish_to_delivery_app = False
        instance.save(update_fields=[
            'is_active', 'publish_to_directory', 'publish_to_facebook',
            'publish_to_delivery_app', 'updated_at',
        ])

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

    @action(detail=False, methods=['post'], url_path='tap-to-add')
    def tap_to_add(self, request):
        """Add shared-catalogue products to a shop in one tap.

        The picker sends the catalogue ids it found (uuid, or a Firestore-era id
        for rows the seeder never touched) plus this shop's own prices and, for
        a phone shop, the IMEI numbers scanned off the boxes. Everything that
        decides *what* lands — matching against what the shop already carries,
        category resolution, the stock ledger, the IMEI checks — is in
        ``bulk_tap_to_add_products``; one failing line rolls the whole batch
        back, so a merchant never ends up with half a catalogue.
        """
        from rest_framework.exceptions import ValidationError as DRFValidationError

        serializer = TapToAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        shop = serializer.validated_data['shop']
        assert_shop_access(request.user, shop.id)

        try:
            products = bulk_tap_to_add_products(
                shop=shop,
                user=request.user,
                branch=serializer.validated_data.get('branch'),
                items_payload=serializer.validated_data['items'],
            )
        except ValidationError as e:
            raise DRFValidationError(
                {"detail": list(e.messages) if hasattr(e, 'messages') else str(e)})

        return Response(
            {'products': ProductSerializer(products, many=True).data},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['get', 'post'], url_path='serials')
    def serials(self, request, pk=None):
        """The individual devices sitting on one product's shelf.

        ``GET`` lists them — all statuses by default, ``?unitStatus=in_stock``
        to narrow — and ``POST`` registers scanned units, raising stock by the
        same count unless ``addToStock`` is false. The unit filter is spelled
        ``unitStatus``, not ``status``: this route sits under the product
        resource, whose own ``?status=`` already filters the *product* row
        (``get_queryset`` above), so reusing the name would 404 every call.
        The product comes from the already shop-scoped queryset, so another
        shop's serial never appears; the membership check is the same one the
        write paths use.
        """
        from rest_framework.exceptions import ValidationError as DRFValidationError

        product = self.get_object()
        assert_shop_access(request.user, product.shop_id)

        if request.method == 'GET':
            queryset = InventorySerialItem.objects.filter(product=product).order_by('-created_at')
            status_filter = first_param(request.query_params, 'unit_status', 'unitStatus')
            if status_filter:
                if status_filter not in dict(InventorySerialItem.STATUS_CHOICES):
                    raise DRFValidationError({'unitStatus': f'Unknown status "{status_filter}".'})
                queryset = queryset.filter(status=status_filter)
            page = self.paginate_queryset(queryset)
            data = SerialItemSerializer(
                page if page is not None else queryset, many=True
            ).data
            if page is not None:
                return self.get_paginated_response(data)
            return Response(data)

        serializer = SerialBulkAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            created = register_serial_items(
                shop=product.shop,
                product=product,
                branch=serializer.validated_data.get('branch'),
                user=request.user,
                serial_list=serializer.validated_data['items'],
                add_to_stock=serializer.validated_data.get('add_to_stock', True),
            )
        except ValidationError as e:
            raise DRFValidationError(
                {"detail": list(e.messages) if hasattr(e, 'messages') else str(e)})

        return Response(
            SerialItemSerializer(created, many=True).data,
            status=status.HTTP_201_CREATED,
        )


class MasterProductSearchView(APIView):
    """Shared-catalogue browse/search behind the tap-to-add picker.

    ``GET /api/v1/master-products/?q=&niche=&niches=&category=&brand=&shopId=&page=``
    answers DRF page-number pagination of ``MasterProductSerializer`` rows,
    ranked the way the marketplace search ranks products so an exact barcode
    lookup lands first and an empty ``q`` is plain browse order (name).

    Deliberately *not* built on ``ShopScopedQuerysetMixin``: a catalogue row
    belongs to no shop, so its ``shop_id__in`` filter would return nothing. The
    only gate is ``is_active`` — this table is platform data every shop reads.

    ``shopId`` does not filter the results either; it annotates each row with
    ``alreadyAdded`` so the picker can show what the merchant already carries.
    That annotation reveals one shop's shelf, so the id is access-checked like
    any other shop-scoped read.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        params = request.query_params

        shop = None
        shop_ref = first_param(params, 'shop_id', 'shopId')
        if shop_ref:
            pk = resolve_legacy_pk(Shop, shop_ref)
            shop = Shop.objects.filter(pk=pk).first() if pk else None
            if shop is not None:
                assert_shop_access(request.user, shop.id)

        # `niches` scopes the picker to the merchant's lanes; both repeated
        # params and one comma-separated param are accepted.
        niches = []
        for chunk in params.getlist('niches'):
            niches.extend(part.strip() for part in chunk.split(',') if part.strip())

        results = search_master_products(
            query=first_param(params, 'q', 'search') or '',
            niche=(first_param(params, 'niche') or '').strip() or None,
            niches=niches or None,
            category_name=(first_param(params, 'category', 'category_name', 'categoryName') or '').strip() or None,
            brand=(first_param(params, 'brand') or '').strip() or None,
            shop=shop,
        )

        # The selector returns a ranked list once a query carries signal, and a
        # queryset for a plain browse; StandardPagination accepts both.
        paginator = StandardPagination()
        paginator.page_size = CATALOG_PAGE_SIZE
        page = paginator.paginate_queryset(results, request, view=self)
        serializer = MasterProductSerializer(page if page is not None else results, many=True)
        return paginator.get_paginated_response(serializer.data)


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
        # ``product__is_active``: a soft-deleted product's stock rows used to
        # vanish with its CASCADE, so the shelf list hides them the same way.
        queryset = (
            self.scope_queryset(super().get_queryset())
            .filter(product__is_active=True)
            .select_related('branch__shop', 'product')
        )
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

        product = Product.objects.filter(
            id=resolve_legacy_pk(Product, data['product_id']), is_active=True
        ).first()
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
