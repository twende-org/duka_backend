from django.contrib import admin

from apps.shops.models import Shop, Branch, UserRole, Invitation


class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0


class UserRoleInline(admin.TabularInline):
    model = UserRole
    extra = 0


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'country', 'businessType', 'isPublic', 'isWholesaleSupplier', 'created_at')
    list_filter = ('isPublic', 'isWholesaleSupplier', 'keepsStock', 'country')
    search_fields = ('name', 'slug', 'phone', 'email')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [BranchInline, UserRoleInline]
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ('name', 'shop', 'is_main', 'branch_type', 'is_active')
    list_filter = ('is_main', 'is_active', 'branch_type')
    search_fields = ('name', 'shop__name')
    raw_id_fields = ('shop', 'manager')


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    list_display = ('user', 'shop', 'role', 'created_at')
    list_filter = ('role',)
    search_fields = ('user__username', 'shop__name')
    raw_id_fields = ('user', 'shop')


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ('email', 'shop', 'role', 'status', 'invited_by', 'created_at')
    list_filter = ('status', 'role')
    search_fields = ('email', 'shop__name')
    raw_id_fields = ('shop', 'invited_by')
