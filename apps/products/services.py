from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError
from .models import Inventory, InventoryMovement, Product, Branch, Category


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
                if barcode:
                    product = Product.objects.filter(shop=shop, barcode=barcode).first()
                if product is None:
                    product = Product.objects.filter(shop=shop, name__iexact=name).first()

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
