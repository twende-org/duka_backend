from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.db.models import F, Q
from rest_framework.exceptions import ValidationError

# pyrefly: ignore [missing-import]
from apps.core.legacy import app_id, resolve_legacy_pk
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.shops.services import ensure_main_branch
# pyrefly: ignore [missing-import]
from apps.products.models import Product, Inventory, InventoryMovement
# pyrefly: ignore [missing-import]
from apps.crm.models import Supplier
# pyrefly: ignore [missing-import]
from apps.purchases.models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseShipment, PurchaseShipmentItem, GRN, GRNItem,
    B2BConnection, B2BSupplierBalance, B2BSupplierInvoice, B2BSupplierPayment,
)

def _decimal(value) -> Decimal:
    """JSON numbers arrive as ints/floats; Decimal rejects floats."""
    return Decimal('0.00') if value in (None, '') else Decimal(str(value))

def _js_number(value: Decimal) -> str:
    """Format a Decimal the way JS interpolated a Number (no trailing zeros)."""
    text = format(value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'

def _resolve_ids(model, values) -> list:
    """App-visible ids (uuid or Firestore legacy id) to pks; unknown values drop out."""
    resolved = []
    for value in values or []:
        pk = resolve_legacy_pk(model, value)
        if pk:
            resolved.append(pk)
    return resolved

def _actor_name(user) -> str:
    """Firestore received the caller's displayName; Django has names + username."""
    if user is None:
        return 'System'
    full_name = ''
    if hasattr(user, 'get_full_name'):
        full_name = (user.get_full_name() or '').strip()
    elif hasattr(user, 'full_name'):
        full_name = str(user.full_name or '').strip()
    if full_name:
        return full_name
    username = ''
    if hasattr(user, 'get_username'):
        username = (user.get_username() or '').strip()
    return username or 'System'

def _supplier_for_shop(buyer_shop: Shop, supplier_shop: Shop):
    """The CRM supplier row mirroring a platform shop, matched by either id spelling.

    ``Supplier.platform_shop_id`` is written from the frontend's ``platformShopId``
    (a Firestore id before the cutover, a Django uuid after), so both are tried.
    Returns None when the buyer never saved a CRM record for this platform shop.
    """
    candidates = {str(supplier_shop.pk)}
    legacy_id = getattr(supplier_shop, 'legacy_id', None)
    if legacy_id:
        candidates.add(str(legacy_id))
    return Supplier.objects.filter(shop=buyer_shop, platform_shop_id__in=candidates).first()

def _bump_b2b_supplier_balance(buyer_shop: Shop, supplier_shop: Shop, balance_delta: Decimal, received_delta: Decimal, purchases_delta: int = 0):
    """Maintain the AP row the legacy ``processGRNTransaction`` kept per buyer/supplier."""
    balance, _ = B2BSupplierBalance.objects.select_for_update().get_or_create(
        buyer_shop=buyer_shop,
        supplier_shop=supplier_shop,
        defaults={'supplier_name': supplier_shop.name},
    )
    balance.total_purchases = F('total_purchases') + purchases_delta
    balance.received_goods_value = F('received_goods_value') + received_delta
    balance.outstanding_balance = F('outstanding_balance') + balance_delta
    if not balance.supplier_name:
        balance.supplier_name = supplier_shop.name
    balance.save()
    return balance

@transaction.atomic
def create_purchase_order(buyer_shop: Shop, supplier_shop: Shop, items_data: list, **kwargs) -> PurchaseOrder:
    if not kwargs.get('supplier_name'):
        kwargs['supplier_name'] = supplier_shop.name
    po = PurchaseOrder.objects.create(
        buyer_shop=buyer_shop,
        supplier_shop=supplier_shop,
        **kwargs
    )
    
    total_amount = Decimal('0.00')
    for item_data in items_data:
        expected_qty = int(item_data['expected_qty'])
        unit_cost = _decimal(item_data['unit_cost'])
        subtotal = expected_qty * unit_cost
        # ``productId`` is the catalog id the app sent: a uuid, a Firestore id, or a
        # free-text ``custom_...`` ref. Link the FK only when it resolves, and keep the
        # echoed value in ``source_product_id`` when it differs from the FK's uuid.
        product_ref = item_data.get('product_id')
        product_pk = resolve_legacy_pk(Product, product_ref) if product_ref else None
        echo_ref = item_data.get('source_product_id')
        if not echo_ref and product_ref and str(product_pk or '') != str(product_ref):
            echo_ref = str(product_ref)
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            product_id=product_pk,
            source_product_id=echo_ref,
            product_name=item_data.get('product_name', 'Unknown Product'),
            expected_qty=expected_qty,
            unit_cost=unit_cost,
            subtotal=subtotal
        )
        total_amount += subtotal
    
    po.subtotal = total_amount
    po.total_amount = total_amount + po.tax_amount + po.shipping_cost - po.discount_amount
    # The legacy createB2BPurchaseOrder opened the timeline with the initial status.
    po.timeline = [{
        'status': po.status,
        'description': f"Purchase order created with status: {po.status}",
        'timestamp': timezone.now().isoformat(),
    }]
    po.save()
    
    return po

@transaction.atomic
def process_grn(purchase_order_id, buyer_shop: Shop, supplier=None, items_data: list = None, shipment_id=None, notes: str = '', user=None, supplier_shop: Shop = None) -> GRN:
    """
    Process a Goods Receipt Note. This atomically:
    1. Creates GRN and GRNItems.
    2. Updates PO and PO Items received quantities.
    3. Increases Buyer's Inventory via InventoryMovement.
    4. Increases Supplier's outstanding balance (CRM row when one exists, and the
       B2B AP balance the Accounts Payable pages read).
    5. Advances the PO/shipment timelines the same way the legacy transaction did.

    ``supplier`` is the CRM row (used by older callers/tests); B2B callers pass
    ``supplier_shop`` — the platform shop the wizard sent as ``supplierId``.
    """
    items_data = items_data or []
    
    # 1. Lock the PurchaseOrder and Supplier
    po = PurchaseOrder.objects.select_for_update().get(id=purchase_order_id, buyer_shop=buyer_shop)
    
    resolved_supplier_shop = supplier_shop or po.supplier_shop
    supplier_record = None
    if supplier is not None:
        supplier_record = Supplier.objects.filter(id=supplier.id, shop=buyer_shop).first()
    else:
        supplier_record = _supplier_for_shop(buyer_shop, resolved_supplier_shop)
    if supplier_record is not None:
        supplier_record = Supplier.objects.select_for_update().get(id=supplier_record.id)
    
    shipment = None
    if shipment_id:
        shipment = PurchaseShipment.objects.select_for_update().get(id=shipment_id, purchase_order=po)
        if shipment.status == 'delivered':
            # Legacy processGRNTransaction threw exactly this string at the wizard.
            raise ValidationError('This shipment has already been received and processed.')
        
    # 2. Create GRN
    grn = GRN.objects.create(
        purchase_order=po,
        shipment=shipment,
        shop=buyer_shop,
        supplier=resolved_supplier_shop,
        status='completed',
        notes=notes,
        completed_at=timezone.now()
    )
    
    # Shops imported from Firestore may have no branch yet; ensure_main_branch creates one.
    default_branch = ensure_main_branch(buyer_shop)
        
    grn_total_value = Decimal('0.00')
    
    # 3. Process Items
    for item_data in items_data:
        product_ref = item_data.get('product_id')
        product_pk = resolve_legacy_pk(Product, product_ref) if product_ref else None
        accepted_qty = int(item_data.get('accepted_qty', 0))
        unit_cost = _decimal(item_data.get('unit_cost', '0.00'))
        
        if accepted_qty > 0:
            value = accepted_qty * unit_cost
            grn_total_value += value
            
            # Update PO Item: by FK when the ref resolves, else by the echoed raw ref.
            po_item = None
            if product_pk:
                po_item = PurchaseOrderItem.objects.filter(purchase_order=po, product_id=product_pk).first()
            if po_item is None and product_ref:
                po_item = PurchaseOrderItem.objects.filter(purchase_order=po, source_product_id=str(product_ref)).first()
            if po_item:
                po_item.received_qty = F('received_qty') + accepted_qty
                po_item.save()
                po_item.refresh_from_db()
            
            # Create GRN Item
            GRNItem.objects.create(
                grn=grn,
                product_id=product_pk,
                product_name=item_data.get('product_name', po_item.product_name if po_item else 'Unknown'),
                expected_qty=item_data.get('expected_qty', 0),
                accepted_qty=accepted_qty,
                damaged_qty=item_data.get('damaged_qty', 0),
                missing_qty=item_data.get('missing_qty', 0),
                rejected_qty=item_data.get('rejected_qty', 0),
                unit_cost=unit_cost
            )
            
            if product_pk:
                # Update Inventory safely
                inventory, created = Inventory.objects.select_for_update().get_or_create(
                    branch=default_branch,
                    product_id=product_pk,
                    defaults={'quantity': 0}
                )
                
                previous_qty = inventory.quantity
                new_qty = previous_qty + accepted_qty
                
                inventory.quantity = new_qty
                inventory.save()
                
                # Log Movement
                InventoryMovement.objects.create(
                    product_id=product_pk,
                    branch=default_branch,
                    movement_type='in',
                    quantity_changed=accepted_qty,
                    previous_qty=previous_qty,
                    new_qty=new_qty,
                    reason=f"GRN Received from PO-{app_id(po)[:6]}",
                    user=user
                )
    
    # 4. Update Supplier Balance
    if supplier_record is not None:
        supplier_record.outstanding_balance = F('outstanding_balance') + grn_total_value
        supplier_record.total_spent = F('total_spent') + grn_total_value
        supplier_record.total_purchases = F('total_purchases') + 1
        supplier_record.save()
    
    _bump_b2b_supplier_balance(
        buyer_shop, resolved_supplier_shop,
        balance_delta=grn_total_value,
        received_delta=grn_total_value,
        purchases_delta=1,
    )
    
    # 5. Update PO Status
    # Check if all items are fully received
    all_fully_received = True
    any_received = False
    
    po.refresh_from_db() # Refresh to get updated F() expressions
    for item in po.items.all():
        if item.received_qty >= item.expected_qty:
            any_received = True
        elif item.received_qty > 0:
            any_received = True
            all_fully_received = False
        else:
            all_fully_received = False
            
    if all_fully_received:
        po.status = 'completed'
    elif any_received:
        po.status = 'partially_received'
    
    # Timeline events mirror the legacy transaction (grn_processed + final status).
    actor = _actor_name(user)
    po_timeline = po.timeline or []
    po_timeline.append({
        'status': 'grn_processed',
        'description': f"Goods Received Note processed by {actor}",
        'timestamp': timezone.now().isoformat(),
    })
    po_timeline.append({
        'status': po.status,
        'description': f"Order {'completed' if all_fully_received else 'partially received'}",
        'timestamp': timezone.now().isoformat(),
    })
    po.timeline = po_timeline
    po.save()
    
    # 6. Update Shipment status if provided
    if shipment:
        shipment.status = 'delivered'
        shipment_timeline = shipment.timeline or []
        shipment_timeline.append({
            'status': 'grn_processed',
            'description': f"Goods Received Note processed by {actor}",
            'timestamp': timezone.now().isoformat(),
        })
        shipment.timeline = shipment_timeline
        shipment.save()
        
    return grn

@transaction.atomic
def create_purchase_shipment(po_id: str, supplier_shop: Shop, items_data: list, **kwargs) -> PurchaseShipment:
    """
    Creates a B2B Shipment from a Purchase Order.
    Validates the PO belongs to the supplier, generates the Shipment, creates Shipment Items,
    and advances the PO status.
    """
    po = PurchaseOrder.objects.select_for_update().get(id=po_id, supplier_shop=supplier_shop)
    
    shipment = PurchaseShipment.objects.create(
        purchase_order=po,
        **kwargs
    )
    
    for item_data in items_data:
        PurchaseShipmentItem.objects.create(
            shipment=shipment,
            product_id=item_data.get('product_id'),
            source_product_id=item_data.get('source_product_id'),
            product_name=item_data.get('product_name', 'Unknown'),
            shipped_qty=int(item_data.get('shipped_qty', 0))
        )
        
    # Add timeline event to the shipment
    shipment_timeline = shipment.timeline or []
    shipment_timeline.append({
        'status': shipment.status,
        'description': f"Shipment created with status: {shipment.status}",
        'timestamp': timezone.now().isoformat()
    })
    shipment.timeline = shipment_timeline
    shipment.save()
    
    # Update PO Status and timeline
    po_timeline = po.timeline or []
    po_timeline.append({
        'status': 'shipment_created',
        'description': 'Shipment created',
        'timestamp': timezone.now().isoformat()
    })
    po.timeline = po_timeline
    
    # If not already completed or partially received, update status
    if po.status not in ['completed', 'partially_received']:
        po.status = 'awaiting_shipment'
    
    po.save()
    return shipment

@transaction.atomic
def update_b2b_order_status(po_id, status: str, notes: str = None) -> PurchaseOrder:
    """Legacy ``updateB2BOrderStatus``: append a status event, optionally rewrite the notes."""
    po = PurchaseOrder.objects.select_for_update().get(id=po_id)
    po.status = status
    # Legacy distinguished undefined from empty string: '' still overwrote notes.
    if notes is not None:
        po.notes = notes
    
    timeline = po.timeline or []
    timeline.append({
        'status': status,
        'description': f"Order status updated to {status}{f' - {notes}' if notes else ''}",
        'timestamp': timezone.now().isoformat(),
    })
    po.timeline = timeline
    po.save()
    return po

@transaction.atomic
def update_b2b_shipment_status(shipment_id, status: str, notes: str = None) -> PurchaseShipment:
    """Legacy ``updateB2BShipmentStatus``: append an event and optionally rewrite supplierNotes."""
    shipment = PurchaseShipment.objects.select_for_update().get(id=shipment_id)
    shipment.status = status
    # Legacy distinguished undefined from empty string: '' still overwrote supplierNotes.
    if notes is not None:
        shipment.supplier_notes = notes
    timeline = shipment.timeline or []
    timeline.append({
        'status': status,
        'description': f"Shipment status updated to {status}{f' - {notes}' if notes else ''}",
        'timestamp': timezone.now().isoformat(),
    })
    shipment.timeline = timeline
    shipment.save()
    return shipment

@transaction.atomic
def request_b2b_connection(buyer_shop: Shop, supplier_shop: Shop) -> B2BConnection:
    """Legacy ``requestB2BConnection`` returned the existing link instead of duplicating it."""
    existing = B2BConnection.objects.filter(buyer_shop=buyer_shop, supplier_shop=supplier_shop).first()
    if existing is not None:
        return existing
    return B2BConnection.objects.create(
        buyer_shop=buyer_shop,
        supplier_shop=supplier_shop,
        status='pending',
    )

def approve_b2b_connection(connection: B2BConnection, pricing_tier: str = 'wholesale') -> B2BConnection:
    """Legacy ``approveB2BConnection`` set status + tier in one update."""
    connection.status = 'approved'
    connection.pricing_tier = pricing_tier or 'wholesale'
    connection.save()
    return connection

@transaction.atomic
def create_b2b_supplier_invoice(
    buyer_shop: Shop,
    supplier_shop: Shop,
    purchase_order=None,
    grn_ids=None,
    invoice_number: str = '',
    invoice_date=None,
    due_date=None,
    currency: str = 'TZS',
    subtotal=0,
    tax_amount=0,
    discount_amount=0,
    total_amount=0,
    attachment_url: str = None,
    status: str = 'draft',
) -> B2BSupplierInvoice:
    """Legacy ``createB2BSupplierInvoice`` wrote the document only; balances move on approval."""
    return B2BSupplierInvoice.objects.create(
        buyer_shop=buyer_shop,
        supplier_shop=supplier_shop,
        purchase_order=purchase_order,
        grn_ids=[str(value) for value in (grn_ids or [])],
        invoice_number=invoice_number or '',
        invoice_date=invoice_date or None,
        due_date=due_date or None,
        currency=currency or 'TZS',
        subtotal=_decimal(subtotal),
        tax_amount=_decimal(tax_amount),
        discount_amount=_decimal(discount_amount),
        total_amount=_decimal(total_amount),
        attachment_url=attachment_url or None,
        status=status or 'draft',
    )

@transaction.atomic
def update_b2b_supplier_invoice_status(invoice: B2BSupplierInvoice, status: str) -> B2BSupplierInvoice:
    """
    Legacy ``updateB2BSupplierInvoiceStatus``: only the ``under_review`` -> ``approved``
    step touched balances — when the invoice total differs from the value of the GRNs
    it covers, the difference lands on the buyer's AP balance (if one exists yet).
    """
    invoice = B2BSupplierInvoice.objects.select_for_update().get(id=invoice.id)
    if invoice.status == 'under_review' and status == 'approved':
        grn_total = Decimal('0.00')
        # Python loop (not SQL Sum) so Integer accepted_qty * Decimal unit_cost stays exact.
        for item in GRNItem.objects.filter(grn_id__in=_resolve_ids(GRN, invoice.grn_ids)):
            grn_total += Decimal(item.accepted_qty) * item.unit_cost
        adjustment = invoice.total_amount - grn_total
        if abs(adjustment) > Decimal('0.01'):
            balance = B2BSupplierBalance.objects.select_for_update().filter(
                buyer_shop=invoice.buyer_shop,
                supplier_shop=invoice.supplier_shop,
            ).first()
            if balance is not None:
                balance.outstanding_balance = F('outstanding_balance') + adjustment
                balance.received_goods_value = F('received_goods_value') + adjustment
                balance.save()
    invoice.status = status
    invoice.save()
    return invoice

@transaction.atomic
def process_b2b_supplier_payment(
    buyer_shop: Shop,
    supplier_shop: Shop,
    amount,
    method: str = 'Cash',
    reference: str = '',
    invoice_ids=None,
    date=None,
    notes: str = '',
    allow_overpayment: bool = False,
) -> B2BSupplierPayment:
    """Legacy ``processSupplierPayment``: draws down the AP balance and settles the listed invoices."""
    amount = _decimal(amount)
    balance = B2BSupplierBalance.objects.select_for_update().filter(
        buyer_shop=buyer_shop,
        supplier_shop=supplier_shop,
    ).first()
    if balance is None:
        raise ValidationError('Supplier balance record not found. Cannot process payment.')
    if not allow_overpayment and amount > balance.outstanding_balance:
        # Plain string so the toast shows exactly the legacy message.
        raise ValidationError(
            f"Payment amount ({_js_number(amount)}) exceeds outstanding balance "
            f"({_js_number(balance.outstanding_balance)}). Please confirm overpayment."
        )
    
    payment = B2BSupplierPayment.objects.create(
        buyer_shop=buyer_shop,
        supplier_shop=supplier_shop,
        amount=amount,
        method=method or 'Cash',
        reference=reference or None,
        invoice_ids=[str(value) for value in (invoice_ids or [])],
        date=date or timezone.now(),
        notes=notes or None,
    )
    
    balance.outstanding_balance = F('outstanding_balance') - amount
    balance.paid_amount = F('paid_amount') + amount
    balance.save()
    
    for invoice_pk in _resolve_ids(B2BSupplierInvoice, invoice_ids):
        B2BSupplierInvoice.objects.filter(id=invoice_pk).update(status='paid')
    
    return payment
