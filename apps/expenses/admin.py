from django.contrib import admin

from apps.expenses.models import Expense


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ('date', 'shop', 'branch', 'category', 'description', 'amount', 'payment_method', 'paid_to', 'is_recurring')
    list_filter = ('category', 'payment_method', 'is_recurring', 'shop', 'date')
    search_fields = ('description', 'reference', 'paid_to', 'shop__name')
    raw_id_fields = ('shop', 'branch', 'shift', 'recorded_by')
    readonly_fields = ('created_at', 'updated_at')
