from django.core.exceptions import ValidationError
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.expenses.models import Expense
from apps.expenses.services import record_expense, update_expense, delete_expense
from apps.shops.models import Shop, Branch
from apps.core.legacy import filter_by_ref
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access
from apps.core.utils import first_param, drf_validation_error
from api.v1.serializers.expenses import ExpenseSerializer


class ExpenseViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = Expense.objects.all()
    serializer_class = ExpenseSerializer
    permission_classes = [IsAuthenticated]
    shop_paths = ('shop_id',)

    def get_queryset(self):
        queryset = self.scope_queryset(super().get_queryset())
        params = self.request.query_params

        shop_id = first_param(params, 'shop_id', 'shopId')
        branch_id = first_param(params, 'branch_id', 'branchId')
        category = params.get('category')
        payment_method = first_param(params, 'payment_method', 'paymentMethod')
        date_from = first_param(params, 'date_from', 'dateFrom')
        date_to = first_param(params, 'date_to', 'dateTo')
        search = params.get('search')

        if shop_id:
            queryset = filter_by_ref(queryset, 'shop_id', shop_id, Shop)
        if branch_id:
            queryset = filter_by_ref(queryset, 'branch_id', branch_id, Branch)
        if category:
            queryset = queryset.filter(category=category)
        if payment_method:
            queryset = queryset.filter(payment_method=payment_method)
        if date_from:
            queryset = queryset.filter(date__gte=date_from)
        if date_to:
            queryset = queryset.filter(date__lte=date_to)
        if search:
            queryset = queryset.filter(
                Q(description__icontains=search) | Q(reference__icontains=search)
                | Q(paid_to__icontains=search) | Q(category__icontains=search)
            )

        return queryset

    def perform_create(self, serializer):
        data = dict(serializer.validated_data)
        shop = data.pop('shop')
        assert_shop_access(self.request.user, shop.id)
        try:
            serializer.instance = record_expense(shop=shop, recorded_by=self.request.user, **data)
        except ValidationError as e:
            raise drf_validation_error(e)

    def perform_update(self, serializer):
        data = dict(serializer.validated_data)
        shop = data.pop('shop', serializer.instance.shop)
        assert_shop_access(self.request.user, shop.id)
        try:
            serializer.instance = update_expense(serializer.instance, **data)
        except ValidationError as e:
            raise drf_validation_error(e)

    def perform_destroy(self, instance):
        delete_expense(instance)
