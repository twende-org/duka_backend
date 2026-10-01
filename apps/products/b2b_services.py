"""Inter-shop (B2B) stock transfer lifecycle — spec Part 2.

Sender stock leaves at dispatch (creation); the buyer's copy stays quarantined
in the ``pending`` transfer until they confirm. Completion and cancellation run
inside ``transaction.atomic()`` with ``select_for_update()`` on the transfer
row, so a double-tap on confirm (or simultaneous confirm + cancel) cannot move
stock twice — the first writer flips the status, the second sees it gone.
"""
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.products.models import B2BStockTransfer, B2BStockTransferItem, Branch, Product
from apps.products.services import adjust_inventory


def create_b2b_transfer(from_shop, to_shop, from_branch, items, created_by=None,
                        to_branch=None, note='', reference=''):
    """Deduct sender stock and stage the manifest as a ``pending`` transfer.

    ``items`` is a list of ``{'product': Product, 'quantity': int, 'unit_cost': Decimal}``.
    Insufficient stock on any line aborts the whole dispatch (the deduction is
    part of the same transaction).
    """
    if from_shop.pk == to_shop.pk:
        raise ValidationError('Use the in-shop stock transfer for moves inside one shop.')
    if not items:
        raise ValidationError('Add at least one item to the transfer.')

    with transaction.atomic():
        transfer = B2BStockTransfer.objects.create(
            from_shop=from_shop,
            to_shop=to_shop,
            from_branch=from_branch,
            to_branch=to_branch,
            created_by=created_by,
            note=note or '',
            reference=reference or '',
        )
        rows = []
        for item in items:
            product = item['product']
            try:
                quantity = int(item['quantity'])
            except (TypeError, ValueError):
                raise ValidationError({'quantity': ['Quantities must be whole numbers.']})
            if quantity <= 0:
                raise ValidationError({'quantity': ['Quantities must be greater than zero.']})
            adjust_inventory(
                product, from_branch, 'transfer', -quantity, user=created_by,
                reason=f'B2B transfer to {to_shop.name}',
            )
            rows.append(B2BStockTransferItem(
                transfer=transfer,
                product=product,
                product_name=product.name[:255],
                sku=(product.sku or '')[:100],
                barcode=(product.barcode or '')[:100],
                unit=(product.unit or 'pcs')[:100],
                quantity=quantity,
                unit_cost=item.get('unit_cost') or product.buying_price,
            ))
        B2BStockTransferItem.objects.bulk_create(rows)
        return transfer


def map_b2b_transfer_items(transfer, mappings, user):
    """Buyer maps each manifest line to one of their own products (or clears it).

    ``mappings`` is a list of ``{'item': B2BStockTransferItem, 'product': Product|None}``.
    Only allowed while pending; every product must belong to the receiving shop.
    Completion is refused until every line is mapped, so a partial map is fine
    mid-review but never silently completes.
    """
    with transaction.atomic():
        transfer = (
            B2BStockTransfer.objects.select_for_update()
            .select_related('to_shop').get(id=transfer.id)
        )
        if transfer.status != 'pending':
            raise ValidationError('Only pending transfers can be mapped.')

        item_ids = [entry['item'].id for entry in mappings]
        known = {
            item.id: item
            for item in transfer.items.filter(id__in=item_ids)
        }
        unknown = set(item_ids) - set(known)
        if unknown:
            raise ValidationError('One of the transfer lines does not belong to this transfer.')

        updates = []
        for entry in mappings:
            item = known[entry['item'].id]
            product = entry.get('product')
            if product is not None:
                if product.shop_id != transfer.to_shop_id:
                    raise ValidationError(
                        f"'{item.product_name}' must map to a product in your shop."
                    )
            if item.mapped_product_id != (product.pk if product else None):
                item.mapped_product = product
                updates.append(item)
        if updates:
            B2BStockTransferItem.objects.bulk_update(updates, ['mapped_product', 'updated_at'])
        return transfer


def complete_b2b_transfer(transfer, user):
    """Buyer confirms: move quarantined quantities into their mapped products.

    Every line must be mapped first (see :func:`map_b2b_transfer_items`) — the
    buyer's account is never auto-populated. Each mapped product only inherits
    the transfer's cost when it has none yet (buying_price 0); the quantity
    enters through :func:`adjust_inventory`, landing in the movement ledger.
    """
    with transaction.atomic():
        transfer = (
            B2BStockTransfer.objects.select_for_update()
            .select_related('from_shop', 'to_shop').get(id=transfer.id)
        )
        if transfer.status != 'pending':
            raise ValidationError(
                f'Transfer is already {transfer.status}; only pending transfers can be completed.')

        branch = transfer.to_branch or _default_branch(transfer.to_shop)
        items = list(transfer.items.select_related('mapped_product', 'received_product'))
        unmapped = [item.product_name for item in items if item.mapped_product_id is None]
        if unmapped:
            raise ValidationError(
                'Map every line to one of your products before receiving. Unmapped: '
                + ', '.join(unmapped)
            )
        for item in items:
            product = item.mapped_product
            updates = []
            if not product.buying_price and item.unit_cost:
                product.buying_price = item.unit_cost
                updates.append('buying_price')
            if product.branch_id is None:
                product.branch = branch
                updates.append('branch')
            if updates:
                updates.append('updated_at')
                product.save(update_fields=updates)
            adjust_inventory(
                product, branch, 'in', item.quantity, user=user,
                reason=f'B2B transfer from {transfer.from_shop.name}',
            )
            item.received_product = product
        B2BStockTransferItem.objects.bulk_update(items, ['received_product'])

        transfer.status = 'completed'
        transfer.completed_by = user
        transfer.completed_at = timezone.now()
        transfer.save(update_fields=[
            'status', 'completed_by', 'completed_at', 'updated_at',
        ])
        return transfer


def accept_b2b_transfer(transfer, user):
    """Buyer's one-tap accept for a sale-sourced delivery manifest.

    Every line is matched against the buyer's catalogue (barcode, then SKU,
    then name) and unmatched lines get a product created for them, so receiving
    needs zero typing. The actual stock move reuses the row-locked completion.
    """
    with transaction.atomic():
        transfer = (
            B2BStockTransfer.objects.select_for_update()
            .select_related('to_shop').get(id=transfer.id)
        )
        if transfer.status != 'pending':
            raise ValidationError(
                f'Transfer is already {transfer.status}; only pending transfers can be accepted.')
        if transfer.source != 'sale':
            raise ValidationError(
                'Only sale deliveries accept in one tap; manual transfers stay mapped by hand.')

        items = list(transfer.items.all())
        for item in items:
            if item.mapped_product_id is not None:
                continue
            product = suggest_product_for_item(transfer.to_shop, item)
            if product is None:
                product = Product.objects.create(
                    shop=transfer.to_shop,
                    name=item.product_name[:255],
                    sku=item.sku[:100],
                    barcode=item.barcode[:100],
                    unit=item.unit[:100],
                    buying_price=item.unit_cost,
                )
            item.mapped_product = product
        B2BStockTransferItem.objects.bulk_update(items, ['mapped_product', 'updated_at'])
        return complete_b2b_transfer(transfer, user)


def stage_sale_delivery(sale, buyer_shop, created_by=None):
    """Write a POS sale's lines into the buyer's pending delivery manifest.

    The sale itself already deducted the seller's stock, so staging moves
    nothing — it only snapshots the manifest the buyer later accepts or
    declines. Re-staging the same sale returns the existing pending manifest
    instead of duplicating it.
    """
    if buyer_shop.pk == sale.shop_id:
        raise ValidationError('The buyer must be a different business than the seller.')
    existing = B2BStockTransfer.objects.filter(
        sale=sale, source='sale', status='pending').first()
    if existing is not None:
        return existing
    items = list(sale.items.select_related('product'))
    if not items:
        raise ValidationError('The sale has no lines to deliver.')
    transfer = B2BStockTransfer.objects.create(
        from_shop_id=sale.shop_id,
        to_shop=buyer_shop,
        from_branch=sale.branch,
        sale=sale,
        source='sale',
        created_by=created_by or sale.attendant,
        reference=f'SALE-{str(sale.pk)[:8]}',
        note=f'Incoming delivery from a sale at {sale.shop.name}.',
    )
    B2BStockTransferItem.objects.bulk_create(
        B2BStockTransferItem(
            transfer=transfer,
            product=item.product,
            product_name=(item.product.name if item.product else item.product_name)[:255],
            sku=((item.product.sku if item.product else '') or '')[:100],
            barcode=((item.product.barcode if item.product else '') or '')[:100],
            unit=((item.product.unit if item.product else 'pcs') or 'pcs')[:100],
            quantity=item.quantity,
            unit_cost=item.unit_price,
        )
        for item in items
    )
    return transfer


def cancel_b2b_transfer(transfer, user):
    """Return the quarantined quantities to the sender's dispatch branch.

    Sale-sourced manifests skip the restock: the sender's stock already left
    through the POS sale, so declining a delivery must not resurrect it.
    """
    with transaction.atomic():
        transfer = (
            B2BStockTransfer.objects.select_for_update()
            .select_related('from_shop').get(id=transfer.id)
        )
        if transfer.status != 'pending':
            raise ValidationError(
                f'Transfer is already {transfer.status}; only pending transfers can be cancelled.')

        if transfer.source == 'sale':
            transfer.status = 'cancelled'
            transfer.save(update_fields=['status', 'updated_at'])
            return transfer

        for item in transfer.items.select_related('product'):
            if item.product_id and transfer.from_branch_id:
                adjust_inventory(
                    item.product, transfer.from_branch, 'in', item.quantity, user=user,
                    reason=f'B2B transfer to {transfer.to_shop.name} cancelled',
                )
        transfer.status = 'cancelled'
        transfer.save(update_fields=['status', 'updated_at'])
        return transfer


def _default_branch(shop):
    branch = (
        Branch.objects.filter(shop=shop, is_main=True).first()
        or Branch.objects.filter(shop=shop).order_by('created_at').first()
    )
    if branch is None:
        raise ValidationError('The receiving shop has no branch to stock; they must create one first.')
    return branch


def suggest_product_for_item(shop, item):
    """Read-only prefill for the buyer's mapping UI: barcode, then SKU, then name."""
    if item.barcode:
        product = Product.objects.filter(shop=shop, barcode=item.barcode).first()
        if product is not None:
            return product
    if item.sku:
        product = Product.objects.filter(shop=shop, sku=item.sku).first()
        if product is not None:
            return product
    return Product.objects.filter(shop=shop, name__iexact=item.product_name).first()
