from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

# pyrefly: ignore [missing-import]
from apps.shops.models import UserRole

# Roles allowed to manage membership (invite staff, assign roles) per spec §8.
MANAGEMENT_ROLES = ('owner', 'manager')


def get_user_shop_ids(user):
    """Shop ids where the user holds a role; None means unrestricted (superuser)."""
    if user.is_superuser:
        return None
    return set(UserRole.objects.filter(user=user).values_list('shop_id', flat=True))


def assert_shop_access(user, shop_id, roles=None):
    """Raise PermissionDenied unless the user holds a qualifying role in the shop."""
    if shop_id is None or user.is_superuser:
        return
    qs = UserRole.objects.filter(user=user, shop_id=shop_id)
    if roles:
        qs = qs.filter(role__in=roles)
    if not qs.exists():
        raise PermissionDenied('You do not have access to this shop.')


class ShopScopedQuerysetMixin:
    """
    Restricts list/retrieve — and via get_object(), update/destroy — to rows whose
    shop the request user holds a role in. Other shops' rows are filtered out
    entirely (detail lookups 404) so their existence is not leaked.

    shop_paths: lookup paths from the model to a shop id; multiple paths are OR-ed
    so a model reachable from either side of a B2B relationship still resolves.
    """

    shop_paths = ('shop_id',)

    def scope_queryset(self, qs):
        shop_ids = get_user_shop_ids(self.request.user)
        if shop_ids is None:
            return qs
        if not shop_ids:
            return qs.none()
        condition = Q()
        for path in self.shop_paths:
            condition |= Q(**{f'{path}__in': shop_ids})
        return qs.filter(condition)
