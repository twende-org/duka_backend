"""Portal read queries: "my receipts" / "my orders" for a signed-in buyer.

Cross-shop by design — the customer portal lists purchases from every shop —
so the caller's identity is the only filter, and every way a row can link back
to the caller is enumerated here.
"""
from django.db.models import Q, QuerySet

# pyrefly: ignore [missing-import]
from apps.core.phone import phone_formats
# pyrefly: ignore [missing-import]
from apps.crm.models import Customer
# pyrefly: ignore [missing-import]
from apps.users.models import User
from .models import Order, Sale


def identity_ids(user: User) -> set:
    """Values that may appear in a legacy link column for this user.

    Both spellings are in circulation: the Django uuid written by
    ``crm.services._attempt_auto_link`` and the Firebase uid kept on Firestore
    documents (``customerUserId``).
    """
    ids = {str(user.pk)}
    if user.firebase_uid:
        ids.add(user.firebase_uid)
    return ids


def buyer_links(user: User) -> set:
    """Every string a sale/order ``customer_id`` may hold for this caller.

    Mirrors Firebase's ``linkCustomerIdentity``: an explicit ``user_id`` link on
    a CRM customer always counts, and an **unclaimed** row whose phone matches
    the caller's is claimed too — a row linked to someone else never is.
    """
    ids = identity_ids(user)
    match = Q(user_id__in=ids)
    variants = phone_formats(user.phone)
    if variants:
        match |= Q(phone__in=variants) & (Q(user_id__isnull=True) | Q(user_id=''))
    links = set(ids)
    for pk, legacy_id in Customer.objects.filter(match).values_list('id', 'legacy_id'):
        links.add(str(pk))
        if legacy_id:
            links.add(legacy_id)
    return links


def get_portal_sales(user: User) -> QuerySet[Sale]:
    """Completed sales visible to the caller as receipts, newest first.

    A sale is the customer's when the account FK points at them, when its
    ``customer_id`` names one of their CRM rows, or when its stored phone is
    theirs — the same reach Firebase's phone-link write produced, resolved at
    read time instead of writing to Firestore.
    """
    match = Q(customer_user=user) | Q(customer_id__in=buyer_links(user))
    variants = phone_formats(user.phone)
    if variants:
        match |= Q(customer_phone__in=variants)
    return (
        Sale.objects.filter(match)
        .exclude(status='draft')
        .select_related('shop')
        .prefetch_related('items__product')
        .order_by('-created_at')
    )


def get_portal_orders(user: User) -> QuerySet[Order]:
    """Orders placed by the caller, newest first.

    Matched on the account link only: guest checkouts carry no
    ``customerUserId`` on Firestore either, so phone matching would surface
    orders the customer never claimed.
    """
    return (
        Order.objects.filter(Q(customer_user=user) | Q(customer_id__in=buyer_links(user)))
        .select_related('shop')
        .prefetch_related('items__product')
        .order_by('-created_at')
    )
