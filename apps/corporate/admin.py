from django.contrib import admin

from apps.corporate.models import CorporateDepartment, CorporatePurchaseOrder


@admin.register(CorporateDepartment)
class CorporateDepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'company_id', 'budget', 'spent', 'created_at')
    search_fields = ('name', 'company_id')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(CorporatePurchaseOrder)
class CorporatePurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ('department_name', 'company_id', 'buyer_name', 'total_amount', 'approval_status', 'created_at')
    list_filter = ('approval_status',)
    search_fields = ('company_id', 'shop_id', 'shop_name', 'buyer_name', 'approver_id')
    readonly_fields = ('created_at', 'updated_at')
