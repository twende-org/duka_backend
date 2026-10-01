from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.users.models import CustomerAddress, Subscription, User, WishlistItem


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('username', 'email', 'phone', 'display_name', 'account_type', 'is_staff', 'is_suspended', 'is_active')
    list_filter = ('account_type', 'is_staff', 'is_suspended', 'is_active', 'can_manage_business', 'can_shop', 'can_buy_for_business')
    search_fields = ('username', 'email', 'phone', 'display_name')
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Biashara profile', {'fields': ('phone', 'display_name', 'account_type', 'is_suspended', 'business_profile', 'corporate_profile', 'can_manage_business', 'can_shop', 'can_buy_for_business')}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Biashara profile', {'fields': ('phone', 'display_name', 'account_type')}),
    )


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'user_email', 'plan', 'status', 'start_date', 'end_date', 'amount', 'confirmed_by')
    list_filter = ('plan', 'status')
    search_fields = ('user_email', 'user_name', 'payment_reference')
    raw_id_fields = ('user',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(WishlistItem)
class WishlistItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'shop_name', 'price', 'created_at')
    search_fields = ('name', 'product_id', 'shop_name', 'user__email')
    raw_id_fields = ('user',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(CustomerAddress)
class CustomerAddressAdmin(admin.ModelAdmin):
    list_display = ('tag', 'name', 'user', 'phone', 'city', 'is_default', 'created_at')
    list_filter = ('tag', 'is_default')
    search_fields = ('name', 'phone', 'street', 'city', 'user__email')
    raw_id_fields = ('user',)
    readonly_fields = ('created_at', 'updated_at')
