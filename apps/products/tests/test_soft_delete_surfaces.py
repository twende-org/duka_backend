"""A soft-deleted product must behave exactly like the Firestore row it replaced.

The API delete is ``is_active=False``, so every surface that used to lose the
document outright has to lose it here too: the B2B portal, the storefront
checkout, the auto-posters, imports, the stock shelf and the pickers — while
receipts, the ledger and the IMEI register keep their link. These tests pin
each surface; the audit-trail side lives in ``test_serial_protection``.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.products.models import Inventory, Product
from apps.products.selectors import search_master_products
from apps.products.services import bulk_tap_to_add_products
from apps.products.tests.helpers import catalog, make_shop
from apps.sales.services import create_b2b_order, create_storefront_order
from apps.social.tasks import next_marketing_product, next_product_for_shop


class SoftDeleteSurfaceTests(TestCase):
    """Deleting a product removes it from every active surface, nothing else."""

    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Ndugu Duka')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def delete_product(self, product):
        response = self.client.delete(f'/api/v1/products/{product.id}/')
        self.assertEqual(response.status_code, 204, response.data)
        product.refresh_from_db()
        return product

    def test_a_storefront_order_for_a_deleted_product_uses_the_snapshot_price(self):
        product = Product.objects.create(
            shop=self.shop, name='Published Phone', selling_price=Decimal('200000'),
            publish_to_directory=True)
        self.delete_product(product)

        response = self.client.post(
            f'/api/v1/public/shops/{self.shop.id}/orders/',
            {'orderId': 'ORD-900102', 'customerName': 'Juma', 'customerPhone': '0711111111',
             'items': [{'productId': str(product.id), 'quantity': 1,
                        'price': '150000', 'productName': 'Published Phone'}]},
            format='json')
        self.assertEqual(response.status_code, 201, response.data)
        item = response.data['items'][0]
        self.assertEqual(Decimal(str(item['unitPrice'])), Decimal('150000'))

    def test_a_storefront_order_for_a_deleted_product_without_a_price_is_refused(self):
        product = Product.objects.create(
            shop=self.shop, name='Published Phone', selling_price=Decimal('200000'))
        self.delete_product(product)

        response = self.client.post(
            f'/api/v1/public/shops/{self.shop.id}/orders/',
            {'orderId': 'ORD-900103', 'customerName': 'Juma', 'customerPhone': '0711111111',
             'items': [{'productId': str(product.id), 'quantity': 1}]},
            format='json')
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_deleted_product_cannot_be_ordered_through_the_b2b_portal(self):
        product = Product.objects.create(shop=self.shop, name='Crate Soda')
        self.delete_product(product)

        with self.assertRaisesMessage(ValidationError, 'does not exist'):
            create_b2b_order(
                shop=self.shop, branch=self.branch,
                items_data=[{'product_id': str(product.id), 'quantity': 1, 'unit_price': '1000'}],
                idempotency_key='B2B-9001')
        self.assertEqual(Product.objects.filter(name='Crate Soda').count(), 1)

    def test_a_second_tap_after_a_delete_starts_a_fresh_row(self):
        master = catalog('iPhone 13')
        original = bulk_tap_to_add_products(
            shop=self.shop, user=self.owner, branch=self.branch,
            items_payload=[{'master_product': master, 'selling_price': Decimal('900000')}])[0]
        self.delete_product(original)

        republished = bulk_tap_to_add_products(
            shop=self.shop, user=self.owner, branch=self.branch,
            items_payload=[{'master_product': master, 'selling_price': Decimal('950000')}])
        self.assertEqual(len(republished), 1)
        self.assertNotEqual(republished[0].pk, original.pk)
        self.assertTrue(republished[0].is_active)
        original.refresh_from_db()
        self.assertFalse(original.is_active)

    def test_the_catalog_picker_offers_a_deleted_product_again(self):
        master = catalog('Samsung A54')
        Product.objects.create(shop=self.shop, master_product=master, name='Samsung A54')
        rows = {row.name: row for row in search_master_products(query='', shop=self.shop)}
        self.assertTrue(rows['Samsung A54'].already_added)

        Product.objects.filter(name='Samsung A54').update(is_active=False)
        rows = {row.name: row for row in search_master_products(query='', shop=self.shop)}
        self.assertFalse(rows['Samsung A54'].already_added)

    def test_the_stock_shelf_hides_a_deleted_products_rows(self):
        kept = Product.objects.create(shop=self.shop, name='Kept Stock')
        gone = Product.objects.create(shop=self.shop, name='Deleted Stock')
        Inventory.objects.create(product=kept, branch=self.branch, quantity=4)
        Inventory.objects.create(product=gone, branch=self.branch, quantity=9)
        self.delete_product(gone)

        response = self.client.get('/api/v1/inventory/')
        self.assertEqual(response.status_code, 200, response.data)
        stocked = {str(row['product']) for row in response.data['results']}
        self.assertIn(str(kept.id), stocked)
        self.assertNotIn(str(gone.id), stocked)

    def test_the_adjust_endpoint_404s_a_deleted_product(self):
        product = Product.objects.create(shop=self.shop, name='Retired Charger')
        Inventory.objects.create(product=product, branch=self.branch, quantity=2)
        self.delete_product(product)

        response = self.client.post('/api/v1/inventory-movements/adjust/', {
            'productId': str(product.id), 'quantity': 5,
        }, format='json')
        self.assertEqual(response.status_code, 404, response.data)

    def test_stock_cannot_be_created_for_a_deleted_product(self):
        product = Product.objects.create(shop=self.shop, name='Retired Charger')
        self.delete_product(product)

        response = self.client.post('/api/v1/inventory/', {
            'productId': str(product.id), 'branchId': str(self.branch.id), 'quantity': 2,
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)

    def test_the_shop_dashboard_counts_only_live_products(self):
        Product.objects.create(shop=self.shop, name='Live One')
        gone = Product.objects.create(shop=self.shop, name='Counted Out')
        self.delete_product(gone)

        response = self.client.get(f'/api/v1/shops/{self.shop.id}/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['productCount'], 1)

    def test_deleting_unpublishes_every_channel_the_merchant_opted_into(self):
        product = Product.objects.create(
            shop=self.shop, name='Broadcast Phone',
            publish_to_directory=True, publish_to_facebook=True,
            publish_to_delivery_app=True)
        self.delete_product(product)
        self.assertFalse(product.publish_to_directory)
        self.assertFalse(product.publish_to_facebook)
        self.assertFalse(product.publish_to_delivery_app)


class SoftDeleteSchedulerTests(TestCase):
    """The auto-posters must never resurrect a product the merchant removed."""

    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Ndugu Duka')

    def test_the_facebook_scheduler_skips_a_deleted_product(self):
        kept = Product.objects.create(shop=self.shop, name='Live Phone', image_url='https://x/y.jpg')
        gone = Product.objects.create(
            shop=self.shop, name='Retired Phone', image_url='https://x/z.jpg')
        Inventory.objects.create(product=kept, branch=self.branch, quantity=2)
        Inventory.objects.create(product=gone, branch=self.branch, quantity=2)
        Product.objects.filter(pk=gone.pk).update(is_active=False)

        self.assertEqual(next_product_for_shop(self.shop), kept)

    def test_the_marketing_drip_skips_a_deleted_product(self):
        kept = Product.objects.create(
            shop=self.shop, name='Live Reel', image_url='https://x/y.jpg',
            publish_to_facebook=True)
        gone = Product.objects.create(
            shop=self.shop, name='Retired Reel', image_url='https://x/z.jpg',
            publish_to_facebook=True)
        Product.objects.filter(pk=gone.pk).update(is_active=False)

        self.assertEqual(next_marketing_product(self.shop), kept)

    def test_a_shop_with_only_deleted_products_posts_nothing(self):
        gone = Product.objects.create(
            shop=self.shop, name='Retired Reel', image_url='https://x/z.jpg',
            publish_to_facebook=True)
        Inventory.objects.create(product=gone, branch=self.branch, quantity=2)
        Product.objects.filter(pk=gone.pk).update(is_active=False)

        self.assertIsNone(next_product_for_shop(self.shop))
        self.assertIsNone(next_marketing_product(self.shop))
