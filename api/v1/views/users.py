"""Platform-user administration, subscriptions and the personal wishlist.

Staff-only: listing/searching users, the suspension flag, the system-admin
toggle, business-profile decisions, account deletion and subscription
administration. Personal: a caller reads their own subscription and manages
their own wishlist. User references in the URL may be a Django uuid or a
Firebase uid, both resolved through ``resolve_app_user``.
"""
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

# pyrefly: ignore [missing-import]
from apps.core.permissions import IsPlatformAdmin
# pyrefly: ignore [missing-import]
from apps.core.utils import first_param
# pyrefly: ignore [missing-import]
from apps.users.models import CustomerAddress, Subscription, User, WishlistItem
# pyrefly: ignore [missing-import]
from api.v1.serializers.users import (
    AddressSerializer, AdminUserSerializer, SubscriptionSerializer, WishlistItemSerializer,
    app_visible_user_id, resolve_app_user,
)


class StaffUserViewSet(viewsets.ModelViewSet):
    """Staff-only view of platform users (legacy ``users/{uid}`` documents).

    Writes are limited to the three admin toggles the panel exposes: the
    suspension flag, ``businessProfile`` (wholesale decisions) and
    ``corporateProfile`` (company membership); ``PATCH`` merges the given keys.
    ``?business_status=PENDING`` is the wholesale-applications queue filter
    (legacy ``where('businessProfile.status', '==', 'PENDING')``).
    """

    serializer_class = AdminUserSerializer
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get_queryset(self):
        queryset = User.objects.all().order_by('-date_joined')
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search) | Q(display_name__icontains=search)
                | Q(phone__icontains=search)
            )
        business_status = first_param(self.request.query_params, 'business_status', 'businessStatus')
        if business_status:
            queryset = queryset.filter(business_profile__status__iexact=business_status)
        return queryset

    def get_object(self):
        user = resolve_app_user(self.kwargs.get('pk'))
        if user is None:
            raise NotFound('User not found.')
        self.check_object_permissions(self.request, user)
        return user

    def create(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Use the registration endpoint to create users.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @action(detail=True, methods=['post'], url_path='system-admin')
    def system_admin(self, request, pk=None):
        """Grant (default) or revoke the platform-admin flag (``is_staff``)."""
        user = self.get_object()
        make_admin = request.data.get('isSystemAdmin', True)
        user.is_staff = bool(make_admin)
        user.save(update_fields=['is_staff'])
        return Response(self.get_serializer(user).data)

    def perform_destroy(self, instance):
        if instance.pk == self.request.user.pk:
            raise ValidationError({'detail': 'You cannot delete your own account.'})
        if instance.is_superuser:
            raise PermissionDenied('Superuser accounts cannot be deleted through the API.')
        # Cascades the user's roles, subscription and wishlist; shops and
        # transactions are kept (their user references are SET_NULL).
        instance.delete()


class SubscriptionViewSet(viewsets.ModelViewSet):
    """One subscription per user, keyed by the app-visible user id.

    ``GET /subscriptions/<user_id>/`` answers 200 with ``null`` when the user has
    no row (``useSubscription`` reads that as the free tier). ``PUT`` upserts —
    the legacy ``setDoc`` created the row when missing — and ``confirm`` is the
    staff activation that stamps who did it from the token.
    """

    serializer_class = SubscriptionSerializer
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsPlatformAdmin()]

    def get_queryset(self):
        queryset = Subscription.objects.select_related('user')
        user = self.request.user
        if not (user.is_staff or user.is_superuser):
            return queryset.filter(user=user)
        user_ref = first_param(self.request.query_params, 'user_id', 'userId')
        if user_ref:
            target = resolve_app_user(user_ref)
            return queryset.filter(user=target) if target is not None else queryset.none()
        return queryset

    def target_user(self):
        user = resolve_app_user(self.kwargs.get('pk'))
        if user is None:
            raise NotFound('User not found.')
        return user

    def get_instance(self, target):
        return self.filter_queryset(self.get_queryset()).filter(user=target).first()

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_instance(self.target_user())
        return Response(self.get_serializer(instance).data if instance else None)

    def upsert(self, request, partial):
        target = self.target_user()
        instance = self.get_instance(target)
        if instance is None:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            serializer.save(
                user=target,
                user_email=data.get('user_email') or target.email or '',
                user_name=data.get('user_name') or target.display_name or '',
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        return self.upsert(request, partial=False)

    def partial_update(self, request, *args, **kwargs):
        return self.upsert(request, partial=True)

    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        instance = self.get_instance(self.target_user())
        if instance is None:
            raise NotFound('No subscription for that user.')
        instance.status = 'active'
        instance.confirmed_by = app_visible_user_id(request.user)
        instance.confirmed_at = timezone.now()
        instance.save(update_fields=['status', 'confirmed_by', 'confirmed_at', 'updated_at'])
        return Response(self.get_serializer(instance).data)


class WishlistViewSet(viewsets.ModelViewSet):
    """The signed-in user's saved products, addressed by ``productId``.

    Guests keep using localStorage on the client; once signed in the same
    operations hit these routes. ``toggle`` answers ``{added: bool}`` because
    that is what the storefront button needs, and ``bulk-delete`` mirrors the
    multi-select on the wishlist page.
    """

    serializer_class = WishlistItemSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']
    lookup_field = 'product_id'

    def get_queryset(self):
        queryset = WishlistItem.objects.filter(user=self.request.user)
        product_id = first_param(self.request.query_params, 'product_id', 'productId')
        if product_id:
            queryset = queryset.filter(product_id=product_id)
        return queryset

    def create(self, request, *args, **kwargs):
        """Upsert by product id: re-adding refreshes the stored snapshot."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        instance = WishlistItem.objects.filter(
            user=request.user, product_id=data['product_id']
        ).first()
        if instance is None:
            serializer.save(user=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        for field, value in data.items():
            setattr(instance, field, value)
        instance.save()
        return Response(self.get_serializer(instance).data)

    @action(detail=False, methods=['post'])
    def toggle(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product_id = serializer.validated_data['product_id']
        instance = WishlistItem.objects.filter(
            user=request.user, product_id=product_id
        ).first()
        if instance is not None:
            instance.delete()
            return Response({'added': False})
        serializer.save(user=request.user)
        return Response({'added': True}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        product_ids = request.data.get('productIds') or request.data.get('product_ids')
        if not isinstance(product_ids, list):
            raise ValidationError({'productIds': 'A list of product ids is required.'})
        deleted, _ = WishlistItem.objects.filter(
            user=request.user, product_id__in=product_ids
        ).delete()
        return Response({'deleted': deleted})


class AddressViewSet(viewsets.ModelViewSet):
    """The signed-in user's saved delivery addresses.

    The portal page creates with the whole snapshot and deletes by id, so the
    default CRUD mapping is enough; rows are private to their owner.
    """

    serializer_class = AddressSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return CustomerAddress.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
