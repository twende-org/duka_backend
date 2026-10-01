from django.db.models import F
from django.http import Http404
from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

# pyrefly: ignore [missing-import]
from apps.core.utils import drf_validation_error, first_param
# pyrefly: ignore [missing-import]
from apps.products import selectors as product_selectors
# pyrefly: ignore [missing-import]
from apps.sales.services import create_storefront_order
# pyrefly: ignore [missing-import]
from apps.shops import selectors
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.shops.services import ensure_main_branch
from api.v1.serializers.public import (
    PublicOrderSerializer,
    PublicProductSerializer,
    PublicShopSerializer,
    PublicWishlistOrderInputSerializer,
)


def _optional_bool(value):
    """Read a query-param boolean; unknown values are ignored (None)."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ('true', '1', 'yes'):
        return True
    if text in ('false', '0', 'no'):
        return False
    return None


class PublicShopViewSet(viewsets.ReadOnlyModelViewSet):
    """Unauthenticated storefront reads, mirroring the Firestore public rules.

    ``authentication_classes`` is intentionally empty: a stale or malformed
    Bearer token must degrade to an anonymous read, not to a 401, or a public
    storefront page would break for signed-in visitors.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    serializer_class = PublicShopSerializer
    lookup_field = 'identifier'

    def get_queryset(self):
        params = self.request.query_params
        return selectors.get_public_shops(
            is_public=_optional_bool(first_param(params, 'is_public', 'isPublic')),
            is_wholesale_supplier=_optional_bool(
                first_param(params, 'is_wholesale_supplier', 'isWholesaleSupplier')),
            search=first_param(params, 'search'),
            slug=first_param(params, 'slug'),
        )

    def get_object(self):
        shop = selectors.get_shop_by_identifier(self.kwargs.get(self.lookup_field))
        if shop is None:
            raise Http404
        self.check_object_permissions(self.request, shop)
        return shop

    @action(detail=True, methods=['get'])
    def products(self, request, identifier=None):
        queryset = product_selectors.get_public_products(shop=self.get_object())
        page = self.paginate_queryset(queryset)
        serializer = PublicProductSerializer(page if page is not None else queryset, many=True)
        if page is None:
            return Response(serializer.data)
        return self.get_paginated_response(serializer.data)

    # Firestore let any visitor append to ``shops/{id}/orders`` — that write *was*
    # wishlist checkout — and the merchant confirmed on WhatsApp afterwards. This
    # action is the same bargain: anonymous shoppers may place an order, prices are
    # re-read from the product rows, and stock only moves once the merchant confirms.

    @action(detail=True, methods=['post'], url_path='orders',
            authentication_classes=[JWTAuthentication],
            permission_classes=[permissions.AllowAny])
    def orders(self, request, identifier=None):
        shop = self.get_object()
        serializer = PublicWishlistOrderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch = ensure_main_branch(shop)

        try:
            order = create_storefront_order(
                shop=shop,
                branch=branch,
                order_id=data['order_id'],
                items=data['items'],
                customer_name=data['customer_name'],
                customer_phone=data['customer_phone'],
                customer_address=data.get('customer_address'),
                notes=data.get('notes'),
                payment_method=data['payment_method'],
                customer_type=data['customer_type'],
                # A signed-in shopper's order must land in their portal history.
                customer_user=request.user if request.user.is_authenticated else None,
            )
        except ValidationError as error:
            raise drf_validation_error(error)

        return Response(PublicOrderSerializer(order).data, status=status.HTTP_201_CREATED)

    # Firestore let any signed-in user bump ``followerCount`` and nothing else
    # (affectedKeys().hasOnly(['followerCount'])); these two actions are the
    # Django equivalent, and are the only writes on this otherwise read-only view.

    @action(detail=True, methods=['post'], url_path='follow',
            authentication_classes=[JWTAuthentication],
            permission_classes=[permissions.IsAuthenticated])
    def follow(self, request, identifier=None):
        return self._adjust_followers(delta=1)

    @action(detail=True, methods=['post'], url_path='unfollow',
            authentication_classes=[JWTAuthentication],
            permission_classes=[permissions.IsAuthenticated])
    def unfollow(self, request, identifier=None):
        return self._adjust_followers(delta=-1)

    def _adjust_followers(self, *, delta):
        shop = self.get_object()
        queryset = Shop.objects.filter(pk=shop.pk)
        if delta < 0:
            queryset = queryset.filter(follower_count__gt=0)
        queryset.update(follower_count=F('follower_count') + delta)
        shop.refresh_from_db(fields=['follower_count'])
        return Response({'followerCount': shop.follower_count})
