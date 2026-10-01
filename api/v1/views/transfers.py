"""B2B wholesaler→retailer transfer endpoints (spec Part 2).

``/api/v1/transfers/`` carries the inter-shop lifecycle: a wholesaler dispatches
(stock leaves immediately), the retailer sees a pending card and either
completes it (stock lands in their shop) or lets the sender cancel it (stock
returns). The intra-shop branch-tobranch moves remain on
``/api/v1/stock-transfers/``.
"""
from django.core.exceptions import ValidationError
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from api.v1.serializers.transfers import (
    B2BStockTransferSerializer, B2BTransferMapSerializer,
)
from apps.core.legacy import LegacyLookupMixin, filter_by_ref
from apps.core.utils import first_param
from apps.products.b2b_services import (
    accept_b2b_transfer, cancel_b2b_transfer, complete_b2b_transfer,
    map_b2b_transfer_items,
)
from apps.products.models import B2BStockTransfer
from apps.shops.models import Shop
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access


class B2BStockTransferViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = B2BStockTransfer.objects.all()
    serializer_class = B2BStockTransferSerializer
    permission_classes = [IsAuthenticated]
    # Lifecycle moves through the complete/cancel actions, never a bare PATCH;
    # deletes would orphan ledger history, so they are not offered.
    http_method_names = ['get', 'post', 'head', 'options']
    shop_paths = ('from_shop_id', 'to_shop_id')

    def get_queryset(self):
        queryset = self.scope_queryset(
            super().get_queryset().select_related(
                'from_shop', 'to_shop', 'from_branch', 'to_branch',
                'created_by', 'completed_by').prefetch_related('items__transfer'))
        params = self.request.query_params
        shop_id = first_param(params, 'shop_id', 'shopId')
        direction = params.get('direction')
        if shop_id:
            if direction == 'incoming':
                queryset = filter_by_ref(queryset, 'to_shop_id', shop_id, Shop)
            elif direction == 'outgoing':
                queryset = filter_by_ref(queryset, 'from_shop_id', shop_id, Shop)
            else:
                queryset = queryset.filter(from_shop_id=shop_id) | queryset.filter(to_shop_id=shop_id)
        if params.get('status'):
            queryset = queryset.filter(status=params['status'])
        return queryset.distinct()

    def perform_create(self, serializer):
        # The sender dispatches; the receiving shop needs no say until complete.
        assert_shop_access(self.request.user, serializer.validated_data['from_shop'].id)
        try:
            serializer.save()
        except ValidationError as exc:
            raise DRFValidationError(
                exc.message_dict if hasattr(exc, 'message_dict') else str(exc))

    @action(detail=True, methods=['post'])
    def map(self, request, pk=None):
        """Buyer maps each line to one of their products before completing."""
        transfer = self.get_object()
        assert_shop_access(request.user, transfer.to_shop_id)
        payload = B2BTransferMapSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            transfer = map_b2b_transfer_items(
                transfer,
                mappings=payload.validated_data['items'],
                user=request.user,
            )
        except ValidationError as exc:
            raise DRFValidationError(
                exc.message_dict if hasattr(exc, 'message_dict') else str(exc))
        return Response(self.get_serializer(transfer).data)

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Buyer-side confirmation: quarantine -> live stock (row-locked)."""
        transfer = self.get_object()
        assert_shop_access(request.user, transfer.to_shop_id)
        try:
            transfer = complete_b2b_transfer(transfer, request.user)
        except ValidationError as exc:
            raise DRFValidationError(str(exc))
        return Response(self.get_serializer(transfer).data)

    @action(detail=True, methods=['post'])
    def accept(self, request, pk=None):
        """Buyer's one-tap accept for a sale-sourced delivery manifest.

        Auto-maps every line (barcode -> SKU -> name, creating missing buyer
        products) and moves the stock in one transaction — zero typing.
        """
        transfer = self.get_object()
        assert_shop_access(request.user, transfer.to_shop_id)
        try:
            transfer = accept_b2b_transfer(transfer, request.user)
        except ValidationError as exc:
            raise DRFValidationError(str(exc))
        return Response(self.get_serializer(transfer).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Rejection from either party: quarantined stock returns to sender.

        No per-side assert here on purpose — both the sender and the receiver
        may reject, and ``get_object`` already limits the row to shops the
        caller holds a role in.
        """
        transfer = self.get_object()
        try:
            transfer = cancel_b2b_transfer(transfer, request.user)
        except ValidationError as exc:
            raise DRFValidationError(str(exc))
        return Response(self.get_serializer(transfer).data)
