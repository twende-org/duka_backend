"""Customer-portal reads: the signed-in buyer's own orders and receipts.

Unlike every other list endpoint, these are **cross-shop**: the portal shows a
customer their purchases from all shops, so the caller's identity is the only
filter. Identity resolution lives in ``apps.sales.selectors``; these views only
wire it to JWT auth and the legacy payload shapes.
"""
from rest_framework import generics, permissions
from rest_framework_simplejwt.authentication import JWTAuthentication

# pyrefly: ignore [missing-import]
from apps.sales import selectors
from api.v1.serializers.portal import PortalOrderSerializer, PortalReceiptSerializer


class PortalBaseListView(generics.ListAPIView):
    """Base for the portal lists; JWT required, results belong to the caller."""

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return self.selector(self.request.user)


class PortalOrderListView(PortalBaseListView):
    """``getCustomerOrders(userId)``: orders the caller placed, newest first."""

    serializer_class = PortalOrderSerializer
    selector = staticmethod(selectors.get_portal_orders)


class PortalReceiptListView(PortalBaseListView):
    """``getCustomerReceipts(userId)``: completed sales as receipts, newest first."""

    serializer_class = PortalReceiptSerializer
    selector = staticmethod(selectors.get_portal_sales)
