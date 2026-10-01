"""Backfill Django with the rows Firestore already holds.

Reads Firestore but never writes to it. Rows are keyed by ``legacy_id`` so the
command is idempotent: a re-run refreshes existing rows instead of duplicating
them. Without ``--apply`` it prints what it would do and touches nothing.

    python manage.py import_firestore                # dry run, every shop
    python manage.py import_firestore --shop <id>    # dry run, one shop
    python manage.py import_firestore --apply        # write to Django
"""
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
import time

import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import ServiceUnavailable
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core import firestore_import as mapping
# pyrefly: ignore [missing-import]
from apps.crm.models import Customer
# pyrefly: ignore [missing-import]
from apps.crm.services import find_user_for_identity, resolve_sale_customer_user
from apps.products.models import Category, MerchantCategory, Product
from apps.sales.models import Order, OrderItem, Sale, SaleItem
from apps.shops.models import Branch, Shop, UserRole
from apps.users.models import User
from apps.users.serializers import find_user_by_email

#: Dry-run stand-in for a user that would only exist after ``--apply``.
SCRATCH_USER = object()

#: Attempts per Firestore read; a dropped stream is reopened from the start.
STREAM_ATTEMPTS = 4


def _is_transient_stream_error(exc):
    """True when reopening the stream may succeed.

    The bundled google-cloud-firestore retry hook is incompatible with the
    installed google-api-core (its run_query callable has no ``_retry``), so a
    dropped stream surfaces as ``AttributeError`` wrapping the real
    ``ServiceUnavailable`` instead of being retried by the library.
    """
    if isinstance(exc, ServiceUnavailable):
        return True
    return isinstance(exc, AttributeError) and '_retry' in str(exc)


def fetch_all(reference):
    """Every document behind a query, reread when the stream drops mid-read.

    Reads are idempotent, so a retry restarts the stream; the list is only
    handed to the caller once complete, keeping per-document side effects
    (report counters, ``--apply`` saves) from running twice.
    """
    for attempt in range(1, STREAM_ATTEMPTS + 1):
        try:
            return list(reference.stream())
        except Exception as exc:
            if attempt == STREAM_ATTEMPTS or not _is_transient_stream_error(exc):
                raise
            time.sleep(min(2 ** attempt, 30))


class Report:
    """Counter set plus warnings, printed as one summary at the end."""

    def __init__(self):
        self.counters = Counter()
        self.warnings = []

    def bump(self, key, amount=1):
        if amount:
            self.counters[key] += amount

    def warn(self, message):
        self.warnings.append(message)

    def summary_lines(self):
        groups = {}
        for key, value in self.counters.items():
            group, _, stat = key.partition('.')
            groups.setdefault(group, []).append(f'{stat}={value}')
        width = max((len(group) for group in groups), default=0)
        lines = [f'{group:<{width}}  ' + '  '.join(stats) for group, stats in sorted(groups.items())]
        if self.warnings:
            lines.append('')
            lines.append(f'warnings ({len(self.warnings)}):')
            lines.extend(f'  {warning}' for warning in self.warnings[:25])
            if len(self.warnings) > 25:
                lines.append(f'  ... and {len(self.warnings) - 25} more')
        return lines


def save_row(model, legacy_id, kwargs, apply, instance=None):
    """Create or refresh the row carrying ``legacy_id``; returns (row, created)."""
    if instance is None:
        instance = model.objects.filter(legacy_id=legacy_id).first()
    if instance is None:
        instance = model(legacy_id=legacy_id, **kwargs)
        if apply:
            instance.save()
        return instance, True
    for field, value in kwargs.items():
        setattr(instance, field, value)
    if apply:
        instance.save()
    return instance, False


def apply_timestamps(instance, data, apply):
    """Restore the Firestore timestamps.

    ``auto_now_add``/``auto_now`` override anything passed to ``save()``, so the
    import timestamps are written with a follow-up ``UPDATE``.
    """
    if not apply or instance.pk is None:
        return
    values = {}
    created = mapping.datetime_value(data.get('createdAt'))
    updated = mapping.datetime_value(data.get('updatedAt'))
    if created:
        values['created_at'] = created
    if updated:
        values['updated_at'] = updated
    if values:
        type(instance).objects.filter(pk=instance.pk).update(**values)


class Command(BaseCommand):
    help = ('Backfill shops, users, roles, products, branches, customers, sales and '
            'orders from Firestore (read-only towards Firestore).')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the rows to Django (default: dry run)')
        parser.add_argument('--shop', action='append', default=[], metavar='SHOP_ID',
                            help='Only import this Firestore shop id; repeatable')
        parser.add_argument('--limit', type=int, metavar='N',
                            help='Import at most N shops (debugging)')

    def handle(self, *args, **options):
        self.apply = options['apply']
        self.report = Report()
        self.users_raw = {}
        self.resolved_users = {}
        self.planned_users = set()
        self.role_keys = set()
        self.branches = {}
        self.main_branches = {}
        self.branch_cache = {}
        self.customer_users = {}
        self.products = {}
        self.product_cache = {}

        client = self._client()
        shops_raw = {d.id: d.to_dict() for d in fetch_all(client.collection('shops'))}
        selected = self._select_shops(shops_raw, options)
        self.users_raw = {d.id: d.to_dict() for d in fetch_all(client.collection('users'))}
        roles_raw = [d.to_dict() for d in fetch_all(client.collection('user_roles'))]
        products_raw = [(d.id, d.to_dict()) for d in fetch_all(client.collection('products'))]
        branches_raw = [(d.id, d.to_dict()) for d in fetch_all(client.collection('branches'))]
        customers_raw = [(d.id, d.to_dict()) for d in fetch_all(client.collection('customers'))]
        balance_raw = {
            (shop_id, d.id): d.to_dict()
            for shop_id in selected
            for d in fetch_all(client.collection('shops', shop_id, 'customer_balances'))
        }
        merchant_raw = {
            shop_id: [(d.id, d.to_dict())
                      for d in fetch_all(client.collection('shops', shop_id, 'merchant_categories'))]
            for shop_id in selected
        }

        taken_slugs = set(Shop.objects.exclude(slug__isnull=True).exclude(slug='')
                          .values_list('slug', flat=True))
        category_cache = {}
        orphan_shops = set()

        with transaction.atomic() if self.apply else nullcontext():
            shops = {}
            for shop_id in selected:
                data = shops_raw[shop_id]
                instance = Shop.objects.filter(legacy_id=shop_id).first()
                kwargs = mapping.shop_kwargs(
                    shop_id, data, None if (instance and instance.slug) else taken_slugs)
                if instance and instance.slug:
                    kwargs['slug'] = instance.slug
                shop, created = save_row(Shop, shop_id, kwargs, self.apply, instance)
                apply_timestamps(shop, data, self.apply)
                self.report.bump('shops.created' if created else 'shops.updated')
                shops[shop_id] = shop

                for category_id, category_data in merchant_raw.get(shop_id, []):
                    _, cat_created = save_row(
                        MerchantCategory, category_id,
                        mapping.merchant_category_kwargs(category_id, category_data, shop),
                        self.apply)
                    self.report.bump('merchant_categories.created' if cat_created
                                     else 'merchant_categories.updated')

            for role in roles_raw:
                shop = shops.get(mapping.text(role.get('shopId'), 64))
                if shop is None:
                    self.report.bump('user_roles.skipped_other_shop')
                    continue
                self._attach_role(shop, mapping.text(role.get('userId'), 128),
                                  mapping.role_value(role.get('role')))

            for shop_id, data in ((sid, shops_raw[sid]) for sid in selected):
                self._attach_role(shops[shop_id], mapping.text(data.get('ownerId'), 128), 'owner')

            for product_id, data in products_raw:
                shop_legacy = mapping.text(data.get('shopId'), 64)
                if shop_legacy not in shops_raw:
                    self.report.bump('products.skipped_orphan')
                    orphan_shops.add(shop_legacy)
                    continue
                shop = shops.get(shop_legacy)
                if shop is None:
                    self.report.bump('products.skipped_other_shop')
                    continue
                category = self._category_for(shop, mapping.category_name(data), category_cache)
                product, created = save_row(
                    Product, product_id,
                    mapping.product_kwargs(product_id, data, shop, category), self.apply)
                apply_timestamps(product, data, self.apply)
                self.products[product_id] = product
                self.report.bump('products.created' if created else 'products.updated')

            for branch_id, data in branches_raw:
                shop_legacy = mapping.text(data.get('shopId'), 64)
                if shop_legacy not in shops_raw:
                    self.report.bump('branches.skipped_orphan')
                    orphan_shops.add(shop_legacy)
                    continue
                shop = shops.get(shop_legacy)
                if shop is None:
                    self.report.bump('branches.skipped_other_shop')
                    continue
                manager_id = mapping.text(data.get('managerId'), 128)
                manager = self._resolve_user(manager_id) if manager_id else None
                branch, created = save_row(
                    Branch, branch_id,
                    mapping.branch_kwargs(branch_id, data, shop, manager), self.apply)
                apply_timestamps(branch, data, self.apply)
                self.branches[branch_id] = branch
                self.report.bump('branches.created' if created else 'branches.updated')

            for customer_id, data in customers_raw:
                shop_legacy = mapping.text(data.get('shopId'), 64)
                if shop_legacy not in shops_raw:
                    self.report.bump('customers.skipped_orphan')
                    orphan_shops.add(shop_legacy)
                    continue
                shop = shops.get(shop_legacy)
                if shop is None:
                    self.report.bump('customers.skipped_other_shop')
                    continue
                balance = balance_raw.get((shop_legacy, customer_id))
                customer, created = save_row(
                    Customer, customer_id,
                    mapping.customer_kwargs(customer_id, data, shop, balance), self.apply)
                apply_timestamps(customer, data, self.apply)
                self.report.bump('customers.created' if created else 'customers.updated')

            for shop_id in selected:
                shop = shops[shop_id]
                for day in fetch_all(client.collection('shops', shop_id, 'sales_days')):
                    for sale_doc in fetch_all(day.reference.collection('sales')):
                        self._import_sale(shop, sale_doc.id, sale_doc.to_dict())

            for shop_id in selected:
                shop = shops[shop_id]
                for order_doc in fetch_all(client.collection('shops', shop_id, 'orders')):
                    self._import_order(shop, order_doc.id, order_doc.to_dict())

        header = ('Firestore import - APPLIED to Django' if self.apply
                  else 'Firestore import - dry run (nothing written)')
        self.stdout.write(self.style.MIGRATE_HEADING(header))
        for line in self.report.summary_lines():
            self.stdout.write(line)
        if orphan_shops:
            self.stdout.write('')
            self.stdout.write('No shops/<id> document; these rows were left behind:')
            self.stdout.write('  ' + ' '.join(sorted(orphan_shops)))

    def _client(self):
        if not firebase_admin._apps:
            path = Path(settings.FIREBASE_CREDENTIALS)
            if not path.exists():
                raise CommandError(f'Firebase credentials not found: {path}')
            firebase_admin.initialize_app(credentials.Certificate(str(path)))
        return firestore.client()

    def _select_shops(self, shops_raw, options):
        selected = sorted(shops_raw)
        if options['shop']:
            unknown = sorted(set(options['shop']) - set(shops_raw))
            if unknown:
                raise CommandError(f'No such Firestore shop: {", ".join(unknown)}')
            selected = [sid for sid in selected if sid in set(options['shop'])]
        if options['limit'] is not None:
            selected = selected[:options['limit']]
        self.report.bump('scope.shops', len(selected))
        return selected

    def _resolve_user(self, uid):
        """Django user for a Firebase uid; SCRATCH_USER in dry-run when new."""
        if uid in self.resolved_users:
            return self.resolved_users[uid]
        data = self.users_raw.get(uid)
        if data is None:
            self.report.warn(f'users/{uid}: no document, left unlinked')
            self.resolved_users[uid] = None
            return None
        email = mapping.text(data.get('email')).lower()
        if not email:
            self.report.warn(f'users/{uid}: no email, cannot be matched')
            self.resolved_users[uid] = None
            return None

        user = find_user_by_email(email)
        if user is None:
            if not self.apply:
                self.resolved_users[uid] = SCRATCH_USER
                if uid not in self.planned_users:
                    self.planned_users.add(uid)
                    self.report.bump('users.would_create')
                return SCRATCH_USER
            user = User(**mapping.user_kwargs(uid, data, email))
            user.set_unusable_password()
            user.save()
            self.report.bump('users.created')
        elif not user.firebase_uid:
            if self.apply:
                user.firebase_uid = uid
                user.save(update_fields=['firebase_uid'])
                self.report.bump('users.linked')
            else:
                self.report.bump('users.would_link')
        self.resolved_users[uid] = user
        return user

    def _attach_role(self, shop, uid, role):
        if not uid:
            self.report.warn(f'{shop.legacy_id}: role without a user id')
            return
        if (shop.legacy_id, uid) in self.role_keys:
            return
        user = self._resolve_user(uid)
        if user is None:
            return
        self.role_keys.add((shop.legacy_id, uid))
        if not self.apply:
            self.report.bump('user_roles.would_create')
            return
        _, created = UserRole.objects.get_or_create(user=user, shop=shop, defaults={'role': role})
        self.report.bump('user_roles.created' if created else 'user_roles.kept')

    def _customer_user(self, customer_id, customer_phone, firebase_uid=None):
        """The buyer's platform account, resolved the way Firestore's writes did."""
        key = (str(customer_id or ''), str(customer_phone or ''), str(firebase_uid or ''))
        if key not in self.customer_users:
            user = find_user_for_identity(user_id=firebase_uid) if firebase_uid else None
            if user is None:
                user = resolve_sale_customer_user(
                    customer_id=customer_id, customer_phone=customer_phone)
            self.customer_users[key] = user
        return self.customer_users[key]

    def _product_for(self, legacy_id):
        if not legacy_id:
            return None
        if legacy_id not in self.product_cache:
            product = self.products.get(legacy_id)
            if product is None:
                product = Product.objects.filter(legacy_id=legacy_id).first()
            self.product_cache[legacy_id] = product
        return self.product_cache[legacy_id]

    def _branch_for(self, shop, branch_legacy):
        """Named branch for a sale/order, else the shop's main branch.

        ``Sale.branch``/``Order.branch`` are NOT NULL, so rows whose Firestore
        ``branchId`` is empty or dangling still need a branch to hang from.
        """
        key = (shop.legacy_id, branch_legacy)
        if key in self.branch_cache:
            return self.branch_cache[key]
        branch = self.branches.get(branch_legacy) if branch_legacy else None
        if branch is not None and branch.shop is not shop:
            branch = None
        if branch is None and branch_legacy and shop.pk is not None:
            branch = Branch.objects.filter(legacy_id=branch_legacy, shop=shop).first()
        if branch is None:
            branch = self._main_branch(shop)
        self.branch_cache[key] = branch
        return branch

    def _main_branch(self, shop):
        if shop.legacy_id in self.main_branches:
            return self.main_branches[shop.legacy_id]
        branch = None
        if shop.pk is not None:
            branch = (Branch.objects.filter(shop=shop, is_main=True).first()
                      or Branch.objects.filter(shop=shop).order_by('created_at').first())
        if branch is None:
            branch = next((b for b in self.branches.values() if b.shop is shop), None)
        if branch is None:
            branch = Branch(shop=shop, name='Main Branch', is_main=True, is_active=True)
            if self.apply:
                branch.save()
            self.report.bump('branches.derived')
        self.main_branches[shop.legacy_id] = branch
        return branch

    def _import_sale(self, shop, sale_id, data):
        branch = self._branch_for(shop, mapping.text(data.get('branchId'), 64))
        created_by = mapping.text(data.get('createdBy'), 128)
        sale, created = save_row(Sale, sale_id, mapping.sale_kwargs(
            data, shop, branch,
            attendant=self._resolve_user(created_by) if created_by else None,
            customer_user=self._customer_user(
                data.get('customerId'), data.get('customerPhone'), data.get('customerUserId')),
        ), self.apply)
        # Sale documents also carry ``date`` (YYYY-MM-DD); older ones lack createdAt.
        apply_timestamps(sale, {**data, 'createdAt': data.get('createdAt') or data.get('date')},
                         self.apply)
        self.report.bump('sales.created' if created else 'sales.updated')
        self._sync_items(sale, SaleItem, 'sale', 'sale_items', data,
                         mapping.sale_item_kwargs)

    def _import_order(self, shop, order_id, data):
        branch = self._branch_for(shop, mapping.text(data.get('branchId'), 64))
        order, created = save_row(Order, order_id, mapping.order_kwargs(
            data, shop, branch,
            customer_user=self._customer_user(
                data.get('customerId'), data.get('customerPhone'), data.get('customerUserId')),
        ), self.apply)
        apply_timestamps(order, data, self.apply)
        self.report.bump('orders.created' if created else 'orders.updated')
        self._sync_items(order, OrderItem, 'order', 'order_items', data,
                         mapping.order_item_kwargs)

    def _sync_items(self, parent, model, parent_field, group, data, kwargs_fn):
        """Rebuild a sale/order's line items; embedded Firestore items have no id."""
        rows = mapping.line_items(data)
        if not rows:
            return
        objs = []
        for item in rows:
            product = self._product_for(item['productId'])
            if product is None and item['productId']:
                self.report.bump(f'{group}.missing_product')
            objs.append(model(**{parent_field: parent}, **kwargs_fn(item, product)))
        if self.apply and parent.pk is not None:
            parent.items.all().delete()
            model.objects.bulk_create(objs)
        self.report.bump(f'{group}.imported', len(objs))

    def _category_for(self, shop, name, cache):
        """Product ``category`` is a display string, so the row is derived."""
        if not name:
            return None
        key = (shop.legacy_id, name)
        if key in cache:
            return cache[key]
        if self.apply:
            category, created = Category.objects.get_or_create(shop=shop, name=name)
            self.report.bump('categories.created' if created else 'categories.kept')
        else:
            category = Category(shop=shop, name=name)
            self.report.bump('categories.derived')
        cache[key] = category
        return category
