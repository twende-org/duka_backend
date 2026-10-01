"""Tests for the Delivery App sync port (``functions/src/sync/deliverySync.js``).

The remote project is faked in memory and patched over
``apps.core.delivery_sync.get_delivery_client``; Celery runs eagerly in tests, so
the signal -> ``transaction.on_commit`` -> task -> client path executes inline
inside ``captureOnCommitCallbacks``.
"""
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

# pyrefly: ignore [missing-import]
from apps.core import delivery_sync
# pyrefly: ignore [missing-import]
from apps.core import tasks as core_tasks
# pyrefly: ignore [missing-import]
from apps.products.models import Category, Inventory, Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.users.models import User


class _FakeDocument:
    def __init__(self, client, collection, key):
        self._client = client
        self._collection = collection
        self.key = key
        self.reference = self

    def set(self, payload, merge=False):
        self._client.apply_set(self._collection, self.key, payload, merge)

    def delete(self):
        self._client.deletes.append((self._collection, self.key))
        self._client.docs.pop((self._collection, self.key), None)


class _FakeQuery:
    def __init__(self, client, collection, field, value):
        self._client = client
        self._collection = collection
        self._field = field
        self._value = value

    def stream(self):
        return [
            _FakeDocument(self._client, collection, key)
            for (collection, key), data in list(self._client.docs.items())
            if collection == self._collection and data.get(self._field) == self._value
        ]


class _FakeCollection:
    def __init__(self, client, name):
        self._client = client
        self._name = name

    def document(self, key):
        return _FakeDocument(self._client, self._name, str(key))

    def where(self, field, op, value):
        return _FakeQuery(self._client, self._name, field, value)


class _FakeBatch:
    def __init__(self, client):
        self._client = client
        self._ops = []

    def set(self, reference, payload, merge=False):
        self._ops.append((reference._collection, reference.key, dict(payload), merge))

    def commit(self):
        self._client.batch_commits.append(len(self._ops))
        for collection, key, payload, merge in self._ops:
            self._client.apply_set(collection, key, payload, merge)
        self._ops = []


class FakeDeliveryClient:
    """In-memory stand-in for the Delivery App Firestore client."""

    def __init__(self):
        self.docs = {}
        self.deletes = []
        self.batch_commits = []

    def collection(self, name):
        return _FakeCollection(self, name)

    def batch(self):
        return _FakeBatch(self)

    def apply_set(self, collection, key, payload, merge):
        store = self.docs.setdefault((collection, key), {})
        if not merge:
            store.clear()
        store.update(payload)

    def remote(self, key, collection=None):
        from django.conf import settings as django_settings

        name = collection or django_settings.DELIVERY_APP_COLLECTION
        return self.docs.get((name, str(key)))


class DeliverySyncBaseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='owner1', email='owner1@test.com', password='x', firebase_uid='fb-owner-1'
        )
        self.shop = Shop.objects.create(
            name='Duka A', legacy_id='shop-1', lat=-6.8, lon=39.2,
            productCategories=['Retail'],
        )
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main')

    def make_product(self, **kwargs):
        values = {
            'shop': self.shop,
            'name': 'Soda',
            'legacy_id': 'prod-1',
            'selling_price': Decimal('1200.00'),
            'discount': Decimal('300.00'),
            'status': 'active',
            'brand': 'Azam',
            'description': 'Cold soda',
            'image_url': 'https://cdn.example/soda.jpg',
            'rating': 4.5,
        }
        values.update(kwargs)
        return Product.objects.create(**values)


class DeliverySyncGuardTests(DeliverySyncBaseTests):
    def test_disabled_by_default(self):
        self.assertFalse(settings.DELIVERY_APP_SYNC_ENABLED)
        product = self.make_product()
        with self.assertRaises(delivery_sync.DeliverySyncDisabled):
            delivery_sync.push_product(product)
        with self.assertRaises(delivery_sync.DeliverySyncDisabled):
            delivery_sync.push_stock(product)

    def test_task_returns_disabled_without_credentials(self):
        product = self.make_product(publish_to_delivery_app=True)
        self.assertEqual(core_tasks.sync_product_to_delivery_app(str(product.pk)), 'disabled')

    @override_settings(DELIVERY_APP_SYNC_ENABLED=True, DELIVERY_APP_CREDENTIALS='/nope/key.json')
    def test_missing_credentials_file_raises(self):
        delivery_sync._client = None
        with self.assertRaises(delivery_sync.DeliverySyncDisabled):
            delivery_sync.get_delivery_client()

    def test_flagged_save_is_silent_while_disabled(self):
        with self.captureOnCommitCallbacks(execute=True):
            product = self.make_product(publish_to_delivery_app=True, legacy_id='prod-guard')
        product.refresh_from_db()
        self.assertTrue(product.publish_to_delivery_app)

    def test_broker_failure_never_breaks_the_save(self):
        broken = mock.Mock()
        broken.delay.side_effect = RuntimeError('broker down')
        with mock.patch.object(delivery_sync, 'get_delivery_client', return_value=FakeDeliveryClient()):
            with mock.patch('apps.core.signals.sync_product_to_delivery_app', broken):
                with self.captureOnCommitCallbacks(execute=True):
                    product = self.make_product(publish_to_delivery_app=True, legacy_id='prod-broker')
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())
        self.assertTrue(broken.delay.called)

    def test_remote_failure_returns_error_instead_of_raising(self):
        fake = FakeDeliveryClient()
        with mock.patch.object(delivery_sync, 'get_delivery_client', return_value=fake):
            with mock.patch.object(
                delivery_sync, 'push_product', side_effect=RuntimeError('firestore 503')
            ):
                product = self.make_product(publish_to_delivery_app=True, legacy_id='prod-fail')
                self.assertEqual(core_tasks.sync_product_to_delivery_app(str(product.pk)), 'error')


class DeliveryTranslateTests(DeliverySyncBaseTests):
    def test_full_legacy_schema(self):
        category = Category.objects.create(shop=self.shop, name='Beverages')
        Inventory.objects.create(product=self.make_product(category=category), branch=self.branch, quantity=7)
        product = Product.objects.get(legacy_id='prod-1')

        payload = delivery_sync.translate_product(product)

        self.assertEqual(payload['availability'], True)
        self.assertEqual(payload['brand'], 'Azam')
        self.assertEqual(payload['cat'], 'Retail')
        self.assertEqual(payload['category'], 'Product')
        self.assertEqual(payload['description'], 'Cold soda')
        self.assertIs(payload['fav'], False)
        self.assertEqual(payload['foodId'], 'prod-1')
        self.assertEqual(payload['imgURL'], ['https://cdn.example/soda.jpg'])
        self.assertEqual(payload['location'], '-6.8,39.2')
        self.assertEqual(payload['name'], 'Soda')
        self.assertEqual(payload['price'], 1200.0)
        self.assertEqual(payload['quantity'], 7)
        self.assertEqual(payload['rate'], [4.5])
        self.assertEqual(payload['store'], 'Duka A')
        self.assertEqual(payload['subCat'], 'Beverages')
        self.assertEqual(payload['shopId'], 'shop-1')
        self.assertEqual(payload['uid'], 'fb-owner-1')
        self.assertEqual(payload['oldprice'], 1500.0)
        self.assertIsInstance(payload['time'], str)
        self.assertIn('T', payload['time'])

    def test_inactive_status_is_unavailable(self):
        product = self.make_product(status='inactive')
        self.assertFalse(delivery_sync.translate_product(product)['availability'])

    def test_subcat_fallback_chain(self):
        product = self.make_product(legacy_id='p-a', marketplace_categories=['Electronics'])
        self.assertEqual(delivery_sync.translate_product(product)['subCat'], 'Electronics')

        product = self.make_product(legacy_id='p-b', marketplace_category_id='cat-9')
        self.assertEqual(delivery_sync.translate_product(product)['subCat'], 'cat-9')

        product = self.make_product(legacy_id='p-c')
        self.assertEqual(delivery_sync.translate_product(product)['subCat'], 'General')

    def test_shop_without_categories_falls_back_to_product(self):
        shop = Shop.objects.create(name='Bare Shop', legacy_id='shop-bare')
        product = self.make_product(shop=shop, legacy_id='p-bare')
        payload = delivery_sync.translate_product(product)
        self.assertEqual(payload['cat'], 'Product')
        self.assertEqual(payload['location'], '')
        self.assertEqual(payload['uid'], '')

    def test_image_falls_back_to_image_urls_and_empty(self):
        product = self.make_product(legacy_id='p-img', image_url='', image_urls=['https://cdn.example/2.jpg'])
        self.assertEqual(delivery_sync.translate_product(product)['imgURL'], ['https://cdn.example/2.jpg'])

        product = self.make_product(legacy_id='p-noimg', image_url='', image_urls=[])
        self.assertEqual(delivery_sync.translate_product(product)['imgURL'], [])

    def test_quantity_sums_across_branches(self):
        product = self.make_product(legacy_id='p-stock')
        other = Branch.objects.create(shop=self.shop, name='Second')
        Inventory.objects.create(product=product, branch=self.branch, quantity=3)
        Inventory.objects.create(product=product, branch=other, quantity=4)
        self.assertEqual(delivery_sync.product_quantity(product), 7)

        blank = self.make_product(legacy_id='p-nostock')
        self.assertEqual(delivery_sync.product_quantity(blank), 0)

    def test_document_key_prefers_legacy_id(self):
        legacy = self.make_product(legacy_id='legacy-key')
        self.assertEqual(delivery_sync.document_key(legacy), 'legacy-key')

        fresh = self.make_product(legacy_id=None, name='No legacy')
        self.assertEqual(delivery_sync.document_key(fresh), str(fresh.pk))

    def test_owner_uid_falls_back_to_pk(self):
        other = User.objects.create_user(username='owner2', email='o2@test.com', password='x')
        shop = Shop.objects.create(name='Duka B', legacy_id='shop-2')
        UserRole.objects.create(user=other, shop=shop, role='owner')
        self.assertEqual(delivery_sync.shop_owner_uid(shop), str(other.pk))


class DeliveryPushTests(DeliverySyncBaseTests):
    def setUp(self):
        super().setUp()
        self.fake = FakeDeliveryClient()
        patcher = mock.patch.object(delivery_sync, 'get_delivery_client', return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_push_product_merges_into_legacy_key(self):
        product = self.make_product(legacy_id='prod-push')
        payload = delivery_sync.push_product(product)
        self.assertEqual(payload['name'], 'Soda')
        self.assertEqual(self.fake.remote('prod-push')['store'], 'Duka A')

    def test_product_create_with_flag_pushes(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_product(publish_to_delivery_app=True, legacy_id='prod-on')
        remote = self.fake.remote('prod-on')
        self.assertEqual(remote['quantity'], 0)
        self.assertEqual(remote['foodId'], 'prod-on')

    def test_product_create_without_flag_is_skipped(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_product(legacy_id='prod-off')
        self.assertIsNone(self.fake.remote('prod-off'))
        self.assertEqual(self.fake.docs, {})

    def test_product_update_repushes_merged_document(self):
        with self.captureOnCommitCallbacks(execute=True):
            product = self.make_product(publish_to_delivery_app=True, legacy_id='prod-up')
        self.fake.docs[('products', 'prod-up')]['promoTag'] = 'ramadan'  # delivery-app-only key

        product.name = 'Soda Mkubwa'
        with self.captureOnCommitCallbacks(execute=True):
            product.save()

        remote = self.fake.remote('prod-up')
        self.assertEqual(remote['name'], 'Soda Mkubwa')
        self.assertEqual(remote['promoTag'], 'ramadan')

    def test_true_to_false_transition_deletes_remote(self):
        with self.captureOnCommitCallbacks(execute=True):
            product = self.make_product(publish_to_delivery_app=True, legacy_id='prod-toggle')
        self.assertIsNotNone(self.fake.remote('prod-toggle'))

        product.publish_to_delivery_app = False
        with self.captureOnCommitCallbacks(execute=True):
            product.save()

        self.assertIsNone(self.fake.remote('prod-toggle'))
        self.assertIn(('products', 'prod-toggle'), self.fake.deletes)

    def test_false_to_false_save_keeps_remote(self):
        """Deviation pin: Django cannot distinguish "never set" from "off"."""
        product = self.make_product(publish_to_delivery_app=False, legacy_id='prod-imported')
        # The row was imported with the toggle off while a legacy mirror exists.
        self.fake.docs[('products', 'prod-imported')] = {'name': 'Soda', 'shopId': 'shop-1'}

        with self.captureOnCommitCallbacks(execute=True):
            product.save()

        self.assertEqual(self.fake.deletes, [])
        self.assertIsNotNone(self.fake.remote('prod-imported'))

    def test_product_delete_removes_remote(self):
        with self.captureOnCommitCallbacks(execute=True):
            product = self.make_product(publish_to_delivery_app=True, legacy_id='prod-del')
        with self.captureOnCommitCallbacks(execute=True):
            product.delete()
        self.assertIsNone(self.fake.remote('prod-del'))

    def test_inventory_save_pushes_quantity_only(self):
        product = self.make_product(publish_to_delivery_app=True, legacy_id='prod-inv')
        delivery_sync.push_product(product)  # seed the remote document

        with self.captureOnCommitCallbacks(execute=True):
            Inventory.objects.create(product=product, branch=self.branch, quantity=12)

        remote = self.fake.remote('prod-inv')
        self.assertEqual(remote['quantity'], 12)
        self.assertEqual(remote['name'], 'Soda')  # untouched keys survive the merge

    def test_inventory_update_pushes_new_quantity(self):
        product = self.make_product(publish_to_delivery_app=True, legacy_id='prod-inv2')
        stock = Inventory.objects.create(product=product, branch=self.branch, quantity=2)

        with self.captureOnCommitCallbacks(execute=True):
            stock.quantity = 9
            stock.save()

        self.assertEqual(self.fake.remote('prod-inv2'), {'quantity': 9})

    def test_shop_rename_batches_store_update(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_product(publish_to_delivery_app=True, legacy_id='prod-s1')
        with self.captureOnCommitCallbacks(execute=True):
            self.make_product(publish_to_delivery_app=True, legacy_id='prod-s2')

        self.shop.name = 'Duka Jipya'
        with self.captureOnCommitCallbacks(execute=True):
            self.shop.save()

        self.assertEqual(self.fake.remote('prod-s1')['store'], 'Duka Jipya')
        self.assertEqual(self.fake.remote('prod-s2')['store'], 'Duka Jipya')
        self.assertEqual(self.fake.batch_commits, [2])

    def test_shop_location_change_updates_location_only(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_product(publish_to_delivery_app=True, legacy_id='prod-loc')

        self.shop.lat = -6.9
        with self.captureOnCommitCallbacks(execute=True):
            self.shop.save()

        remote = self.fake.remote('prod-loc')
        self.assertEqual(remote['location'], '-6.9,39.2')
        self.assertEqual(remote['store'], 'Duka A')

    def test_shop_save_without_identity_change_is_noop(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_product(publish_to_delivery_app=True, legacy_id='prod-noop')

        self.shop.description = 'Ni duka zuri'
        with self.captureOnCommitCallbacks(execute=True):
            self.shop.save()

        self.assertEqual(self.fake.batch_commits, [])
        self.assertEqual(self.fake.remote('prod-noop')['store'], 'Duka A')

    def test_shop_update_with_no_mirrored_products_writes_nothing(self):
        self.shop.name = 'Duka Tupu'
        with self.captureOnCommitCallbacks(execute=True):
            self.shop.save()
        self.assertEqual(self.fake.batch_commits, [])
        self.assertEqual(self.fake.docs, {})


class DeliveryMigrationCommandTests(DeliverySyncBaseTests):
    def setUp(self):
        super().setUp()
        self.fake = FakeDeliveryClient()
        self.flagged = self.make_product(publish_to_delivery_app=True, legacy_id='cmd-on')
        self.unflagged = self.make_product(publish_to_delivery_app=False, legacy_id='cmd-off')

    def test_dry_run_writes_nothing(self):
        patcher = mock.patch.object(delivery_sync, 'get_delivery_client', return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

        out = mock.Mock()
        call_command('sync_delivery_app', stdout=out)

        message = out.write.call_args[0][0]
        self.assertIn('Dry run: 1 product(s)', message)
        self.assertEqual(self.fake.docs, {})

    def test_dry_run_all_includes_unflagged(self):
        out = mock.Mock()
        call_command('sync_delivery_app', '--all', stdout=out)
        self.assertIn('Dry run: 2 product(s)', out.write.call_args[0][0])

    def test_dry_run_limit(self):
        out = mock.Mock()
        call_command('sync_delivery_app', '--all', '--limit', '1', stdout=out)
        self.assertIn('Dry run: 1 product(s)', out.write.call_args[0][0])

    @override_settings(DELIVERY_APP_SYNC_ENABLED=True)
    def test_apply_pushes_flagged_products(self):
        patcher = mock.patch.object(delivery_sync, 'get_delivery_client', return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

        out = mock.Mock()
        call_command('sync_delivery_app', '--apply', stdout=out)

        self.assertIn('Successfully migrated 1 products', out.write.call_args[0][0])
        self.assertIsNotNone(self.fake.remote('cmd-on'))
        self.assertIsNone(self.fake.remote('cmd-off'))

    def test_apply_refuses_while_disabled(self):
        with self.assertRaisesMessage(CommandError, 'DELIVERY_APP_SYNC_ENABLED is off'):
            call_command('sync_delivery_app', '--apply', stdout=mock.Mock())

    @override_settings(DELIVERY_APP_SYNC_ENABLED=True)
    def test_apply_reports_failures(self):
        with mock.patch.object(
            delivery_sync, 'get_delivery_client', return_value=FakeDeliveryClient()
        ):
            with mock.patch.object(
                delivery_sync, 'push_product', side_effect=RuntimeError('remote exploded')
            ):
                with self.assertRaisesMessage(CommandError, '1 product(s) failed'):
                    call_command('sync_delivery_app', '--apply', stdout=mock.Mock(), stderr=mock.Mock())
