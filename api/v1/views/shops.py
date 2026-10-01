from django.contrib.auth import get_user_model
from django.db.models import OuterRef, Subquery, Sum
from django.http import Http404
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError as DRFValidationError
from apps.shops import services, selectors
# pyrefly: ignore [missing-import]
from apps.core.utils import first_param, drf_validation_error
# pyrefly: ignore [missing-import]
from apps.core.legacy import LegacyLookupMixin, filter_by_ref, is_uuid, resolve_legacy_pk
# pyrefly: ignore [missing-import]
from apps.sales.models import DailySalesSummary
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch, UserRole, Invitation
from apps.shops.permissions import assert_shop_access, MANAGEMENT_ROLES
from api.v1.serializers.shops import ShopSerializer, BranchSerializer, UserRoleSerializer, InvitationSerializer

User = get_user_model()


def _resolve_user(value):
    """Resolve a member reference (app-visible id or email) to a User.

    Users carry no Firestore legacy id — the app already addresses them by their
    Django uuid — so a non-uuid reference is matched by email, then username.
    """
    if not value:
        return None
    text = str(value).strip()
    if is_uuid(text):
        return User.objects.filter(pk=text).first()
    return (
        User.objects.filter(email__iexact=text).first()
        or User.objects.filter(username=text).first()
    )


class ShopViewSet(LegacyLookupMixin, viewsets.ModelViewSet):
    """
    ViewSet for viewing and creating shops.
    Delegates business logic to shops.services and reads to shops.selectors.
    """
    serializer_class = ShopSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # ``salesTotal`` replaces the counter the Firestore shop docs carried for
        # the admin list. A correlated subquery keeps the merchant list path free
        # of the join/group-by that summing the summary rows shop-wide would need.
        sales_totals = (
            DailySalesSummary.objects.filter(shop=OuterRef('pk'))
            .values('shop')
            .annotate(total=Sum('total_sales'))
            .values('total')[:1]
        )
        return (
            selectors.get_user_shops(user=self.request.user)
            .prefetch_related('products')
            .annotate(sales_total=Subquery(sales_totals))
        )

    def perform_create(self, serializer):
        # We don't call serializer.save() because we want to use our service
        # instead of the standard model create.
        shop = services.create_shop(
            user=self.request.user,
            **serializer.validated_data
        )
        serializer.instance = shop

    def get_serializer(self, *args, **kwargs):
        # Admin approval (``isPublic``) is the platform's final publish gate:
        # a merchant cannot list their own shop on the marketplace — only the
        # platform admin's Approve/Revoke toggle flips it.
        data = kwargs.get('data')
        if data is not None and not (self.request.user.is_staff or self.request.user.is_superuser):
            data = data.copy()
            data.pop('isPublic', None)
            data.pop('is_public', None)
            kwargs['data'] = data
        return super().get_serializer(*args, **kwargs)

    @action(detail=True, methods=['get'])
    def analytics(self, request, pk=None):
        # Return mocked analytics for now
        return Response({
            "storeViews": 0,
            "productClicks": 0
        })

    # Named update_settings (not `settings`) because an attribute named `settings`
    # shadows DRF's APIView.settings and breaks every request to this ViewSet.
    @action(detail=True, methods=['get', 'patch', 'put'], url_path='settings')
    def update_settings(self, request, pk=None):
        from apps.shops.serializers import ShopSettingsSerializer
        shop = self.get_object()
        if request.method == 'GET':
            return Response(ShopSettingsSerializer(shop).data)
        # Payout details and store policies live here, so the same owner/manager
        # restriction as the staff-invite path applies.
        assert_shop_access(request.user, shop.id, roles=MANAGEMENT_ROLES)
        serializer = ShopSettingsSerializer(shop, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class BranchViewSet(LegacyLookupMixin, viewsets.ModelViewSet):
    serializer_class = BranchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # A simple query can stay here, or we can add to selectors
        qs = Branch.objects.filter(shop__user_roles__user=self.request.user).distinct()
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        return qs

    def perform_create(self, serializer):
        shop = serializer.validated_data.get('shop')
        if shop is None:
            raw = first_param(self.request.data, 'shopId', 'shop_id', 'shop')
            pk = resolve_legacy_pk(Shop, raw)
            shop = Shop.objects.get(pk=pk) if pk else None
        if shop is None:
            raise DRFValidationError({'shopId': 'A valid shop is required.'})
        assert_shop_access(self.request.user, shop.id)
        serializer.save(shop=shop)

class UserRoleViewSet(LegacyLookupMixin, viewsets.ModelViewSet):
    serializer_class = UserRoleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # ?mine=true lists the caller's own roles; used to restore the session
        # after a reload without knowing which shops they belong to yet.
        if self.request.query_params.get('mine') in ('true', '1', 'True'):
            qs = UserRole.objects.filter(user=self.request.user)
        else:
            qs = UserRole.objects.filter(shop__user_roles__user=self.request.user).distinct()
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        # ?userId= pinpoints one member's role in a shop; the staff page uses it
        # to turn a (userId, shopId) pair back into the role row it must delete.
        user_ref = first_param(self.request.query_params, 'user_id', 'userId')
        if user_ref:
            member = _resolve_user(user_ref)
            qs = qs.filter(user=member) if member else qs.none()
        return qs

    def create(self, request, *args, **kwargs):
        """Assign a role. The member arrives as ``userId`` (app-visible id) or email.

        Upsert semantics mirror Firestore's composite ``user_roles/{userId}_{shopId}``
        document write, which overwrote a pre-existing role rather than failing.
        """
        shop_ref = first_param(request.data, 'shopId', 'shop_id', 'shop')
        shop_pk = resolve_legacy_pk(Shop, shop_ref)
        if shop_pk is None:
            raise DRFValidationError({'shopId': 'A valid shop is required.'})
        assert_shop_access(request.user, shop_pk, roles=MANAGEMENT_ROLES)

        member = _resolve_user(first_param(request.data, 'userId', 'user_id', 'user', 'email'))
        if member is None:
            raise DRFValidationError({'userId': 'No user matches that id or email.'})
        role_value = request.data.get('role')
        if role_value not in dict(UserRole.ROLE_CHOICES):
            raise DRFValidationError({'role': 'Unknown role.'})

        role_obj, _created = UserRole.objects.update_or_create(
            user=member, shop_id=shop_pk, defaults={'role': role_value}
        )
        serializer = self.get_serializer(role_obj)
        return Response(serializer.data, status=status.HTTP_200_OK)

class InvitationViewSet(LegacyLookupMixin, viewsets.ModelViewSet):
    serializer_class = InvitationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # ?mine=true lists the invitations addressed to the caller: the invitee holds
        # no role in the shop yet, so the shop-scoped view below cannot see them.
        if self.request.query_params.get('mine') in ('true', '1', 'True'):
            qs = Invitation.objects.filter(email__iexact=self.request.user.email)
        else:
            qs = Invitation.objects.filter(shop__user_roles__user=self.request.user).distinct()
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        status_value = self.request.query_params.get('status')
        if shop_id:
            qs = filter_by_ref(qs, 'shop_id', shop_id, Shop)
        if status_value:
            qs = qs.filter(status=status_value)
        return qs

    def perform_create(self, serializer):
        # Inviting staff is restricted to owners/managers of that shop. ``shopId``
        # writes the FK through the legacy-aware field, so it may be a Firestore id.
        data = serializer.validated_data
        assert_shop_access(self.request.user, data['shop'].id, roles=MANAGEMENT_ROLES)
        invitation = services.upsert_invitation(
            shop=data['shop'], email=data['email'], role=data['role'],
            invited_by=self.request.user,
        )
        serializer.instance = invitation

    def destroy(self, request, *args, **kwargs):
        # We perform a hard delete as approved
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _get_own_invitation(self):
        """
        Fetch the invitation by pk on behalf of the invitee.

        Accept/decline happen before any UserRole exists, so the shop-scoped
        queryset would hide them; instead the pk is resolved directly and an
        invitation addressed to another email stays invisible (404).
        """
        invitation = Invitation.objects.filter(pk=self.kwargs.get('pk')).first()
        if invitation is None:
            raise Http404
        if (invitation.email or '').lower() != (self.request.user.email or '').lower():
            raise Http404
        return invitation

    @action(detail=True, methods=['post'])
    def accept(self, request, pk=None):
        invitation = self._get_own_invitation()
        try:
            invitation, _role = services.accept_invitation(user=request.user, invitation=invitation)
        except DRFValidationError as exc:
            raise drf_validation_error(exc)
        return Response(self.get_serializer(invitation).data)

    @action(detail=True, methods=['post'])
    def decline(self, request, pk=None):
        invitation = self._get_own_invitation()
        try:
            invitation = services.decline_invitation(invitation=invitation)
        except DRFValidationError as exc:
            raise drf_validation_error(exc)
        return Response(self.get_serializer(invitation).data)
