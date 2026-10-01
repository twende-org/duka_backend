from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.core.legacy import filter_by_ref, resolve_legacy_pk
from apps.core.utils import first_param, drf_validation_error, resolve_shop
from apps.purchases.models import (
    B2BConnection, B2BSupplierBalance, B2BSupplierInvoice, B2BSupplierPayment, PurchaseOrder,
)
from apps.purchases.services import (
    request_b2b_connection, approve_b2b_connection,
    create_b2b_supplier_invoice, update_b2b_supplier_invoice_status,
    process_b2b_supplier_payment,
)
from apps.shops.models import Shop
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access
from api.v1.serializers.b2b import (
    B2BConnectionSerializer, RequestConnectionInputSerializer, ApproveConnectionInputSerializer,
    B2BSupplierBalanceSerializer, B2BSupplierInvoiceSerializer, CreateSupplierInvoiceInputSerializer,
    UpdateSupplierInvoiceStatusInputSerializer, B2BSupplierPaymentSerializer,
    ProcessSupplierPaymentInputSerializer,
)


class B2BConnectionViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = B2BConnection.objects.all().order_by('-created_at')
    serializer_class = B2BConnectionSerializer
    shop_paths = ('buyer_shop_id', 'supplier_shop_id')

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        buyer_id = first_param(self.request.query_params, 'buyer_shop_id', 'buyerShopId')
        supplier_id = first_param(self.request.query_params, 'supplier_shop_id', 'supplierShopId')
        if buyer_id:
            qs = filter_by_ref(qs, 'buyer_shop_id', buyer_id, Shop)
        if supplier_id:
            qs = filter_by_ref(qs, 'supplier_shop_id', supplier_id, Shop)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = RequestConnectionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        buyer_shop = resolve_shop(data['buyer_shop_id'], 'buyerShopId')
        supplier_shop = resolve_shop(data['supplier_shop_id'], 'supplierShopId')
        assert_shop_access(request.user, buyer_shop.id)

        connection = request_b2b_connection(buyer_shop=buyer_shop, supplier_shop=supplier_shop)
        return Response(B2BConnectionSerializer(connection).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        connection = self.get_object()
        serializer = ApproveConnectionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        connection = approve_b2b_connection(connection, data.get('pricing_tier') or 'wholesale')
        return Response(B2BConnectionSerializer(connection).data)


class B2BSupplierBalanceViewSet(ShopScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = B2BSupplierBalance.objects.all().order_by('-updated_at')
    serializer_class = B2BSupplierBalanceSerializer
    shop_paths = ('buyer_shop_id', 'supplier_shop_id')

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        buyer_id = first_param(self.request.query_params, 'buyer_shop_id', 'buyerShopId')
        supplier_id = first_param(self.request.query_params, 'supplier_shop_id', 'supplierShopId')
        if buyer_id:
            qs = filter_by_ref(qs, 'buyer_shop_id', buyer_id, Shop)
        if supplier_id:
            qs = filter_by_ref(qs, 'supplier_shop_id', supplier_id, Shop)
        return qs


class B2BSupplierInvoiceViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = B2BSupplierInvoice.objects.all().order_by('-created_at')
    serializer_class = B2BSupplierInvoiceSerializer
    shop_paths = ('buyer_shop_id', 'supplier_shop_id')

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        buyer_id = first_param(self.request.query_params, 'buyer_shop_id', 'buyerShopId')
        supplier_id = first_param(self.request.query_params, 'supplier_shop_id', 'supplierShopId')
        status_value = self.request.query_params.get('status')
        if buyer_id:
            qs = filter_by_ref(qs, 'buyer_shop_id', buyer_id, Shop)
        if supplier_id:
            qs = filter_by_ref(qs, 'supplier_shop_id', supplier_id, Shop)
        if status_value:
            qs = qs.filter(status=status_value)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = CreateSupplierInvoiceInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        buyer_shop = resolve_shop(data.pop('buyer_shop_id'), 'buyerShopId')
        supplier_shop = resolve_shop(data.pop('supplier_shop_id'), 'supplierShopId')
        assert_shop_access(request.user, buyer_shop.id)

        purchase_order = None
        if data.get('purchase_order_id'):
            po_pk = resolve_legacy_pk(PurchaseOrder, data.pop('purchase_order_id'))
            purchase_order = PurchaseOrder.objects.filter(pk=po_pk, buyer_shop=buyer_shop).first() if po_pk else None
            if purchase_order is None:
                raise DRFValidationError({'purchaseOrderId': 'Purchase order not found.'})
        else:
            data.pop('purchase_order_id', None)

        # ``invoice_date``/``due_date`` arrive as the dialog's yyyy-mm-dd strings.
        for key in ('invoice_date', 'due_date'):
            value = data.get(key)
            data[key] = value or None

        invoice = create_b2b_supplier_invoice(
            buyer_shop=buyer_shop,
            supplier_shop=supplier_shop,
            purchase_order=purchase_order,
            **data
        )
        return Response(B2BSupplierInvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='update_status')
    def update_status(self, request, pk=None):
        invoice = self.get_object()
        serializer = UpdateSupplierInvoiceStatusInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        invoice = update_b2b_supplier_invoice_status(invoice, data['status'])
        return Response(B2BSupplierInvoiceSerializer(invoice).data)


class B2BSupplierPaymentViewSet(ShopScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = B2BSupplierPayment.objects.all().order_by('-date', '-created_at')
    serializer_class = B2BSupplierPaymentSerializer
    shop_paths = ('buyer_shop_id', 'supplier_shop_id')

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        buyer_id = first_param(self.request.query_params, 'buyer_shop_id', 'buyerShopId')
        supplier_id = first_param(self.request.query_params, 'supplier_shop_id', 'supplierShopId')
        if buyer_id:
            qs = filter_by_ref(qs, 'buyer_shop_id', buyer_id, Shop)
        if supplier_id:
            qs = filter_by_ref(qs, 'supplier_shop_id', supplier_id, Shop)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = ProcessSupplierPaymentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        buyer_shop = resolve_shop(data.pop('buyer_shop_id'), 'buyerShopId')
        supplier_shop = resolve_shop(data.pop('supplier_shop_id'), 'supplierShopId')
        assert_shop_access(request.user, buyer_shop.id)

        try:
            payment = process_b2b_supplier_payment(
                buyer_shop=buyer_shop,
                supplier_shop=supplier_shop,
                amount=data['amount'],
                method=data['method'],
                reference=data.get('reference') or '',
                invoice_ids=data.get('invoice_ids', []),
                date=data.get('date') or None,
                notes=data.get('notes') or '',
                allow_overpayment=data.get('allow_overpayment', False),
            )
        except DRFValidationError as exc:
            raise drf_validation_error(exc)

        return Response(B2BSupplierPaymentSerializer(payment).data, status=status.HTTP_201_CREATED)
