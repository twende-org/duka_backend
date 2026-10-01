from django.contrib import admin

from apps.sales.models import Sale, SaleItem, DailySalesSummary, Shift, Order, OrderItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    raw_id_fields = ('product',)


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ('id', 'shop', 'branch', 'attendant', 'total_amount', 'profit', 'payment_method', 'status', 'created_at')
    list_filter = ('payment_method', 'status', 'shop', 'branch')
    search_fields = ('customer_name', 'shop__name')
    raw_id_fields = ('shop', 'branch', 'attendant', 'shift')
    inlines = [SaleItemInline]
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ('id', 'shop', 'status', 'opened_by_name', 'opened_at', 'opening_cash', 'cash_sales_total', 'cash_expenses_total', 'expected_closing_cash', 'discrepancy', 'owner_approval_status')
    list_filter = ('status', 'owner_approval_status', 'shop')
    search_fields = ('opened_by_name', 'closed_by_name', 'shop__name')
    raw_id_fields = ('shop', 'opened_by', 'closed_by')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(DailySalesSummary)
class DailySalesSummaryAdmin(admin.ModelAdmin):
    list_display = ('date', 'shop', 'branch', 'total_sales', 'transactions', 'profit', 'net_profit', 'total_expenses')
    list_filter = ('shop', 'branch', 'date')
    raw_id_fields = ('shop', 'branch')


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    raw_id_fields = ('product',)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'shop', 'customer_name', 'total_amount', 'payment_method', 'status', 'approval_status', 'source', 'created_at')
    list_filter = ('status', 'approval_status', 'source', 'payment_method', 'shop')
    search_fields = ('customer_name', 'customer_phone', 'customer_po_number', 'shop__name')
    raw_id_fields = ('shop', 'branch')
    inlines = [OrderItemInline]
    readonly_fields = ('created_at', 'updated_at')
