from django.db.models import Q, Sum
from django.core.exceptions import ValidationError
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.sales.models import Sale, DailySalesSummary, Order, Shift
from apps.shops.models import Shop, Branch
from apps.products.models import Product
from apps.core.legacy import (
    LegacyLookupMixin, filter_by_ref, is_uuid, resolve_legacy_pk,
)
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access
from apps.core.utils import drf_validation_error, first_param
from api.v1.serializers.sales import (
    SaleSerializer, POSSaleInputSerializer,
    OrderSerializer, CreateB2BOrderInputSerializer,
    UpdateFulfillmentStatusSerializer, PayOrderInputSerializer,
)
from apps.sales.services import (
    process_pos_sale, create_draft_sale, confirm_draft_sale,
    create_b2b_order, update_fulfillment_status, pay_order,
)
from apps.products.b2b_services import stage_sale_delivery


class SalesViewSet(ShopScopedQuerysetMixin, LegacyLookupMixin, viewsets.ModelViewSet):
    queryset = Sale.objects.all()
    serializer_class = SaleSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        qs = qs.select_related('shop', 'branch', 'attendant', 'shift').prefetch_related('items__product')
        params = self.request.query_params

        shop_id = first_param(params, 'shop_id', 'shopId')
        branch_id = first_param(params, 'branch_id', 'branchId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        if branch_id:
            qs = filter_by_ref(qs, 'branch_id', branch_id, Branch)

        status_param = params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        # Firestore keyed sales by a day document; here the day is derived from
        # ``createdAt`` (TIME_ZONE is UTC, exactly what the app's ISO date is).
        date_param = first_param(params, 'date')
        if date_param:
            qs = qs.filter(created_at__date=date_param)
        date_from = first_param(params, 'date_from', 'dateFrom')
        date_to = first_param(params, 'date_to', 'dateTo')
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        # Newest first — the order the day list and the range report read.
        return qs.order_by('-created_at')

    def create(self, request, *args, **kwargs):
        """
        Creates a POS sale (or a draft) using the atomic services.
        """
        input_serializer = POSSaleInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        data = input_serializer.validated_data

        # The POS knows its branch; blanks ("" when "all branches" is selected)
        # fall back to the shop's main branch.
        branch = None
        if data.get('branch_id'):
            branch = Branch.objects.filter(pk=resolve_legacy_pk(Branch, data['branch_id'])).first()
            if branch is None:
                raise DRFValidationError({'branchId': 'Branch not found.'})

        shop = branch.shop if branch else None
        if data.get('shop_id'):
            shop = Shop.objects.filter(pk=resolve_legacy_pk(Shop, data['shop_id'])).first()
            if shop is None:
                raise DRFValidationError({'shopId': 'Shop not found.'})
        if shop is None:
            raise DRFValidationError({'shopId': 'This field is required.'})

        assert_shop_access(request.user, shop.id)

        if branch is None:
            branch = shop.branches.filter(is_main=True).first() or shop.branches.first()
            if branch is None:
                raise DRFValidationError({'branchId': 'Shop has no branch to record the sale against.'})
        elif branch.shop_id != shop.id:
            raise DRFValidationError({'branchId': 'Branch does not belong to this shop.'})

        # Hydrate product objects; ids arrive legacy or uuid, and must be this shop's.
        hydrated_items = []
        for item in data['items']:
            value = str(item.get('product_id') or '')
            lookup = Q(id=value) if is_uuid(value) else Q(legacy_id=value)
            product = Product.objects.filter(lookup, shop=shop).first()
            if product is None:
                raise DRFValidationError({'items': f"Product {item['product_id']} not found."})
            hydrated_items.append({
                'product': product,
                'quantity': item['quantity'],
                'unit_price': item['unit_price'],
                'total_price': item.get('total_price'),
            })

        shift = None
        if data.get('shift_id'):
            value = str(data['shift_id'])
            # Shifts carry no Firestore id, so only our own uuid-keyed drawers link.
            shift = Shift.objects.filter(pk=value, shop=shop).first() if is_uuid(value) else None
            if shift is None:
                raise DRFValidationError({'shiftId': 'Shift not found.'})

        # A buyer business on the platform turns this sale into a delivery:
        # validated up front so a bad id never leaves a half-recorded sale.
        buyer_shop = None
        buyer_value = str(data.get('buyer_shop_id') or '')
        if buyer_value:
            buyer_lookup = Q(id=buyer_value) if is_uuid(buyer_value) else Q(legacy_id=buyer_value)
            buyer_shop = Shop.objects.filter(buyer_lookup).first()
            if buyer_shop is None:
                raise DRFValidationError({'buyerShopId': 'Buyer business not found.'})
            if buyer_shop.pk == shop.pk:
                raise DRFValidationError({'buyerShopId': 'The buyer must be a different business than the seller.'})

        service = create_draft_sale if data.get('status') == 'draft' else process_pos_sale

        try:
            sale = service(
                shop=shop,
                branch=branch,
                items_data=hydrated_items,
                payment_method=data['payment_method'],
                attendant=request.user,
                customer_id=data.get('customer_id'),
                customer_name=data.get('customer_name'),
                customer_phone=data.get('customer_phone'),
                notes=data.get('notes'),
                shift=shift,
            )
            if buyer_shop is not None and sale.status == 'completed':
                stage_sale_delivery(sale, buyer_shop, created_by=request.user)
            return Response(SaleSerializer(sale).data, status=status.HTTP_201_CREATED)

        except ValidationError as e:
            # Catch core django ValidationErrors (like Insufficient Stock) and return clean DRF error
            raise drf_validation_error(e)

    def perform_destroy(self, instance):
        # Legacy ``deleteDraftSale`` only ever removed drafts; a completed sale
        # already moved stock and the day summary, so deleting one would orphan them.
        if instance.status != 'draft':
            raise DRFValidationError({'detail': 'Only draft sales can be deleted.'})
        instance.delete()

    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        """Completes a saved draft, moving stock and the day summary the way ``confirmDraftSale`` did."""
        sale = self.get_object()
        try:
            sale = confirm_draft_sale(sale, attendant=request.user)
        except ValidationError as e:
            raise drf_validation_error(e)
        return Response(SaleSerializer(sale).data)

    @action(detail=False, methods=['get'])
    def summaries(self, request):
        """
        Per-day totals aggregated across the shop's branches — the shape
        Firestore's shop-level ``sales_days`` documents had.
        """
        params = request.query_params
        shop_id = first_param(params, 'shop_id', 'shopId')
        if not shop_id:
            raise DRFValidationError({'shopId': 'This field is required.'})

        qs = self.scope_queryset(DailySalesSummary.objects.all())
        qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)

        date_param = first_param(params, 'date')
        if date_param:
            qs = qs.filter(date=date_param)
        date_from = first_param(params, 'date_from', 'dateFrom', 'start_date', 'startDate')
        date_to = first_param(params, 'date_to', 'dateTo', 'end_date', 'endDate')
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)

        branch_id = first_param(params, 'branch_id', 'branchId')
        if branch_id:
            qs = filter_by_ref(qs, 'branch_id', branch_id, Branch)

        rows = (
            qs.values('date')
            .annotate(
                total_sales=Sum('total_sales'),
                transactions=Sum('transactions'),
                profit=Sum('profit'),
                net_profit=Sum('net_profit'),
                total_expenses=Sum('total_expenses'),
            )
            .order_by('date')
        )

        return Response([
            {
                'date': str(row['date']),
                'totalSales': row['total_sales'] or 0,
                'transactions': row['transactions'] or 0,
                'profit': row['profit'] or 0,
                'netProfit': row['net_profit'] or 0,
                'totalExpenses': row['total_expenses'] or 0,
            }
            for row in rows
        ])


class OrderViewSet(ShopScopedQuerysetMixin, LegacyLookupMixin, viewsets.ModelViewSet):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        qs = qs.select_related('shop', 'branch').prefetch_related('items')
        shop_id = self.request.query_params.get('shop_id')
        branch_id = self.request.query_params.get('branch_id')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        if branch_id:
            qs = filter_by_ref(qs, 'branch_id', branch_id, Branch)
        # The shell polls ``?status=pending&page_size=1`` for its badge count.
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def create(self, request, *args, **kwargs):
        """
        Creates a new B2B Order using the atomic service.
        """
        input_serializer = CreateB2BOrderInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        data = input_serializer.validated_data

        # The order wizard posts ``shopId`` with an often-blank ``branchId``, while
        # internal callers know only the branch; both spellings resolve here.
        branch = None
        if data.get('branch_id'):
            branch = Branch.objects.filter(pk=resolve_legacy_pk(Branch, data['branch_id'])).first()
            if branch is None:
                raise DRFValidationError({'branchId': 'Branch not found.'})

        shop = branch.shop if branch else None
        if data.get('shop_id'):
            shop = Shop.objects.filter(pk=resolve_legacy_pk(Shop, data['shop_id'])).first()
            if shop is None:
                raise DRFValidationError({'shopId': 'Shop not found.'})

        assert_shop_access(request.user, shop.id)

        if branch is None:
            # Firestore orders could carry a blank branch; Django inventory needs a
            # real one, so fall back to the shop's main branch.
            branch = shop.branches.filter(is_main=True).first() or shop.branches.first()
            if branch is None:
                raise DRFValidationError({'branchId': 'Shop has no branch to record the order against.'})
        elif branch.shop_id != shop.id:
            raise DRFValidationError({'branchId': 'Branch does not belong to this shop.'})

        try:
            order = create_b2b_order(
                shop=shop,
                branch=branch,
                items_data=data['items'],
                idempotency_key=data['idempotency_key'],
                payment_method=data['payment_method'],
                customer_id=data.get('customer_id'),
                customer_name=data.get('customer_name'),
                customer_phone=data.get('customer_phone'),
                customer_type=data.get('customer_type'),
                customer_po_number=data.get('customer_po_number'),
                required_delivery_date=data.get('required_delivery_date'),
                # Legacy stamped ``createdBy: userId`` on every order.
                salesperson_id=data.get('salesperson_id') or str(request.user.pk),
                internal_notes=data.get('internal_notes'),
                notes=data.get('notes'),
                fulfillment_details=data.get('fulfillment_details'),
                approval_status=data.get('approval_status'),
                source=data.get('source', 'in_app')
            )
            
            # The callable-shaped ``orderId`` sits alongside the serialized row:
            # ``useCreateOrder`` reads it, existing callers read ``id``.
            body = OrderSerializer(order).data
            body['success'] = True
            body['orderId'] = str(order.pk)
            return Response(body, status=status.HTTP_201_CREATED)
            
        except ValidationError as e:
            # Catch core django ValidationErrors (like Insufficient Stock) and return clean DRF error
            raise drf_validation_error(e)

    @action(detail=True, methods=['patch'])
    def update_status(self, request, pk=None):
        order = self.get_object()
        
        serializer = UpdateFulfillmentStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            order = update_fulfillment_status(
                order=order,
                status=serializer.validated_data['status'],
                fulfillment_data=serializer.validated_data.get('fulfillment_data'),
                items=serializer.validated_data.get('items')
            )
            return Response(OrderSerializer(order).data)
        except ValidationError as e:
            raise drf_validation_error(e)

    @action(detail=True, methods=['post'])
    def pay(self, request, pk=None):
        order = self.get_object()

        serializer = PayOrderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        shift = None
        if data.get('shift_id'):
            # Shifts carry no Firestore id of their own, so only our own
            # uuid-keyed drawer rows can be moved by a payment.
            value = str(data['shift_id'])
            shift = Shift.objects.filter(pk=value, shop=order.shop).first() if is_uuid(value) else None
            if shift is None:
                raise DRFValidationError({'shiftId': 'Shift not found.'})

        try:
            order = pay_order(
                order=order,
                payment_method=data['payment_method'],
                shift=shift,
            )
            return Response(OrderSerializer(order).data)
        except ValidationError as e:
            raise drf_validation_error(e)
