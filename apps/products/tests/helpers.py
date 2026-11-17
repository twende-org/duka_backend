"""Row factories shared by the products and sales test packages.

Deliberately outside any one test module: POS checkout needs a shop, a branch and
a catalogue row exactly as much as tap-to-add does, and a third copy of that
fixture would be the first thing to drift.
"""
from itertools import count

from apps.products.models import MasterProduct
from apps.shops.models import Branch, Shop, UserRole
from apps.users.models import User

# ``User.email`` is unique, and one test routinely needs three attendants in the
# same shop, so every staff row takes the next number rather than a name-derived
# address that would collide on the second call.
_staff_seq = count()


def shop_with_branch(name):
    """A shop and its main branch, with no staff attached."""
    shop = Shop.objects.create(name=name, isPublic=True)
    branch = Branch.objects.create(shop=shop, name='Main Branch', is_main=True)
    return shop, branch


def add_staff(shop, name, role='attendant'):
    """A user holding ``role`` in ``shop``; the name only has to be unique per shop."""
    slug = f'{name.lower().replace(" ", "_")}_{role}_{next(_staff_seq)}'
    user = User.objects.create_user(username=slug, email=f'{slug}@example.com')
    UserRole.objects.create(user=user, shop=shop, role=role)
    return user


def make_shop(name, owner_role='owner'):
    """``(shop, main branch, one staff user)`` — the fixture nearly every test starts from.

    ``owner_role`` is the role that user holds, so passing ``'attendant'`` yields a
    caller the management-only rules are meant to block.
    """
    shop, branch = shop_with_branch(name)
    return shop, branch, add_staff(shop, name, owner_role)


def catalog(name, brand='Tecno', category='Smartphones', **kwargs):
    """A shared-catalogue row, active and verified unless the test says otherwise."""
    return MasterProduct.objects.create(
        name=name, brand=brand, category_name=category, **kwargs
    )
