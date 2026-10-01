from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.core.legacy import LegacyLookupMixin, filter_by_ref, resolve_legacy_pk
from apps.core.utils import first_param, drf_validation_error, resolve_shop
from apps.purchases.models import PurchaseOrder, PurchaseShipment, GRN
from apps.shops.models import Shop
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access
from apps.purchases.services import (
    create_purchase_order, process_grn, create_purchase_shipment,
    update_b2b_order_status, update_b2b_shipment_status,
)
from api.v1.serializers.purchases import (
    PurchaseOrderSerializer, CreatePurchaseOrderInputSerializer, UpdateOrderStatusInputSerializer,
    PurchaseShipmentSerializer, GRNSerializer, ProcessGRNInputSerializer,
    CreatePurchaseShipmentInputSerializer, UpdateShipmentStatusInputSerializer,
)


class PurchaseOrderViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = PurchaseOrder.objects.all().order_by('-created_at')
    serializer_class = PurchaseOrderSerializer
    # A shop sees a PO whether it is the buyer or the supplier.
    shop_paths = ('buyer_shop_id', 'supplier_shop_id')

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        role = first_param(self.request.query_params, 'role')
        if shop_id:
            if role == 'supplier':
                qs = filter_by_ref(qs, 'supplier_shop_id', shop_id, Shop)
            else:
                qs = filter_by_ref(qs, 'buyer_shop_id', shop_id, Shop)
        return qs.select_related('buyer_shop', 'supplier_shop').prefetch_related('items')

    def create(self, request, *args, **kwargs):
        serializer = CreatePurchaseOrderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        buyer_shop = resolve_shop(data.pop('buyer_shop_id'), 'buyerShopId')
        supplier_shop = resolve_shop(data.pop('supplier_shop_id'), 'supplierShopId')

        assert_shop_access(request.user, buyer_shop.id)

        po = create_purchase_order(
            buyer_shop=buyer_shop,
            supplier_shop=supplier_shop,
            items_data=data.pop('items'),
            **data
        )

        return Response(PurchaseOrderSerializer(po).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='update_status')
    def update_status(self, request, pk=None):
        po = self.get_object()
        serializer = UpdateOrderStatusInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        po = update_b2b_order_status(po.id, data['status'], data.get('notes'))
        return Response(PurchaseOrderSerializer(po).data)

    @action(detail=False, methods=['post'], url_path='process_grn')
    def process_grn(self, request):
        serializer = ProcessGRNInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        buyer_shop = resolve_shop(data['buyer_shop_id'], 'shopId')
        assert_shop_access(request.user, buyer_shop.id)

        po_pk = resolve_legacy_pk(PurchaseOrder, data['purchase_order_id'])
        if po_pk is None or not PurchaseOrder.objects.filter(pk=po_pk, buyer_shop=buyer_shop).exists():
            raise DRFValidationError({'poId': 'Purchase order not found.'})

        supplier_shop = None
        if data.get('supplier_shop_id'):
            supplier_shop = resolve_shop(data['supplier_shop_id'], 'supplierId')

        shipment_pk = None
        if data.get('shipment_id'):
            shipment_pk = resolve_legacy_pk(PurchaseShipment, data['shipment_id'])
            if shipment_pk is None or not PurchaseShipment.objects.filter(pk=shipment_pk, purchase_order_id=po_pk).exists():
                raise DRFValidationError({'shipmentId': 'Shipment not found.'})

        try:
            grn = process_grn(
                purchase_order_id=po_pk,
                buyer_shop=buyer_shop,
                supplier_shop=supplier_shop,
                items_data=data['items'],
                shipment_id=shipment_pk,
                notes=data.get('notes') or '',
                user=request.user
            )
        except DRFValidationError as exc:
            raise drf_validation_error(exc)

        return Response(GRNSerializer(grn).data, status=status.HTTP_201_CREATED)


class PurchaseShipmentViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = PurchaseShipment.objects.all().order_by('-created_at')
    serializer_class = PurchaseShipmentSerializer
    shop_paths = ('purchase_order__buyer_shop_id', 'purchase_order__supplier_shop_id')

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        po_id = first_param(self.request.query_params, 'po_id', 'poId', 'purchase_order_id', 'purchaseOrderId')
        if po_id:
            qs = filter_by_ref(qs, 'purchase_order_id', po_id, PurchaseOrder)
        return qs.select_related(
            'purchase_order', 'purchase_order__buyer_shop', 'purchase_order__supplier_shop'
        ).prefetch_related('items')

    def create(self, request, *args, **kwargs):
        serializer = CreatePurchaseShipmentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        supplier_ref = data.pop('supplier_shop_id', None) or self.request.query_params.get('shop_id')
        if not supplier_ref:
            raise DRFValidationError({'supplierShopId': 'This field is required.'})
        supplier_shop = resolve_shop(supplier_ref, 'supplierShopId')

        assert_shop_access(request.user, supplier_shop.id)

        po_pk = resolve_legacy_pk(PurchaseOrder, data.pop('purchase_order_id'))
        if po_pk is None or not PurchaseOrder.objects.filter(pk=po_pk, supplier_shop=supplier_shop).exists():
            raise DRFValidationError({'poId': 'Purchase order not found.'})

        shipment = create_purchase_shipment(
            po_id=po_pk,
            supplier_shop=supplier_shop,
            items_data=data.pop('items'),
            **data
        )

        return Response(PurchaseShipmentSerializer(shipment).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='update_status')
    def update_status(self, request, pk=None):
        shipment = self.get_object()
        serializer = UpdateShipmentStatusInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        shipment = update_b2b_shipment_status(shipment.id, data['status'], data.get('notes'))
        return Response(PurchaseShipmentSerializer(shipment).data)


class GRNViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = GRN.objects.all().order_by('-created_at')
    serializer_class = GRNSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(super().get_queryset())
        po_id = first_param(self.request.query_params, 'po_id', 'poId', 'purchase_order_id', 'purchaseOrderId')
        if po_id:
            qs = filter_by_ref(qs, 'purchase_order_id', po_id, PurchaseOrder)
        return qs.select_related('purchase_order', 'shipment', 'shop', 'supplier').prefetch_related('items')
