"""Telemetry endpoints: activity trail, storefront events, client error reports.

Writes are fire-and-forget from the app and must never fail the caller, so the
create actions accept whatever an old client sends (the serializers coerce).
Reads are narrower: the full streams are staff-only, the shop-scoped activity
read is for that shop's members, and the storefront rollups are computed here
instead of by aggregate queries the browser used to run against Firestore.
"""
from django.db.models import Count, Q
from django.utils import timezone
from datetime import timedelta
from rest_framework import viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

# pyrefly: ignore [missing-import]
from apps.core.legacy import resolve_legacy_pk
# pyrefly: ignore [missing-import]
from apps.core.permissions import IsPlatformAdmin
# pyrefly: ignore [missing-import]
from apps.core.utils import first_param
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.shops.permissions import assert_shop_access
# pyrefly: ignore [missing-import]
from apps.telemetry.models import ActivityLog, AnalyticsEvent, ErrorEvent
# pyrefly: ignore [missing-import]
from api.v1.serializers.telemetry import (
    ActivityLogSerializer, AnalyticsEventSerializer, ErrorEventSerializer,
)
# pyrefly: ignore [missing-import]
from api.v1.serializers.users import app_visible_user_id, resolve_app_user


def resolve_shop_filter(shop_ref):
    """(django_pk, Q) matching telemetry rows written under either id space.

    Shops are still Firestore-keyed in the app, so rows carry the Firestore id
    while newer ones may carry the Django pk; a single filter has to catch both.
    ``(None, None)`` means the reference matches no shop (callers return empty).
    """
    pk = resolve_legacy_pk(Shop, shop_ref)
    if pk is None:
        return None, None
    return str(pk), (Q(shop_id=str(shop_ref)) | Q(shop_id=str(pk)))


def resolve_user_ids(user_ref):
    """Every id a user may be stored under: the caller's value plus both spaces."""
    ids = {str(user_ref)}
    user = resolve_app_user(user_ref)
    if user is not None:
        ids.add(app_visible_user_id(user))
        ids.add(str(user.pk))
    return ids


def is_staff_user(user):
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


class ActivityLogViewSet(viewsets.ModelViewSet):
    """Audit trail of user actions (legacy ``activity_logs`` collection).

    ``POST`` is the app's fire-and-forget write and identity is taken from the
    token, never the body. ``GET ?shop_id=`` is the merchant dashboard read,
    scoped to that shop's members; ``GET`` without ``shop_id`` is the platform
    stream, which only staff may see.
    """

    queryset = ActivityLog.objects.all()
    serializer_class = ActivityLogSerializer
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        user = self.request.user
        params = self.request.query_params
        queryset = ActivityLog.objects.all()

        shop_ref = first_param(params, 'shop_id', 'shopId')
        if shop_ref:
            pk, condition = resolve_shop_filter(shop_ref)
            if condition is None:
                return queryset.none()
            assert_shop_access(user, pk)
            queryset = queryset.filter(condition)
        elif not is_staff_user(user):
            return queryset.none()

        user_ref = first_param(params, 'user_id', 'userId')
        if user_ref:
            queryset = queryset.filter(user_id__in=resolve_user_ids(user_ref))

        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(
            user_id=app_visible_user_id(user),
            user_email=user.email or '',
            user_name=user.display_name or '',
        )


class AnalyticsEventViewSet(viewsets.ModelViewSet):
    """Storefront funnel events (legacy ``analytics_events`` collection).

    The storefront is public, so guests write here without a token; the list is
    staff-only and exists for debugging the funnel.
    """

    queryset = AnalyticsEvent.objects.all()
    serializer_class = AnalyticsEventSerializer
    http_method_names = ['get', 'post', 'head', 'options']

    def get_permissions(self):
        if self.action == 'create':
            return [AllowAny()]
        return [IsAuthenticated(), IsPlatformAdmin()]

    def get_queryset(self):
        params = self.request.query_params
        queryset = AnalyticsEvent.objects.all()

        event_type = first_param(params, 'event_type', 'eventType')
        if event_type:
            queryset = queryset.filter(event_type=event_type)

        shop_ref = first_param(params, 'shop_id', 'shopId')
        if shop_ref:
            _, condition = resolve_shop_filter(shop_ref)
            if condition is None:
                return queryset.none()
            queryset = queryset.filter(condition)

        device_id = first_param(params, 'device_id', 'deviceId')
        if device_id:
            queryset = queryset.filter(device_id=device_id)

        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        if user and user.is_authenticated:
            serializer.save(user_id=app_visible_user_id(user))
        else:
            serializer.save()


class ErrorEventViewSet(viewsets.ModelViewSet):
    """Client-side error reports (legacy ``error_events`` collection).

    Crash reports come from contexts that may not be signed in (boundary,
    404 page, login), so creates are open; reads are staff-only.
    """

    queryset = ErrorEvent.objects.all()
    serializer_class = ErrorEventSerializer
    http_method_names = ['get', 'post', 'head', 'options']

    def get_permissions(self):
        if self.action == 'create':
            return [AllowAny()]
        return [IsAuthenticated(), IsPlatformAdmin()]

    def perform_create(self, serializer):
        user = self.request.user
        if user and user.is_authenticated:
            serializer.save(
                user_id=app_visible_user_id(user),
                user_email=user.email or '',
                user_name=user.display_name or '',
            )
        else:
            serializer.save()


def days_param(request, default=7):
    try:
        return max(1, int(request.query_params.get('days') or default))
    except (TypeError, ValueError):
        return default


class ShopAnalyticsSummaryView(APIView):
    """Storefront funnel for one shop over a window (legacy getShopAnalytics).

    ``GET /api/v1/analytics/shop-summary/?shop_id=<id>&days=7`` answers
    ``{visits, productViews, whatsappClicks, followers}``.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        shop_ref = first_param(request.query_params, 'shop_id', 'shopId')
        if not shop_ref:
            return Response({'error': 'shop_id is required'}, status=400)
        pk, condition = resolve_shop_filter(shop_ref)
        if condition is None:
            return Response({'error': 'Shop not found'}, status=404)
        assert_shop_access(request.user, pk)

        since = timezone.now() - timedelta(days=days_param(request))
        counts = {
            row['event_type']: row['n']
            for row in (
                AnalyticsEvent.objects
                .filter(condition, created_at__gte=since)
                .values('event_type')
                .annotate(n=Count('id'))
            )
        }
        return Response({
            'visits': counts.get('shop_visit', 0),
            'productViews': counts.get('product_view', 0),
            'whatsappClicks': counts.get('whatsapp_click', 0),
            'followers': counts.get('shop_follow', 0),
        })


class GlobalAnalyticsSummaryView(APIView):
    """Platform-wide storefront totals for the admin dashboard.

    ``GET /api/v1/analytics/global-summary/?days=7`` answers
    ``{totalSearches, totalWhatsAppClicks, totalShopVisits, topSearches}``. The
    legacy client fell back to mock numbers when the store was empty; a new
    install now honestly reports zeros (the dashboard renders "No searches yet").
    """

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        since = timezone.now() - timedelta(days=days_param(request))
        events = AnalyticsEvent.objects.filter(created_at__gte=since)

        top_searches = (
            events.filter(event_type='search_query')
            .exclude(query='')
            .values('query')
            .annotate(count=Count('id'))
            .order_by('-count', 'query')[:5]
        )
        return Response({
            'totalSearches': events.filter(event_type='search_query').count(),
            'totalWhatsAppClicks': events.filter(event_type='whatsapp_click').count(),
            'totalShopVisits': events.filter(event_type='shop_visit').count(),
            'topSearches': [{'query': row['query'], 'count': row['count']} for row in top_searches],
        })
