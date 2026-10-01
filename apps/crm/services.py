from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from apps.crm.models import Customer, CustomerPayment, CustomerInvoice, Supplier, SupplierInvoice, SupplierPayment
# pyrefly: ignore [missing-import]
from apps.core.legacy import is_uuid
# pyrefly: ignore [missing-import]
from apps.core.phone import phone_formats
from apps.users.models import User
from decimal import Decimal

def find_user_for_identity(*, user_id=None, phone=None):
    """The platform account behind a legacy link column and/or a phone number.

    ``Customer.user_id`` holds either a Django uuid (written by
    ``_attempt_auto_link``) or a Firebase uid (kept from Firestore documents), so
    both spellings are accepted; phones match through every common spelling
    (``0712…``, ``255712…``, ``+255712…``), like ``getPhoneFormats`` did.
    """
    candidate = str(user_id) if user_id else ''
    if candidate:
        match = Q(firebase_uid=candidate)
        if is_uuid(candidate):
            match |= Q(pk=candidate)
        user = User.objects.filter(match).first()
        if user is not None:
            return user
    variants = phone_formats(phone)
    if variants:
        return User.objects.filter(phone__in=variants).first()
    return None


def resolve_sale_customer_user(*, customer_id=None, customer_phone=None):
    """The buyer's platform account for a new sale/order, Firestore-style.

    Firebase's ``addSale`` read ``customers/{customerId}.userId`` first and,
    when that was empty, matched a ``users`` document by phone. ``customer_id``
    here is the *customer row* id (Firebase id or Django uuid), never a user id.
    """
    customer = None
    if customer_id:
        value = str(customer_id)
        if is_uuid(value):
            customer = Customer.objects.filter(id=value).first()
        if customer is None:
            customer = Customer.objects.filter(legacy_id=value).first()
    if customer is None and not customer_phone:
        return None
    return find_user_for_identity(
        user_id=customer.user_id if customer else None,
        phone=customer_phone or (customer.phone if customer else None),
    )


def _attempt_auto_link(customer):
    """
    Looks for a platform user with a matching phone number.
    If found, links the user_id to the customer.
    """
    if customer.phone and not customer.user_id:
        user = find_user_for_identity(phone=customer.phone)
        if user:
            customer.user_id = str(user.id)
            customer.linked_at = timezone.now()
            return True
    return False

def _backfill_historical_sales(customer):
    """Stamp the freshly linked account onto this customer's past sales/orders.

    Mirrors Firestore's ``updateCustomer`` backfill, which set ``customerUserId``
    on sales linked by ``customerId`` and on same-shop sales whose phone matched
    (repairing their ``customerId`` too). The portal reads depend on that link.
    """
    user = find_user_for_identity(user_id=customer.user_id)
    if user is None:
        return

    from apps.sales.models import Order, Sale

    links = {str(customer.pk)}
    if customer.legacy_id:
        links.add(customer.legacy_id)
    Sale.objects.filter(customer_id__in=links).update(customer_user=user)
    Order.objects.filter(customer_id__in=links).update(customer_user=user)

    variants = phone_formats(customer.phone)
    if variants:
        Sale.objects.filter(shop=customer.shop, customer_phone__in=variants).update(
            customer_user=user, customer_id=customer.legacy_id or str(customer.pk))

def _identity_user_ids(user):
    """Every spelling of this account stored in ``Customer.user_id``.

    Firestore's ``resolveIdentity`` compared against the document id, which is the
    Firebase uid; Django rows may hold either the Firebase uid or the Django uuid.
    """
    ids = {str(user.pk)}
    firebase_uid = getattr(user, 'firebase_uid', None)
    if firebase_uid:
        ids.add(str(firebase_uid))
    return ids


def _customer_identity_query(phone=None, email=None):
    """All CRM rows (any shop) reachable through this phone and/or email.

    The legacy function ran a collection-group query; each tenant keeps its own
    ``Customer`` rows here, so the match is deliberately shop-agnostic.
    """
    match = Q()
    variants = phone_formats(phone)
    if variants:
        match |= Q(phone__in=variants)
    if email:
        match |= Q(email__iexact=email)
    return match


def link_identity(user, *, phone=None, email=None):
    """Port of Firebase's ``resolveIdentity``: attach this account to its CRM rows.

    Firestore matched every ``customers`` document by phone spellings and/or
    email, linked the unclaimed ones (and re-stamped ones already owned by the
    caller), backfilled their historical sales/orders, and reported the total
    match count — including rows owned by a different account, which are left
    untouched. ``linkedCount`` is preserved the same way for the callers that
    display it.
    """
    match = _customer_identity_query(phone=phone, email=email)
    if not match:
        return 0

    mine = _identity_user_ids(user)
    now = timezone.now()
    matches = list(Customer.objects.filter(match))
    for customer in matches:
        if customer.user_id and customer.user_id not in mine:
            continue
        if not customer.user_id:
            customer.user_id = str(user.pk)
            customer.linked_at = now
            customer.save(update_fields=['user_id', 'linked_at'])
        _backfill_historical_sales(customer)

    return len(matches)


@transaction.atomic
def create_customer(shop, **kwargs):
    customer = Customer(shop=shop, **kwargs)
    _attempt_auto_link(customer)
    customer.save()
    return customer

@transaction.atomic
def update_customer(customer, **kwargs):
    was_unlinked = not bool(customer.user_id)
    
    for key, value in kwargs.items():
        setattr(customer, key, value)
        
    just_linked = False
    if was_unlinked:
        just_linked = _attempt_auto_link(customer)
        
    customer.save()
    
    if just_linked:
        _backfill_historical_sales(customer)
        
    return customer

@transaction.atomic
def process_customer_payment(shop, customer, amount: Decimal, method: str, reference: str = '', notes: str = '', invoice_ids: list = None):
    # 1. Lock the customer and validate the payment amount
    customer = Customer.objects.select_for_update().get(id=customer.id)
    if amount <= 0:
        raise ValidationError("Payment amount must be greater than zero.")
    if amount > customer.outstanding_balance:
        raise ValidationError(
            f"Payment ({amount}) exceeds the customer's outstanding balance ({customer.outstanding_balance})."
        )

    # 2. Create Payment Record
    payment = CustomerPayment.objects.create(
        shop=shop,
        customer=customer,
        amount=amount,
        method=method,
        reference=reference,
        notes=notes,
        date=timezone.now().date()
    )

    # 3. Reduce Customer's outstanding balance
    customer.outstanding_balance -= amount
    customer.save(update_fields=['outstanding_balance'])

    # 4. Apply to Invoices if provided
    # Allocation order must be deterministic: oldest due date first (invoices without
    # a due date last), then oldest created — otherwise the split is undefined.
    remaining_amount = amount
    if invoice_ids:
        invoices = (
            CustomerInvoice.objects
            .select_for_update()
            .filter(id__in=invoice_ids, customer=customer)
            .order_by(F('due_date').asc(nulls_last=True), 'created_at')
        )
        for inv in invoices:
            if remaining_amount <= 0:
                break

            amount_to_pay = inv.amount_due - inv.amount_paid
            if amount_to_pay <= 0:
                continue

            if remaining_amount >= amount_to_pay:
                inv.amount_paid += amount_to_pay
                remaining_amount -= amount_to_pay
                inv.status = 'paid'
            else:
                inv.amount_paid += remaining_amount
                remaining_amount = 0
                inv.status = 'pending'

            inv.save(update_fields=['amount_paid', 'status'])
            payment.invoices.add(inv)

    return payment

@transaction.atomic
def create_supplier(shop, **kwargs):
    supplier = Supplier(shop=shop, **kwargs)
    supplier.save()
    return supplier

@transaction.atomic
def update_supplier(supplier, **kwargs):
    for key, value in kwargs.items():
        setattr(supplier, key, value)
    supplier.save()
    return supplier

@transaction.atomic
def process_supplier_payment(shop, supplier, amount: Decimal, method: str, reference: str = '', notes: str = '', invoice_ids: list = None):
    # 1. Lock the supplier and validate the payment amount
    supplier = Supplier.objects.select_for_update().get(id=supplier.id)
    if amount <= 0:
        raise ValidationError("Payment amount must be greater than zero.")
    if amount > supplier.outstanding_balance:
        raise ValidationError(
            f"Payment ({amount}) exceeds the supplier's outstanding balance ({supplier.outstanding_balance})."
        )

    # 2. Create Payment Record
    payment = SupplierPayment.objects.create(
        shop=shop,
        supplier=supplier,
        amount=amount,
        method=method,
        reference=reference,
        notes=notes,
        date=timezone.now().date()
    )

    # 3. Reduce Supplier's outstanding balance
    supplier.outstanding_balance -= amount
    supplier.save(update_fields=['outstanding_balance'])

    # 4. Apply to Invoices if provided
    # Allocation order must be deterministic: oldest due date first (invoices without
    # a due date last), then oldest created — otherwise the split is undefined.
    remaining_amount = amount
    if invoice_ids:
        invoices = (
            SupplierInvoice.objects
            .select_for_update()
            .filter(id__in=invoice_ids, supplier=supplier)
            .order_by(F('due_date').asc(nulls_last=True), 'created_at')
        )
        for inv in invoices:
            if remaining_amount <= 0:
                break

            amount_to_pay = inv.amount_due - inv.amount_paid
            if amount_to_pay <= 0:
                continue

            if remaining_amount >= amount_to_pay:
                inv.amount_paid += amount_to_pay
                remaining_amount -= amount_to_pay
                inv.status = 'paid'
            else:
                inv.amount_paid += remaining_amount
                remaining_amount = 0
                inv.status = 'pending'

            inv.save(update_fields=['amount_paid', 'status'])
            payment.invoices.add(inv)

    return payment
