"""Reverse the Firestore import: delete every row that carries a legacy_id.

The import_firestore command marks every migrated record with legacy_id (the
original Firestore document id). This command deletes those records in reverse
dependency order so foreign-key constraints are satisfied.

Without --apply it prints what it would delete and touches nothing.

    python manage.py rollback_firestore_import              # dry run
    python manage.py rollback_firestore_import --apply       # delete from Django
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import ProtectedError

from apps.crm.models import Customer
from apps.products.models import Category, InventorySerialItem, MerchantCategory, Product
from apps.sales.models import Order, OrderItem, Sale, SaleItem
from apps.shops.models import Branch, Shop, UserRole


class Command(BaseCommand):
    help = 'Delete all records imported from Firestore (rows with legacy_id set).'

    # A Shop cascade reaches its products, and a product an IMEI anchors is
    # protected — so shops need the same keep-and-report handling as products.
    PROTECTED_MODELS = ('Product', 'Shop')

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Actually delete the rows (default: dry run)'
        )

    def handle(self, *args, **options):
        apply = options['apply']
        
        models = [
            ('SaleItem', SaleItem, 'sale'),
            ('OrderItem', OrderItem, 'order'),
            ('Sale', Sale, 'legacy_id'),
            ('Order', Order, 'legacy_id'),
            ('Customer', Customer, 'legacy_id'),
            ('UserRole', UserRole, None),
            ('Branch', Branch, 'legacy_id'),
            ('MerchantCategory', MerchantCategory, 'legacy_id'),
            ('Product', Product, 'legacy_id'),
            ('Category', Category, 'legacy_id'),
            ('Shop', Shop, 'legacy_id'),
        ]

        total_deleted = {}
        
        if apply:
            with transaction.atomic():
                for name, model, id_field in models:
                    if id_field is None:
                        count = self._delete_user_roles(model)
                    elif name in self.PROTECTED_MODELS:
                        count, kept = self._delete_protected(model, id_field)
                        self.stdout.write(f'{name}: deleted {count} rows')
                        if kept:
                            self.stdout.write(self.style.WARNING(
                                f'{name}: kept {kept} row(s) — sold IMEI units still '
                                f'anchor them (InventorySerialItem.product is PROTECT, '
                                f'the audit trail must survive a rollback).'))
                        total_deleted[name] = count
                        continue
                    else:
                        count, _ = model.objects.filter(**{f'{id_field}__isnull': False}).delete()
                    total_deleted[name] = count
                    self.stdout.write(f'{name}: deleted {count} rows')
        else:
            anchored = (
                InventorySerialItem.objects.filter(product__legacy_id__isnull=False)
                .values('product_id').distinct().count()
            )
            blocked_shops = Shop.objects.filter(
                products__serial_items__isnull=False).distinct().count()
            for name, model, id_field in models:
                if id_field is None:
                    count = self._count_user_roles(model)
                else:
                    count = model.objects.filter(**{f'{id_field}__isnull': False}).count()
                total_deleted[name] = count
                self.stdout.write(f'{name}: would delete {count} rows')
            if anchored:
                self.stdout.write(self.style.WARNING(
                    f'Product: {anchored} imported product(s) would be KEPT — '
                    f'sold IMEI units still anchor them.'))
            if blocked_shops:
                self.stdout.write(self.style.WARNING(
                    f'Shop: {blocked_shops} imported shop(s) would be KEPT — '
                    f'their anchored products keep the shop row alive.'))

        self.stdout.write('')
        header = 'Rollback APPLIED' if apply else 'Rollback dry run (nothing deleted)'
        self.stdout.write(self.style.WARNING(header))
        self.stdout.write(f'Total: {sum(total_deleted.values())} rows')

    def _delete_protected(self, model, id_field):
        """Delete imported rows one by one, keeping any an IMEI unit still anchors.

        ``InventorySerialItem.product`` is PROTECT: a device a shop sold months
        ago must keep its record even during a full import rollback, and since
        ``Product.shop`` cascades, the shop row falls with its kept product.
        """
        queryset = model.objects.filter(**{f'{id_field}__isnull': False})
        kept = 0
        deleted = 0
        for row in queryset.iterator():
            try:
                row.delete()
                deleted += 1
            except ProtectedError:
                kept += 1
        return deleted, kept

    def _delete_user_roles(self, model):
        """UserRole links users to shops; delete roles for imported shops."""
        shop_ids = Shop.objects.filter(legacy_id__isnull=False).values_list('id', flat=True)
        count = model.objects.filter(shop_id__in=shop_ids).count()
        model.objects.filter(shop_id__in=shop_ids).delete()
        return count

    def _count_user_roles(self, model):
        """Count UserRole rows that would be deleted."""
        shop_ids = Shop.objects.filter(legacy_id__isnull=False).values_list('id', flat=True)
        return model.objects.filter(shop_id__in=shop_ids).count()
