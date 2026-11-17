from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError
from .models import Inventory, InventoryMovement, MasterProduct, Product, Branch, Category, InventorySerialItem


def _import_decimal(value):
    """Coerce an Excel cell ('1,500', 1500, '1500.00') into a Decimal, or None."""
    if value is None or value == '':
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"'{value}' is not a valid number.")


def _import_quantity(value):
    """Coerce an Excel stock cell into a non-negative whole number, or None."""
    if value is None or value == '':
        return None
    try:
        quantity = Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"'{value}' is not a valid quantity.")
    if quantity != quantity.to_integral_value():
        raise ValidationError(f"Quantity '{value}' must be a whole number.")
    quantity = int(quantity)
    if quantity < 0:
        raise ValidationError("Quantity cannot be negative.")
    return quantity

def adjust_inventory(product, branch, movement_type, quantity_change, user=None, reason=""):
    """
    Atomically adjusts the inventory quantity and creates a movement ledger record.
    
    :param product: Product instance
    :param branch: Branch instance
    :param movement_type: string ('in', 'out', 'sale', 'transfer', 'adjustment')
    :param quantity_change: int (delta, e.g., 5 for 'in', -2 for 'out')
    :param user: User instance making the change
    :param reason: string reason for the change
    """
    with transaction.atomic():
        # Lock the inventory row to prevent race conditions
        inventory, created = Inventory.objects.select_for_update().get_or_create(
            product=product,
            branch=branch,
            defaults={'quantity': 0, 'allocated_qty': 0}
        )
        
        previous_qty = inventory.quantity
        new_qty = previous_qty + quantity_change
        
        if new_qty < 0:
            raise ValidationError(f"Insufficient stock for product {product.name}. Current: {previous_qty}, Requested: {-quantity_change}")
            
        inventory.quantity = new_qty
        inventory.save(update_fields=['quantity', 'updated_at'])
        
        # Create immutable ledger record
        movement = InventoryMovement.objects.create(
            product=product,
            branch=branch,
            movement_type=movement_type,
            quantity_changed=quantity_change,
            previous_qty=previous_qty,
            new_qty=new_qty,
            reason=reason,
            user=user
        )
        
        return inventory, movement

def process_stock_transfer(transfer, user=None):
    """
    Atomically processes a stock transfer by moving inventory from the source branch
    to the destination branch. Marks the transfer as completed.
    
    :param transfer: StockTransfer instance in 'pending' status
    :param user: User instance processing the transfer
    """
    from django.utils import timezone
    
    if transfer.status != 'pending':
        raise ValidationError(f"Cannot process transfer {transfer.id} with status {transfer.status}.")
        
    with transaction.atomic():
        # Deduct from source branch
        adjust_inventory(
            product=transfer.product,
            branch=transfer.from_branch,
            movement_type='transfer',
            quantity_change=-transfer.quantity,
            user=user,
            reason=f"Stock transfer {transfer.id} to {transfer.to_branch.name}"
        )
        
        # Add to destination branch
        adjust_inventory(
            product=transfer.product,
            branch=transfer.to_branch,
            movement_type='transfer',
            quantity_change=transfer.quantity,
            user=user,
            reason=f"Stock transfer {transfer.id} from {transfer.from_branch.name}"
        )
        
        transfer.status = 'completed'
        transfer.completed_at = timezone.now()
        transfer.save(update_fields=['status', 'completed_at', 'updated_at'])
        
        return transfer


def bulk_import_products(shop, rows, user=None, branch=None):
    """
    Apply an Excel-import batch of product rows to the live tables.

    Each row matches an existing product of the shop — barcode first, then a
    case-insensitive name match, mirroring the intake apply — and refreshes the
    fields the row carries (empty cells leave existing values untouched);
    unmatched rows become new products. A numeric ``quantity`` is treated as
    the counted stock and enters through :func:`adjust_inventory`, so every
    change lands in the movement ledger (reason ``Excel import``).

    Rows are processed independently (one transaction each): a bad row is
    reported and skipped without rolling back the rest.

    :param shop: Shop instance the import belongs to
    :param rows: list of dicts with optional name/barcode/sku/category/unit/
                 buyingPrice/sellingPrice/quantity keys (camelCase Excel headers)
    :param user: User performing the import
    :param branch: Branch to stock; defaults to the shop's main (or first) branch
    :returns: (summary dict, per-row result list)
    """
    if branch is None:
        branch = (
            Branch.objects.filter(shop=shop, is_main=True).first()
            or Branch.objects.filter(shop=shop).order_by('created_at').first()
        )
    if branch is None:
        raise ValidationError('This shop has no branch to stock; create one first.')

    from apps.core.legacy import app_id

    results = []
    created = updated = errors = stock_changes = 0

    for index, row in enumerate(rows, start=1):
        name = str(row.get('name') or '').strip()
        result = {'row': index, 'name': name, 'status': 'error'}
        if not name:
            result['message'] = 'Name is required.'
            results.append(result)
            errors += 1
            continue

        try:
            with transaction.atomic():
                buying_price = _import_decimal(row.get('buyingPrice'))
                selling_price = _import_decimal(row.get('sellingPrice'))
                quantity = _import_quantity(row.get('quantity'))
                barcode = str(row.get('barcode') or '').strip()
                sku = str(row.get('sku') or '').strip()
                unit = str(row.get('unit') or '').strip()
                category_name = str(row.get('category') or '').strip()

                product = None
                # Matching skips soft-deleted rows, as a Firestore import did:
                # a deleted barcode re-imports as a fresh product, it does not
                # silently restock a row the merchant removed from the shelves.
                if barcode:
                    product = Product.objects.filter(shop=shop, barcode=barcode, is_active=True).first()
                if product is None:
                    product = Product.objects.filter(shop=shop, name__iexact=name, is_active=True).first()

                category = None
                if category_name:
                    category, _ = Category.objects.get_or_create(
                        shop=shop, name__iexact=category_name[:255],
                        defaults={'name': category_name[:255]})

                if product is None:
                    product = Product.objects.create(
                        shop=shop,
                        branch=branch,
                        category=category,
                        name=name[:255],
                        unit=unit or 'pcs',
                        barcode=barcode,
                        sku=sku,
                        buying_price=buying_price if buying_price is not None else Decimal('0'),
                        selling_price=selling_price if selling_price is not None else Decimal('0'),
                    )
                    result['status'] = 'created'
                    created += 1
                else:
                    changed = []
                    if buying_price is not None:
                        product.buying_price = buying_price
                        changed.append('buying_price')
                    if selling_price is not None:
                        product.selling_price = selling_price
                        changed.append('selling_price')
                    if unit:
                        product.unit = unit
                        changed.append('unit')
                    if barcode:
                        product.barcode = barcode
                        changed.append('barcode')
                    if sku:
                        product.sku = sku
                        changed.append('sku')
                    if category and product.category_id is None:
                        product.category = category
                        changed.append('category')
                    if changed:
                        product.save(update_fields=changed + ['updated_at'])
                    result['status'] = 'updated'
                    updated += 1

                if quantity is not None:
                    inventory = Inventory.objects.filter(product=product, branch=branch).first()
                    current = inventory.quantity if inventory else 0
                    delta = quantity - current
                    if delta != 0:
                        adjust_inventory(
                            product,
                            branch,
                            'in' if delta > 0 else 'adjustment',
                            delta,
                            user=user,
                            reason='Excel import',
                        )
                        stock_changes += 1

                result['productId'] = app_id(product)
                results.append(result)
        except (ValidationError, IntegrityError) as exc:
            result['message'] = '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc)
            results.append(result)
            errors += 1

    summary = {
        'total': len(rows),
        'created': created,
        'updated': updated,
        'errors': errors,
        'stockChanges': stock_changes,
    }
    return summary, results


# ---------------------------------------------------------------------------
# Shared master catalogue (Phase 1: tap-to-add) and per-unit IMEI tracking
# ---------------------------------------------------------------------------

TAP_TO_ADD_REASON = 'Tap-to-Add Catalog Onboarding'
SERIAL_REGISTRATION_REASON = 'Serial/IMEI registration'


def _catalog_branch(shop, branch=None):
    """Branch catalogue stock lands on: the caller's, else main, else the oldest.

    Same rule ``bulk_import_products`` and the intake apply already use, so a
    Firestore-imported shop with no branch rows behaves identically here. A
    branch is checked against the shop before anything is written: ``branchId``
    arrives from the client, and stocking another shop's branch would be a
    cross-tenant write the app-level scoping elsewhere would never catch.
    """
    if branch is not None:
        if branch.shop_id != shop.id:
            raise ValidationError('This branch does not belong to that shop.')
        return branch
    return (
        Branch.objects.filter(shop=shop, is_main=True).first()
        or Branch.objects.filter(shop=shop).order_by('created_at').first()
    )


def clean_imeis(raw):
    """Trim and de-duplicate one item's IMEI list, keeping input order.

    Also used by the POS checkout, where the cashier's scan list arrives the same
    way and must collapse the same way before it is counted against a line.
    """
    seen = []
    for value in raw or []:
        imei = str(value or '').strip()
        if imei and imei not in seen:
            seen.append(imei)
    return seen


def _reject_taken_imeis(shop, imeis):
    """Raise when any IMEI is already sitting on this shop's shelf.

    One query for the whole batch: the DB constraint would also catch these, but
    it reports the last row written, not the numbers the merchant should fix.
    """
    if not imeis:
        return
    taken = sorted(set(
        InventorySerialItem.objects.filter(
            shop=shop, status='in_stock', imei_number__in=imeis
        ).values_list('imei_number', flat=True)
    ))
    if taken:
        raise ValidationError(
            f"Already in stock in this shop: {', '.join(taken)}."
        )


def bulk_tap_to_add_products(shop, user, branch, items_payload):
    """Add catalogue rows to one shop in a single tap (spec Phase 1).

    Each payload item names a :class:`MasterProduct` and this shop's own price
    and opening stock for it. The catalogue row supplies identity only —
    name, brand, barcode, warranty, image, ``requires_imei`` — and
    ``category_name`` resolves into (or creates) the shop's own per-shop
    :class:`Category`, exactly as the intake apply step does.

    Idempotent on ``(shop, master_product)``: a second tap on the same catalogue
    row refreshes the prices it was sent and stocks the existing product, rather
    than creating a duplicate. Name, brand and barcode are never re-copied after
    the first add — the merchant owns that row from then on.

    Stock enters through :func:`adjust_inventory`, so every unit is in the
    movement ledger. When a line carries IMEIs they are the authoritative count:
    ``quantity`` defaults to how many were scanned, and sending both must agree,
    otherwise ``Inventory`` and the serial rows would disagree from day one.

    Whole batch is one transaction: a bad line rolls back every other line, so
    the merchant sees a clean 400 instead of half a catalogue.

    :param shop: Shop receiving the products
    :param user: User performing the add (stamped on the ledger)
    :param branch: Branch to stock; defaults to the shop's main (or first) branch
    :param items_payload: list of validated dicts with ``master_product`` and
        optional ``buying_price`` / ``selling_price`` / ``wholesale_price`` /
        ``quantity`` / ``publish_to_directory`` / ``imei_numbers``
    :returns: the touched :class:`Product` instances, in payload order
    """
    branch = _catalog_branch(shop, branch)
    if branch is None:
        raise ValidationError('This shop has no branch to stock; create one first.')
    if not items_payload:
        raise ValidationError('Select at least one catalog product to add.')

    # --- pre-flight: validate the whole batch before writing any of it ---
    picked = []
    seen_masters = set()
    all_imeis = []
    for index, item in enumerate(items_payload, start=1):
        master = item['master_product']
        if master.pk in seen_masters:
            raise ValidationError(f'Item {index}: "{master.name}" was selected more than once.')
        seen_masters.add(master.pk)
        if not master.is_active:
            raise ValidationError(f'Item {index}: "{master.name}" is no longer available in the catalog.')

        imeis = clean_imeis(item.get('imei_numbers'))
        item['imei_numbers'] = imeis
        quantity = int(item.get('quantity') or 0)
        if quantity < 0:
            raise ValidationError(f'Item {index}: quantity cannot be negative.')
        if imeis and not quantity:
            # A scanned unit is a unit of stock: with no count given, the scan
            # is the count, so Inventory and the serial rows cannot drift apart.
            quantity = len(imeis)
        elif imeis and quantity != len(imeis):
            raise ValidationError(
                f'Item {index}: quantity {quantity} does not match the {len(imeis)} IMEI numbers sent. '
                f'One scanned device is one unit of stock.'
            )
        item['quantity'] = quantity
        all_imeis.extend(imeis)
        picked.append((index, master, item))

    duplicates = {i for i in all_imeis if all_imeis.count(i) > 1}
    if duplicates:
        raise ValidationError(
            f'The same IMEI cannot be added twice in one batch: {", ".join(sorted(duplicates))}.'
        )
    _reject_taken_imeis(shop, all_imeis)

    # --- write ---
    products = []
    with transaction.atomic():
        for _index, master, item in picked:
            quantity = item['quantity']
            imeis = item['imei_numbers']
            category = None
            if master.category_name.strip():
                category, _ = Category.objects.get_or_create(
                    shop=shop, name__iexact=master.category_name.strip()[:255],
                    defaults={'name': master.category_name.strip()[:255]})

            # The idempotency match only revives a product the merchant still
            # carries; after a soft delete a second tap starts a fresh row,
            # exactly as tapping after a Firestore delete did.
            product = Product.objects.filter(shop=shop, master_product=master, is_active=True).first()
            if product is None:
                product = Product.objects.create(
                    shop=shop,
                    master_product=master,
                    category=category,
                    branch=branch,
                    name=master.name[:255],
                    brand=master.brand[:100],
                    barcode=master.barcode[:100],
                    warranty=master.default_warranty[:255],
                    image_url=master.image_url,
                    requires_imei=master.requires_imei,
                    unit='pcs',
                    buying_price=item.get('buying_price') or Decimal('0'),
                    selling_price=item.get('selling_price') or Decimal('0'),
                    wholesale_price=item.get('wholesale_price') or Decimal('0'),
                    publish_to_directory=bool(item.get('publish_to_directory') or False),
                )
            else:
                # Refresh only what the tap actually carried: blank prices are
                # "leave the merchant's margin alone", not "zero it".
                changed = []
                for key, field in (
                    ('buying_price', 'buying_price'),
                    ('selling_price', 'selling_price'),
                    ('wholesale_price', 'wholesale_price'),
                ):
                    value = item.get(key)
                    if value is not None:
                        setattr(product, field, value)
                        changed.append(field)
                if item.get('publish_to_directory') is not None:
                    product.publish_to_directory = bool(item['publish_to_directory'])
                    changed.append('publish_to_directory')
                if category and product.category_id is None:
                    product.category = category
                    changed.append('category')
                if changed:
                    product.save(update_fields=changed + ['updated_at'])

            if quantity:
                adjust_inventory(
                    product, branch, 'in', quantity, user=user,
                    reason=TAP_TO_ADD_REASON,
                )
            if imeis:
                InventorySerialItem.objects.bulk_create([
                    InventorySerialItem(
                        shop=shop, product=product, branch=branch,
                        imei_number=imei[:100], status='in_stock',
                    )
                    for imei in imeis
                ])
            products.append(product)

    return products


def register_serial_items(shop, product, branch, user, serial_list, add_to_stock=True):
    """Record IMEI/serial units against an existing shop product.

    The Phase 1 companion to :func:`bulk_tap_to_add_products` for devices that
    arrive after the product row exists (a wholesaler delivery, a trade-in).
    Every accepted unit is ``in_stock`` and — unless ``add_to_stock`` is false —
    raises ``Inventory`` by the same count through :func:`adjust_inventory`, so
    the shelf count and the unit count cannot drift apart.

    Already-shelved IMEIs for this shop are rejected by number, and duplicates
    inside the payload are rejected before anything is written.

    :param serial_list: list of dicts with ``imei_number`` and optional
        ``serial_number`` / ``notes``
    :returns: the created :class:`InventorySerialItem` rows
    """
    if product.shop_id != shop.id:
        raise ValidationError('This product does not belong to that shop.')
    if not serial_list:
        raise ValidationError('No serial numbers were provided.')

    branch = _catalog_branch(shop, branch)
    if branch is None:
        raise ValidationError('This shop has no branch to stock; create one first.')

    entries = []
    seen = set()
    for entry in serial_list:
        imei = str(entry.get('imei_number') or '').strip()
        serial = str(entry.get('serial_number') or '').strip()
        if not imei and not serial:
            raise ValidationError('Each unit needs an IMEI number or a serial number.')
        if imei and imei in seen:
            raise ValidationError(f'IMEI {imei} was sent more than once.')
        seen.add(imei)
        entries.append({
            'imei_number': imei[:100],
            'serial_number': serial[:100],
            'notes': str(entry.get('notes') or ''),
        })

    _reject_taken_imeis(shop, [e['imei_number'] for e in entries if e['imei_number']])

    with transaction.atomic():
        created = InventorySerialItem.objects.bulk_create([
            InventorySerialItem(
                shop=shop, product=product, branch=branch,
                imei_number=e['imei_number'], serial_number=e['serial_number'],
                notes=e['notes'], status='in_stock',
            )
            for e in entries
        ])
        if add_to_stock:
            adjust_inventory(
                product, branch, 'in', len(created), user=user,
                reason=SERIAL_REGISTRATION_REASON,
            )
    return created
