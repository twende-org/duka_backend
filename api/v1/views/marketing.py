from rest_framework import viewsets, permissions, filters
from rest_framework.decorators import action
from rest_framework.response import Response
# pyrefly: ignore [missing-import]
from apps.core.legacy import filter_by_ref
# pyrefly: ignore [missing-import]
from apps.core.utils import first_param
# pyrefly: ignore [missing-import]
from apps.marketing.models import DiscountCode, Campaign
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access
from api.v1.serializers.marketing import DiscountCodeSerializer, CampaignSerializer


class DiscountCodeViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    CRUD for Discount Codes (promo codes) scoped to a shop.
    Supports filtering by shop_id via query param: ?shop_id=<id>
    """
    serializer_class = DiscountCodeSerializer
    permission_classes = [permissions.IsAuthenticated]
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(DiscountCode.objects.all())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['shop'].id)
        serializer.save()

    @action(detail=True, methods=['post'])
    def increment_usage(self, request, pk=None):
        """Atomically increments the used_count when a code is redeemed."""
        discount = self.get_object()
        from django.db.models import F
        DiscountCode.objects.filter(pk=discount.pk).update(used_count=F('used_count') + 1)
        discount.refresh_from_db()
        return Response({'used_count': discount.used_count})


class CampaignViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    CRUD for Marketing Campaigns (broadcasts) scoped to a shop.
    Supports filtering by shop_id via query param: ?shop_id=<id>
    Supports filtering by source: ?source=ai_auto_pilot
    """
    serializer_class = CampaignSerializer
    permission_classes = [permissions.IsAuthenticated]
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(Campaign.objects.select_related('promo_code').all())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        source = self.request.query_params.get('source')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        if source:
            qs = qs.filter(source=source)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        assert_shop_access(self.request.user, serializer.validated_data['shop'].id)
        serializer.save()
