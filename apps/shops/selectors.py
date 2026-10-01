import re

from django.db.models import Q, QuerySet
from .models import Shop, Branch, UserRole
# pyrefly: ignore [missing-import]
from apps.core.legacy import is_uuid
# pyrefly: ignore [missing-import]
from apps.users.models import User

_NON_SLUG = re.compile(r'[^a-z0-9]+')

def get_user_shops(*, user: User) -> QuerySet[Shop]:
    """
    Returns a queryset of shops the user has access to.
    Superusers see all shops.
    """
    if user.is_superuser:
        return Shop.objects.all()
    return Shop.objects.filter(user_roles__user=user).distinct()

def get_shop_branches(*, shop: Shop) -> QuerySet[Branch]:
    """
    Returns branches for a given shop.
    """
    return Branch.objects.filter(shop=shop)


def app_slug(value: str) -> str:
    """Mirror of the frontend ``createSlug`` (lowercase, non-alphanumerics → '-')."""
    return _NON_SLUG.sub('-', (value or '').lower()).strip('-')


def get_public_shops(*, is_public=None, is_wholesale_supplier=None,
                     search=None, slug=None) -> QuerySet[Shop]:
    """Storefront shop list. ``None`` filters are ignored; omit is_public to see all."""
    shops = Shop.objects.all()
    if is_public is not None:
        shops = shops.filter(isPublic=is_public)
    if is_wholesale_supplier is not None:
        shops = shops.filter(isWholesaleSupplier=is_wholesale_supplier)
    if slug:
        shops = shops.filter(slug=slug)
    if search:
        shops = shops.filter(
            Q(name__icontains=search)
            | Q(description__icontains=search)
            | Q(location__icontains=search)
        )
    return shops


def get_shop_by_identifier(identifier: str):
    """Resolve a storefront URL segment to a shop, or None.

    Matches the frontend's getShopBySlugOrId chain: stored slug, then id
    (Firestore legacy id or Django uuid), then the slugified name for links
    generated from shops that never stored a slug — the frontend slugifies with
    its own rule, which differs from Django's ``slugify`` for punctuation.
    """
    value = (identifier or '').strip()
    if not value:
        return None
    match = Q(slug=value) | Q(legacy_id=value)
    if is_uuid(value):
        match |= Q(id=value)
    shop = Shop.objects.filter(match).first()
    if shop is not None:
        return shop
    wanted = app_slug(value)
    if not wanted:
        return None
    return next((s for s in Shop.objects.all() if app_slug(s.name) == wanted), None)
