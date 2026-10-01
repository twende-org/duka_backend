from django.core.exceptions import ValidationError
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.sales.models import Shift
from apps.sales.services import open_shift, close_shift
from apps.shops.models import Shop
from apps.core.legacy import filter_by_ref
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access
from apps.core.utils import first_param, drf_validation_error
from api.v1.serializers.shifts import ShiftSerializer, CloseShiftInputSerializer


class ShiftViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    Cash-drawer shifts. Use ?shop_id=<id>&status=OPEN to fetch the active shift;
    closing goes through the dedicated `close` action so totals stay server-computed.
    """

    queryset = Shift.objects.all()
    serializer_class = ShiftSerializer
    permission_classes = [IsAuthenticated]
    shop_paths = ('shop_id',)

    def get_queryset(self):
        queryset = self.scope_queryset(super().get_queryset())
        params = self.request.query_params

        shop_id = first_param(params, 'shop_id', 'shopId')
        status_filter = params.get('status')

        if shop_id:
            queryset = filter_by_ref(queryset, 'shop_id', shop_id, Shop)
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        return queryset

    def perform_create(self, serializer):
        data = dict(serializer.validated_data)
        shop = data.pop('shop')
        assert_shop_access(self.request.user, shop.id)
        # Identity comes from the authenticated user, never the payload.
        data.pop('opened_by', None)
        data.pop('status', None)
        try:
            serializer.instance = open_shift(shop=shop, opened_by=self.request.user, **data)
        except ValidationError as e:
            raise drf_validation_error(e)

    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        shift = self.get_object()

        serializer = CloseShiftInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        if not data.get('closed_by_name'):
            data['closed_by_name'] = self._display_name(request.user)

        try:
            shift = close_shift(shift, closed_by=request.user, **data)
        except ValidationError as e:
            raise drf_validation_error(e)

        return Response(ShiftSerializer(shift).data)

    @staticmethod
    def _display_name(user):
        return getattr(user, 'display_name', '') or user.get_full_name() or user.username
