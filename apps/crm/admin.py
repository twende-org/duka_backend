from django.contrib import admin

from apps.crm.models import (
    Customer, CustomerInvoice, CustomerPayment,
    Supplier, SupplierInvoice, SupplierPayment,
)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('name', 'shop', 'customer_type', 'phone', 'outstanding_balance', 'credit_limit', 'total_purchases', 'last_purchase_date')
    list_filter = ('customer_type', 'shop')
    search_fields = ('name', 'phone', 'email', 'business_name', 'shop__name')
    raw_id_fields = ('shop',)


@admin.register(CustomerInvoice)
class CustomerInvoiceAdmin(admin.ModelAdmin):
    list_display = ('customer', 'shop', 'amount_due', 'amount_paid', 'due_date', 'status', 'created_at')
    list_filter = ('status', 'shop')
    search_fields = ('customer__name', 'shop__name')
    raw_id_fields = ('customer', 'shop', 'order')


@admin.register(CustomerPayment)
class CustomerPaymentAdmin(admin.ModelAdmin):
    list_display = ('customer', 'shop', 'amount', 'method', 'reference', 'date')
    list_filter = ('method', 'shop')
    search_fields = ('customer__name', 'reference', 'shop__name')
    raw_id_fields = ('customer', 'shop')


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'shop', 'phone', 'outstanding_balance', 'total_purchases', 'total_spent')
    list_filter = ('shop',)
    search_fields = ('name', 'phone', 'email', 'shop__name')
    raw_id_fields = ('shop',)


@admin.register(SupplierInvoice)
class SupplierInvoiceAdmin(admin.ModelAdmin):
    list_display = ('supplier', 'shop', 'amount_due', 'amount_paid', 'due_date', 'status', 'created_at')
    list_filter = ('status', 'shop')
    search_fields = ('supplier__name', 'shop__name')
    raw_id_fields = ('supplier', 'shop', 'purchase_order')


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(admin.ModelAdmin):
    list_display = ('supplier', 'shop', 'amount', 'method', 'reference', 'date')
    list_filter = ('method', 'shop')
    search_fields = ('supplier__name', 'reference', 'shop__name')
    raw_id_fields = ('supplier', 'shop')
