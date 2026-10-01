import datetime
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils import timezone

# pyrefly: ignore [missing-import]
from apps.core.legacy import is_uuid, filter_by_ref
# pyrefly: ignore [missing-import]
from apps.crm.services import resolve_sale_customer_user
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.sales.models import Sale, SaleItem, DailySalesSummary, Shift
# pyrefly: ignore [missing-import]
from apps.products.services import adjust_inventory

# The UI writes "Taslimu" while API callers use "cash"; Firebase only matched
# the literal "cash" in some paths, so both spellings must count as cash here.
CASH_PAYMENT_METHODS = {'cash', 'taslimu'}

# The POS select posts the label the cashier sees; Firestore stored a slug and
# the UI maps slugs back to labels, so both directions live here.
PAYMENT_METHOD_SLUGS = {
    'cash': 'cash',
    'taslimu': 'cash',
    'pesa taslimu': 'cash',
    'mpesa': 'mpesa',
    'm-pesa': 'mpesa',
    'm pesa': 'mpesa',
    'tigopesa': 'tigopesa',
    'tigo pesa': 'tigopesa',
    'airtel_money': 'airtel_money',
    'airtel money': 'airtel_money',
    'halopesa': 'halopesa',
    'halo pesa': 'halopesa',
    'bank': 'bank',
    'benki': 'bank',
    'bank transfer': 'bank',
    'card': 'card',
    'kadi': 'card',
    'credit': 'credit',
    'mkopo': 'credit',
    'deni': 'credit',
}

PAYMENT_METHOD_DISPLAY = {
    'cash': 'Taslimu',
    'mpesa': 'M-Pesa',
    'tigopesa': 'Tigo Pesa',
    'airtel_money': 'Airtel Money',
    'halopesa': 'Halopesa',
    'bank': 'Benki',
    'card': 'Kadi',
    'credit': 'Mkopo',
}


def normalize_payment_method(value, default: str = 'cash') -> str:
    """Collapses the app's spellings onto the stored slug; unknown non-blank values are rejected."""
    text = (value or '').strip().lower()
    if not text:
        return default
    slug = PAYMENT_METHOD_SLUGS.get(text)
    if slug is None:
        raise ValidationError(f"Njia ya malipo '{value}' haitambuliki.")
    return slug


def display_payment_method(value) -> str:
    """Mirrors the stored slug back to the cashier's label; unknown values pass through untouched."""
    text = (value or '').strip()
    return PAYMENT_METHOD_DISPLAY.get(text.lower(), text)


def is_cash_payment(payment_method) -> bool:
    return (payment_method or '').strip().lower() in CASH_PAYMENT_METHODS


def apply_daily_summary_delta(
    shop: Shop,
    branch: Branch,
    date,
    *,
    total_sales=Decimal('0.00'),
    transactions: int = 0,
    profit=Decimal('0.00'),
    net_profit=None,
    total_expenses=Decimal('0.00'),
):
    """
    Applies signed deltas to the cached daily summary row, creating it on first use.
    Must be called inside an atomic block; locks the row with select_for_update.
    """
    if branch is None:
        return None

    summary, _ = DailySalesSummary.objects.get_or_create(
        shop=shop,
        branch=branch,
        date=date,
    )
    locked_summary = DailySalesSummary.objects.select_for_update().get(id=summary.id)
    locked_summary.total_sales += total_sales
    locked_summary.transactions += transactions
    locked_summary.profit += profit
    locked_summary.net_profit += profit if net_profit is None else net_profit
    locked_summary.total_expenses += total_expenses
    locked_summary.save()
    return locked_summary


@transaction.atomic
def open_shift(
    shop: Shop,
    opened_by=None,
    opened_by_name: str = '',
    opened_at=None,
    opening_cash=Decimal('0.00'),
    notes: str = None,
):
    existing = Shift.objects.select_for_update().filter(shop=shop, status='OPEN').first()
    if existing:
        raise ValidationError("An open shift already exists for this shop. Please close it first.")

    shift = Shift.objects.create(
        shop=shop,
        status='OPEN',
        opened_by=opened_by,
        opened_by_name=opened_by_name or '',
        opened_at=opened_at or timezone.now(),
        opening_cash=opening_cash or Decimal('0.00'),
        expected_closing_cash=opening_cash or Decimal('0.00'),
        notes=notes,
    )
    return shift


@transaction.atomic
def close_shift(
    shift: Shift,
    closed_by=None,
    closed_by_name: str = '',
    actual_closing_cash=Decimal('0.00'),
    cash_left_for_next_day=Decimal('0.00'),
    cash_submitted_to_owner=Decimal('0.00'),
    notes: str = None,
):
    locked_shift = Shift.objects.select_for_update().get(id=shift.id)
    if locked_shift.status == 'CLOSED':
        raise ValidationError("This shift is already closed.")

    expected_cash = (
        locked_shift.opening_cash
        + locked_shift.cash_sales_total
        - locked_shift.cash_expenses_total
    )

    locked_shift.expected_closing_cash = expected_cash
    locked_shift.discrepancy = actual_closing_cash - expected_cash
    locked_shift.actual_closing_cash = actual_closing_cash
    locked_shift.cash_left_for_next_day = cash_left_for_next_day
    locked_shift.cash_submitted_to_owner = cash_submitted_to_owner
    locked_shift.closed_by = closed_by
    locked_shift.closed_by_name = closed_by_name or ''
    locked_shift.closed_at = timezone.now()
    locked_shift.owner_approval_status = 'PENDING'
    locked_shift.status = 'CLOSED'
    if notes is not None:
        locked_shift.notes = notes
    locked_shift.save()
    return locked_shift


def _price_items(items_data: list):
    """
    Prices each cart line, honouring the discounted line total the POS sends.

    ``total_price`` is the cart's ``lineTotal`` (unit price after the line
    discount, times quantity); when absent the list price is used. Returns
    ``(processed_items, total_amount, total_profit, total_discount)``.
    items_data format: [{'product': ProductInstance, 'quantity': 10, 'unit_price': 500}, ...]
    """
    processed_items = []
    total_amount = Decimal('0.00')
    total_profit = Decimal('0.00')
    total_discount = Decimal('0.00')

    for item_data in items_data:
        product = item_data['product']
        quantity = int(item_data['quantity'])
        unit_price = Decimal(str(item_data['unit_price']))

        if quantity <= 0:
            raise ValidationError(f"Quantity for {product.name} must be greater than zero.")

        list_price = unit_price * quantity
        subtotal = item_data.get('total_price')
        subtotal = list_price if subtotal is None else Decimal(str(subtotal))
        if subtotal < 0:
            raise ValidationError(f"Total for {product.name} cannot be negative.")

        buying_price = product.buying_price or Decimal('0.00')
        item_profit = subtotal - buying_price * quantity

        total_amount += subtotal
        total_profit += item_profit
        total_discount += list_price - subtotal

        processed_items.append({
            'product': product,
            'quantity': quantity,
            'unit_price': unit_price,
            'total_price': subtotal,
            'profit': item_profit,
        })

    return processed_items, total_amount, total_profit, total_discount


@transaction.atomic
def process_pos_sale(
    shop: Shop,
    branch: Branch,
    items_data: list,
    payment_method: str,
    attendant=None,
    customer_id: str = None,
    customer_name: str = None,
    customer_phone: str = None,
    notes: str = None,
    shift: Shift = None,
):
    """
    Atomically processes a Point of Sale transaction.
    - Validates and deducts stock for all items
    - Calculates total price and profit (per-line discounted totals win)
    - Creates Sale and SaleItem records
    - Increments the DailySalesSummary for the branch
    - Links the sale to an open shift and adds cash payments to its drawer total

    items_data format: [{'product': ProductInstance, 'quantity': 10, 'unit_price': 500}, ...]
    """
    if not items_data:
        raise ValidationError("Sale must contain at least one item.")

    locked_shift = None
    if shift is not None:
        # Lock the shift for the whole sale so it cannot be closed mid-transaction.
        locked_shift = Shift.objects.select_for_update().filter(id=shift.id, shop=shop).first()
        if locked_shift is None:
            raise ValidationError("Shift does not belong to this shop.")
        if locked_shift.status != 'OPEN':
            raise ValidationError("Shift is not open.")

    processed_items, total_sale_amount, total_sale_profit, total_discount = _price_items(items_data)

    for p_item in processed_items:
        # adjust_inventory locks the row and raises ValidationError on insufficient stock.
        adjust_inventory(
            product=p_item['product'],
            branch=branch,
            movement_type='sale',
            quantity_change=-p_item['quantity'],
            user=attendant,
            reason='POS Sale'
        )

    payment_method = normalize_payment_method(payment_method)

    # Create Sale Record
    sale = Sale.objects.create(
        shop=shop,
        branch=branch,
        attendant=attendant,
        shift=locked_shift,
        total_amount=total_sale_amount,
        discount_amount=total_discount,
        profit=total_sale_profit,
        payment_method=payment_method,
        customer_id=customer_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        # Portal receipts are found through this link; resolve it the way
        # Firestore's addSale did (customer row's userId, else phone match).
        customer_user=resolve_sale_customer_user(
            customer_id=customer_id, customer_phone=customer_phone),
        notes=notes,
        status='completed'
    )
    
    # Create Sale Items
    sale_item_objs = []
    for p_item in processed_items:
        sale_item_objs.append(
            SaleItem(
                sale=sale,
                product=p_item['product'],
                product_name=p_item['product'].name,
                quantity=p_item['quantity'],
                unit_price=p_item['unit_price'],
                total_price=p_item['total_price'],
                profit=p_item['profit']
            )
        )
    SaleItem.objects.bulk_create(sale_item_objs)
    
    # Update Daily Summary Atomically
    today = timezone.localtime(timezone.now()).date()
    apply_daily_summary_delta(
        shop=shop,
        branch=branch,
        date=today,
        total_sales=total_sale_amount,
        transactions=1,
        profit=total_sale_profit,
    )

    # Tie cash sales to the shift's drawer total
    if locked_shift is not None and is_cash_payment(payment_method):
        locked_shift.cash_sales_total += total_sale_amount
        locked_shift.save(update_fields=['cash_sales_total', 'updated_at'])

    return sale


@transaction.atomic
def create_draft_sale(
    shop: Shop,
    branch: Branch,
    items_data: list,
    payment_method: str,
    attendant=None,
    customer_id: str = None,
    customer_name: str = None,
    customer_phone: str = None,
    notes: str = None,
    shift: Shift = None,
):
    """
    Saves a cart as a draft exactly like Firestore's ``addDraftSale``: stock and
    the day summary stay untouched until ``confirm_draft_sale`` runs.
    """
    if not items_data:
        raise ValidationError("Sale must contain at least one item.")

    locked_shift = None
    if shift is not None:
        # A draft may outlive its drawer; an unknown id just drops the link.
        locked_shift = Shift.objects.filter(id=shift.id, shop=shop).first()

    processed_items, total_amount, total_profit, total_discount = _price_items(items_data)
    payment_method = normalize_payment_method(payment_method)

    sale = Sale.objects.create(
        shop=shop,
        branch=branch,
        attendant=attendant,
        shift=locked_shift,
        total_amount=total_amount,
        discount_amount=total_discount,
        profit=total_profit,
        payment_method=payment_method,
        customer_id=customer_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        customer_user=resolve_sale_customer_user(
            customer_id=customer_id, customer_phone=customer_phone),
        notes=notes,
        status='draft'
    )

    SaleItem.objects.bulk_create([
        SaleItem(
            sale=sale,
            product=p_item['product'],
            product_name=p_item['product'].name,
            quantity=p_item['quantity'],
            unit_price=p_item['unit_price'],
            total_price=p_item['total_price'],
            profit=p_item['profit']
        )
        for p_item in processed_items
    ])

    return sale


@transaction.atomic
def confirm_draft_sale(sale: Sale, attendant=None):
    """
    Completes a saved draft the way Firestore's ``confirmDraftSale`` did: stock
    and the day summary move, and the drawer is credited only while its shift is
    still open (legacy confirm never touched the shift at all).
    """
    locked_sale = Sale.objects.select_for_update().get(id=sale.id)
    if locked_sale.status != 'draft':
        raise ValidationError("Sale hii tayari imethibitishwa.")

    items = list(locked_sale.items.select_related('product'))
    if not items:
        raise ValidationError("Draft haina bidhaa yoyote.")

    for item in items:
        if item.product is None:
            raise ValidationError(f"Bidhaa '{item.product_name}' haipo tena.")
        adjust_inventory(
            product=item.product,
            branch=locked_sale.branch,
            movement_type='sale',
            quantity_change=-item.quantity,
            user=attendant,
            reason=f"Confirmation of Draft #{locked_sale.id}"
        )

    apply_daily_summary_delta(
        shop=locked_sale.shop,
        branch=locked_sale.branch,
        date=timezone.localtime(locked_sale.created_at).date(),
        total_sales=locked_sale.total_amount,
        transactions=1,
        profit=locked_sale.profit,
    )

    if locked_sale.shift_id:
        open_shift_row = Shift.objects.select_for_update().filter(
            id=locked_sale.shift_id, status='OPEN').first()
        if open_shift_row is not None and is_cash_payment(locked_sale.payment_method):
            open_shift_row.cash_sales_total += locked_sale.total_amount
            open_shift_row.save(update_fields=['cash_sales_total', 'updated_at'])

    locked_sale.status = 'completed'
    locked_sale.save(update_fields=['status', 'updated_at'])
    return locked_sale

@transaction.atomic
def create_b2b_order(
    shop: Shop,
    branch: Branch,
    items_data: list,
    idempotency_key: str,
    payment_method: str = 'Cash',
    customer_id: str = None,
    customer_name: str = None,
    customer_phone: str = None,
    customer_type: str = None,
    customer_po_number: str = None,
    required_delivery_date=None,
    salesperson_id: str = None,
    internal_notes: str = None,
    notes: str = None,
    fulfillment_details: dict = None,
    approval_status: str = None,
    source: str = 'in_app'
):
    # pyrefly: ignore [missing-import]
    from apps.crm.models import Customer, CustomerInvoice
    # pyrefly: ignore [missing-import]
    from apps.sales.models import Order, OrderItem
    
    # 1. Idempotency Check
    existing_order = Order.objects.filter(idempotency_key=idempotency_key).first()
    if existing_order:
        return existing_order
        
    # 2. Customer Credit Check & AR Prep
    customer = None
    if customer_id:
        # Either spelling the app sends: Firestore customer id (legacy) or uuid.
        value = str(customer_id)
        lookup = Q(id=value) if is_uuid(value) else Q(legacy_id=value)
        customer = Customer.objects.select_for_update().filter(lookup).first()
    
    # 3. Product & Inventory Validation
    total_sale_amount = Decimal('0.00')
    total_sale_profit = Decimal('0.00')
    processed_items = []
    
    for item in items_data:
        product_id = item.get('product_id')
        quantity = int(item.get('quantity', 1))
        unit_price = Decimal(str(item.get('unit_price', '0.00')))
        
        # Product ids travel in the same two spellings as customer ids.
        value = str(product_id or '')
        lookup = Q(id=value) if is_uuid(value) else Q(legacy_id=value)
        product = Product.objects.filter(lookup, shop=shop).first()
        if product is None:
            raise ValidationError(f"Product {product_id} does not exist.")
            
        # Deduct inventory (this handles the select_for_update locking and checking)
        adjust_inventory(
            product=product,
            branch=branch,
            movement_type='sale',
            quantity_change=-quantity,
            reason=f"B2B Order Creation: {idempotency_key}"
        )
        
        total_item_price = unit_price * quantity
        buying_price = product.buying_price or Decimal('0.00')
        item_profit = (unit_price - buying_price) * quantity
        
        total_sale_amount += total_item_price
        total_sale_profit += item_profit
        
        processed_items.append({
            'product': product,
            'quantity': quantity,
            'unit_price': unit_price,
            'subtotal': total_item_price,
        })
        
    # 4. Check Credit Limit
    if customer and payment_method.lower() in ['credit', 'credit / debt']:
        if customer.credit_limit > 0 and (customer.outstanding_balance + total_sale_amount) > customer.credit_limit:
            raise ValidationError(f"Order total ({total_sale_amount}) exceeds customer credit limit.")
    
    # 5. Create Order Record. The fallbacks mirror Firestore ``createOrder``:
    # an absent tier reads ``commercialSettings.priceTier`` first, and orders with
    # no CRM row still carry a name and a pickup fulfilment.
    settings = (customer.commercial_settings or {}) if customer else {}
    customer_name = customer_name or (customer.name if customer else None) or 'Unknown'
    customer_type = customer_type or settings.get('priceTier') or (customer.customer_type if customer else None) or 'retail'
    order = Order.objects.create(
        shop=shop,
        branch=branch,
        idempotency_key=idempotency_key,
        subtotal=total_sale_amount,
        total_amount=total_sale_amount, # Tax/Discount logic can be added later
        profit_estimate=total_sale_profit,
        payment_method=payment_method,
        status='pending',
        approval_status=approval_status,
        source=source,
        customer_id=customer_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        # Portal "my orders" is keyed on this link, exactly like customerUserId.
        customer_user=resolve_sale_customer_user(
            customer_id=customer_id, customer_phone=customer_phone),
        customer_type=customer_type,
        customer_po_number=customer_po_number,
        required_delivery_date=required_delivery_date,
        salesperson_id=salesperson_id,
        internal_notes=internal_notes,
        notes=notes,
        fulfillment_details=fulfillment_details or {'deliveryMethod': 'pickup'}
    )
    
    # 6. Create Order Items
    order_items_to_create = []
    for p_item in processed_items:
        order_items_to_create.append(
            OrderItem(
                order=order,
                product=p_item['product'],
                product_name=p_item['product'].name,
                quantity=p_item['quantity'],
                unit_price=p_item['unit_price'],
                subtotal=p_item['subtotal']
            )
        )
    OrderItem.objects.bulk_create(order_items_to_create)
    
    # 7. Create Customer Invoice & Update Balance
    if customer and payment_method.lower() in ['credit', 'credit / debt']:
        CustomerInvoice.objects.create(
            customer=customer,
            shop=shop,
            order=order,
            amount_due=total_sale_amount,
            amount_paid=0,
            due_date=required_delivery_date,
            status='pending'
        )
        customer.outstanding_balance += total_sale_amount
        customer.save(update_fields=['outstanding_balance'])
        
    return order


def storefront_unit_price(product, quantity: int) -> Decimal:
    """The price a storefront shopper pays: the wholesale tier at or above the MOQ.

    Mirrors ``getItemPrice`` in WishlistCheckoutModal. ``wholesale_price`` has a
    zero default, and a zero-tier price means "no wholesale price set", exactly
    like the client's truthiness check.
    """
    if product.wholesale_price and int(quantity) >= (product.moq or 1):
        return product.wholesale_price
    return product.selling_price


@transaction.atomic
def create_storefront_order(
    shop,
    branch,
    order_id: str,
    items: list,
    customer_name: str,
    customer_phone: str,
    customer_address: str = None,
    notes: str = None,
    payment_method: str = 'Cash / WhatsApp',
    customer_type: str = 'retail',
    customer_user=None,
):
    """A storefront wishlist checkout, previously written straight to Firestore.

    Two deliberate differences from ``create_b2b_order``: prices come from the
    product rows instead of the caller (a public endpoint cannot trust a client
    price), and inventory is **not** touched — Firestore's ``shops/{id}/orders``
    write never moved stock either; the merchant confirms on WhatsApp first.
    """
    # pyrefly: ignore [missing-import]
    from apps.sales.models import Order, OrderItem

    # The client's ORD-###### doubles as the idempotency key, so a double submit
    # returns the order already written instead of a duplicate row.
    existing = Order.objects.filter(idempotency_key=order_id).first()
    if existing is not None:
        if existing.shop_id == shop.pk:
            return existing
        raise ValidationError('That order number has already been used.')

    total_amount = Decimal('0.00')
    profit_estimate = Decimal('0.00')
    lines = []
    for item in items:
        quantity = int(item.get('quantity') or 1)
        value = str(item.get('product_id') or '')
        lookup = Q(id=value) if is_uuid(value) else Q(legacy_id=value)
        product = Product.objects.filter(lookup, shop=shop).first()

        if product is not None:
            unit_price = storefront_unit_price(product, quantity)
            product_name = product.name
            buying_price = product.buying_price or Decimal('0.00')
        else:
            # The product was deleted since it was wishlisted; the order still
            # records the line, as the Firestore write did, but only at the price
            # the client last saw — an existing product is always re-priced above.
            if item.get('price') is None:
                raise ValidationError(f"Product {item.get('product_id')} does not exist.")
            unit_price = Decimal(str(item['price']))
            product_name = item.get('product_name') or 'Item'
            buying_price = Decimal('0.00')

        subtotal = unit_price * quantity
        total_amount += subtotal
        profit_estimate += (unit_price - buying_price) * quantity
        lines.append((product, product_name, quantity, unit_price, subtotal))

    # Firestore's wishlist document carried the address at its top level; Django
    # has no column for it, so it rides in the fulfilment map the Orders dialog reads.
    fulfillment_details = {'deliveryMethod': 'pickup'}
    if customer_address:
        fulfillment_details['deliveryAddress'] = customer_address

    order = Order.objects.create(
        shop=shop,
        branch=branch,
        legacy_id=order_id,
        idempotency_key=order_id,
        subtotal=total_amount,
        total_amount=total_amount,
        profit_estimate=profit_estimate,
        payment_method=payment_method,
        status='pending',
        source='wishlist',
        customer_name=customer_name,
        customer_phone=customer_phone,
        # Portal "my orders" is keyed on this link, exactly like customerUserId.
        customer_user=customer_user or resolve_sale_customer_user(customer_phone=customer_phone),
        customer_type=customer_type,
        notes=notes,
        fulfillment_details=fulfillment_details,
    )

    OrderItem.objects.bulk_create([
        OrderItem(
            order=order,
            product=product,
            product_name=product_name,
            quantity=quantity,
            unit_price=unit_price,
            subtotal=subtotal,
        )
        for product, product_name, quantity, unit_price, subtotal in lines
    ])

    return order


@transaction.atomic
def update_fulfillment_status(
    order,
    status: str,
    fulfillment_data: dict = None,
    items: list = None
):
    """
    Updates the fulfillment status of a B2B order, merges fulfillment details,
    updates item picking quantities, appends timestamps, and handles cancellation rollback.
    """
    from django.utils import timezone
    # pyrefly: ignore [missing-import]
    from apps.products.services import adjust_inventory
    # pyrefly: ignore [missing-import]
    from apps.crm.models import CustomerInvoice
    
    current_status = order.status
    
    # 1. Handle Cancellation Rollback
    if status == 'cancelled' and current_status != 'cancelled':
        # Add stock back
        for item in order.items.all():
            adjust_inventory(
                product=item.product,
                branch=order.branch,
                movement_type='adjustment',
                quantity_change=item.quantity,
                reason=f"Order {order.idempotency_key} Cancelled"
            )
            
        # Optional: Reverse AR if applicable
        if order.payment_method.lower() == 'credit':
            try:
                # Decrease customer outstanding balance
                # pyrefly: ignore [missing-import]
                from apps.crm.models import Customer
                if order.customer_id:
                    customer = Customer.objects.select_for_update().get(id=order.customer_id)
                    customer.outstanding_balance -= order.total_amount
                    customer.save(update_fields=['outstanding_balance'])
                    
                # Cancel the invoice
                invoice = CustomerInvoice.objects.get(order=order)
                invoice.status = 'cancelled'
                invoice.save(update_fields=['status'])
            except (CustomerInvoice.DoesNotExist, Customer.DoesNotExist):
                pass
                
    # 2. Merge Fulfillment Details
    if fulfillment_data:
        current_details = order.fulfillment_details or {}
        current_details.update(fulfillment_data)
        order.fulfillment_details = current_details
        
    # 3. Handle Timestamps Based on Status
    now_iso = timezone.now().isoformat()
    if status == 'allocated':
        order.fulfillment_details['stockCheckedAt'] = now_iso
    elif status == 'out_for_delivery':
        order.fulfillment_details['dispatchedAt'] = now_iso
    elif status == 'in_transit':
        order.fulfillment_details['inTransitAt'] = now_iso
    elif status == 'delivered':
        order.fulfillment_details['deliveredAt'] = now_iso
        
    # 4. Update Items (e.g. during picking). Rows arrive either with the OrderItem
    # id (internal callers) or keyed by product only — PickingDialog sends the latter.
    if items:
        for row in items:
            picked_qty = row.get('picked_qty', 0)
            if row.get('id'):
                targets = order.items.filter(id=row['id'])
            elif row.get('product_id'):
                targets = filter_by_ref(
                    order.items.all(), 'product_id', row['product_id'], Product)
            else:
                continue
            targets.update(picked_qty=picked_qty)
        # List/detail views prefetch ``items``; that cache would otherwise shadow
        # the rows just written when the caller re-serializes the order.
        cache = getattr(order, '_prefetched_objects_cache', None)
        if cache is not None:
            cache.pop('items', None)
                
    order.status = status
    order.save(update_fields=['status', 'fulfillment_details', 'updated_at'])
    
    return order


@transaction.atomic
def pay_order(order, payment_method: str = 'Cash', shift=None):
    """
    Settles a pending order the way Firestore's ``payOrder`` did: the status
    flips to ``paid`` and the day summary, the cashier's drawer total and the
    customer's lifetime stats all move — but no inventory does.
    """
    # pyrefly: ignore [missing-import]
    from apps.crm.models import Customer

    if order.status not in ('pending', 'approved', 'awaiting_shipment'):
        raise ValidationError("Order hii tayari imelipwa au imeghairiwa")

    locked_shift = None
    if shift is not None:
        locked_shift = Shift.objects.select_for_update().filter(
            id=shift.id, shop=order.shop).first()
        if locked_shift is None:
            raise ValidationError("Shift does not belong to this shop.")

    payment_method = payment_method or 'Cash'
    order.status = 'paid'
    order.payment_method = payment_method
    order.paid_at = timezone.now()
    order.save(update_fields=['status', 'payment_method', 'paid_at', 'updated_at'])

    today = timezone.localtime(timezone.now()).date()
    apply_daily_summary_delta(
        shop=order.shop,
        branch=order.branch,
        date=today,
        total_sales=order.total_amount,
        transactions=1,
        profit=order.profit_estimate or Decimal('0.00'),
    )

    if locked_shift is not None and is_cash_payment(payment_method):
        locked_shift.cash_sales_total += order.total_amount
        locked_shift.save(update_fields=['cash_sales_total', 'updated_at'])

    if order.customer_id:
        value = str(order.customer_id)
        lookup = Q(id=value) if is_uuid(value) else Q(legacy_id=value)
        customer = Customer.objects.select_for_update().filter(lookup).first()
        if customer is not None:
            customer.total_spent += order.total_amount
            customer.total_purchases += 1
            customer.save(update_fields=['total_spent', 'total_purchases'])

    return order
