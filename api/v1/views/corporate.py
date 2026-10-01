"""Corporate procurement API (legacy ``companies/{id}`` subcollections).

Departments and purchase orders were Firestore documents under
``companies/{companyId}/...``; buyers are plain users whose ``corporateProfile``
carries the company id. All three viewsets are therefore company-scoped from the
caller's own profile:

* any member may read their company's departments, colleagues and orders;
* creating a department is an admin action (legacy "Unda Idara" gate);
* approving/rejecting an order is an approver action (legacy ``isApprover``).

Platform staff bypass the gate and may pass any ``?companyId=`` to inspect a
company. ``?companyId=`` must never widen a regular member's scope — a member
only ever sees their own company.
"""
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

# pyrefly: ignore [missing-import]
from apps.core.utils import first_param
# pyrefly: ignore [missing-import]
from apps.corporate.models import CorporateDepartment, CorporatePurchaseOrder
# pyrefly: ignore [missing-import]
from apps.users.models import User
# pyrefly: ignore [missing-import]
from api.v1.serializers.corporate import (
    CorporateBuyerSerializer, CorporateDepartmentSerializer,
    CorporatePurchaseOrderSerializer,
)
# pyrefly: ignore [missing-import]
from api.v1.serializers.users import app_visible_user_id


def corporate_company_id(user) -> str:
    """The caller's company id from ``corporateProfile`` (tolerates either key)."""
    profile = user.corporate_profile or {}
    return str(profile.get('companyId') or profile.get('company_id') or '')


def corporate_role(user) -> str:
    profile = user.corporate_profile or {}
    return str(profile.get('role') or '')


class CorporateCompanyScopedMixin:
    """Resolves the company a request may act on and enforces the role gates."""

    def scoped_company_id(self) -> str:
        user = self.request.user
        requested = first_param(self.request.query_params, 'company_id', 'companyId')
        if user.is_staff or user.is_superuser:
            company_id = requested or corporate_company_id(user)
            if not company_id:
                raise ValidationError({'companyId': 'This field is required.'})
            return company_id
        own = corporate_company_id(user)
        if not own:
            raise PermissionDenied('You are not a member of any corporate account.')
        if requested and requested != own:
            raise PermissionDenied('You can only access your own company records.')
        return own

    def require_corporate_role(self, allowed):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return
        if corporate_role(user) not in allowed:
            raise PermissionDenied('Your corporate role does not allow this action.')


class CorporateDepartmentViewSet(
    CorporateCompanyScopedMixin, mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet
):
    """Company departments: members read, admins create."""

    serializer_class = CorporateDepartmentSerializer
    permission_classes = [IsAuthenticated]
    queryset = CorporateDepartment.objects.all()

    def get_queryset(self):
        return self.queryset.filter(company_id=self.scoped_company_id())

    def create(self, request, *args, **kwargs):
        self.require_corporate_role(('admin',))
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        department = serializer.save(company_id=self.scoped_company_id())
        return Response(
            self.get_serializer(department).data, status=status.HTTP_201_CREATED
        )


class CorporateBuyerViewSet(
    CorporateCompanyScopedMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    """The company's members (legacy ``users`` query on ``corporateProfile.companyId``)."""

    serializer_class = CorporateBuyerSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        company_id = self.scoped_company_id()
        return User.objects.filter(
            Q(corporate_profile__companyId=company_id)
            | Q(corporate_profile__company_id=company_id)
        ).order_by('-date_joined')


class CorporatePurchaseOrderViewSet(
    CorporateCompanyScopedMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Company purchase orders: members read and file, approvers decide."""

    serializer_class = CorporatePurchaseOrderSerializer
    permission_classes = [IsAuthenticated]
    queryset = CorporatePurchaseOrder.objects.all().order_by('-created_at')

    def get_queryset(self):
        return self.queryset.filter(company_id=self.scoped_company_id())

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        order = serializer.save(
            company_id=self.scoped_company_id(),
            # The checkout sends the signed-in buyer; fall back to the token.
            buyer_id=data.get('buyer_id') or app_visible_user_id(request.user),
            approval_status='pending_approval',
        )
        return Response(self.get_serializer(order).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        self.require_corporate_role(('approver', 'admin'))
        order = self.get_object()
        order.approval_status = 'APPROVED'
        order.approver_id = request.data.get('approverId') or app_visible_user_id(request.user)
        order.approved_at = timezone.now()
        order.save(update_fields=['approval_status', 'approver_id', 'approved_at', 'updated_at'])
        return Response(self.get_serializer(order).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        self.require_corporate_role(('approver', 'admin'))
        order = self.get_object()
        order.approval_status = 'REJECTED'
        order.rejected_at = timezone.now()
        order.save(update_fields=['approval_status', 'rejected_at', 'updated_at'])
        return Response(self.get_serializer(order).data)
