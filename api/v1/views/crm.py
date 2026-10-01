from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from apps.core.legacy import LegacyLookupMixin, filter_by_ref, resolve_legacy_pk
# pyrefly: ignore [missing-import]
from apps.core.utils import first_param, drf_validation_error
from apps.crm.models import (
    Customer, CustomerInvoice, CustomerPayment, Supplier, SupplierInvoice, SupplierPayment,
)
from apps.shops.models import Shop
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access
from api.v1.serializers.crm import (
    CustomerSerializer, CustomerInvoiceSerializer, CustomerPaymentSerializer,
    CreateCustomerInputSerializer, UpdateCustomerInputSerializer, ProcessCustomerPaymentInputSerializer,
    SupplierSerializer, CreateSupplierInputSerializer, UpdateSupplierInputSerializer,
    SupplierInvoiceSerializer, SupplierPaymentSerializer, ProcessSupplierPaymentInputSerializer,
)
from apps.crm.services import (
    create_customer, update_customer, process_customer_payment,
    create_supplier, update_supplier, process_supplier_payment,
)

class CustomerViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = CreateCustomerInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        pk = resolve_legacy_pk(Shop, data.pop('shop_id'))
        shop = Shop.objects.filter(pk=pk).first() if pk else None
        if shop is None:
            raise DRFValidationError({'shopId': 'Shop not found.'})

        assert_shop_access(request.user, shop.id)

        customer = create_customer(shop=shop, **data)
        return Response(CustomerSerializer(customer).data, status=status.HTTP_201_CREATED)
        
    def update(self, request, *args, **kwargs):
        customer = self.get_object()
        serializer = UpdateCustomerInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        
        customer = update_customer(customer, **data)
        return Response(CustomerSerializer(customer).data)
        
    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        customer = self.get_object()
        serializer = ProcessCustomerPaymentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        
        try:
            payment = process_customer_payment(
                shop=customer.shop,
                customer=customer,
                amount=data['amount'],
                method=data['method'],
                reference=data.get('reference', ''),
                notes=data.get('notes', ''),
                invoice_ids=data.get('invoice_ids', [])
            )
        except DRFValidationError as exc:
            raise drf_validation_error(exc)
        return Response(CustomerPaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

class CustomerInvoiceViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = CustomerInvoice.objects.all()
    serializer_class = CustomerInvoiceSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        customer_id = first_param(self.request.query_params, 'customer_id', 'customerId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        if customer_id:
            qs = filter_by_ref(qs, 'customer_id', customer_id, Customer)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['shop'].id)
        serializer.save()

class CustomerPaymentViewSet(ShopScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = CustomerPayment.objects.all()
    serializer_class = CustomerPaymentSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        customer_id = first_param(self.request.query_params, 'customer_id', 'customerId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        if customer_id:
            qs = filter_by_ref(qs, 'customer_id', customer_id, Customer)
        return qs.order_by('-created_at')

class SupplierViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = CreateSupplierInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        pk = resolve_legacy_pk(Shop, data.pop('shop_id'))
        shop = Shop.objects.filter(pk=pk).first() if pk else None
        if shop is None:
            raise DRFValidationError({'shopId': 'Shop not found.'})

        assert_shop_access(request.user, shop.id)

        supplier = create_supplier(shop=shop, **data)
        return Response(SupplierSerializer(supplier).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        supplier = self.get_object()
        serializer = UpdateSupplierInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        supplier = update_supplier(supplier, **data)
        return Response(SupplierSerializer(supplier).data)

    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        supplier = self.get_object()
        serializer = ProcessSupplierPaymentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            payment = process_supplier_payment(
                shop=supplier.shop,
                supplier=supplier,
                amount=data['amount'],
                method=data['method'],
                reference=data.get('reference', ''),
                notes=data.get('notes', ''),
                invoice_ids=data.get('invoice_ids', [])
            )
        except DRFValidationError as exc:
            raise drf_validation_error(exc)
        return Response(SupplierPaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

class SupplierInvoiceViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = SupplierInvoice.objects.all()
    serializer_class = SupplierInvoiceSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        supplier_id = first_param(self.request.query_params, 'supplier_id', 'supplierId')
        status_value = self.request.query_params.get('status')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        if supplier_id:
            qs = filter_by_ref(qs, 'supplier_id', supplier_id, Supplier)
        if status_value:
            qs = qs.filter(status=status_value)
        return qs

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['shop'].id)
        serializer.save()

class SupplierPaymentViewSet(ShopScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = SupplierPayment.objects.all()
    serializer_class = SupplierPaymentSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        supplier_id = first_param(self.request.query_params, 'supplier_id', 'supplierId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        if supplier_id:
            qs = filter_by_ref(qs, 'supplier_id', supplier_id, Supplier)
        return qs
