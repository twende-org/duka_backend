"""Phase 2: unit history that survives, and who may retire a product.

The IMEI register is only worth having if a device's record outlives the product
row it was sold under. Three things keep that true, and each gets pushed on here:
``PROTECT`` on the FK, the API's soft delete instead of a hard one, and the
owner/manager gate in front of both.
"""
from decimal import Decimal

from django.db.models.deletion import ProtectedError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.products.models import Inventory, InventorySerialItem, Product
from apps.products.selectors import get_public_products, search_public_products
from apps.products.tests.helpers import add_staff, make_shop


def shelve(shop, product, branch, *imeis):
    """Put devices on one shop's shelf, the way registration would."""
    return InventorySerialItem.objects.bulk_create([
        InventorySerialItem(shop=shop, product=product, branch=branch, imei_number=imei)
        for imei in imeis
    ])


class SerialHistorySurvivesProductDeletionTests(TestCase):
    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Simu Duka')
        self.phone = Product.objects.create(
            shop=self.shop, name='Tecno Spark 20', requires_imei=True)

    def test_a_product_that_still_carries_units_cannot_be_hard_deleted(self):
        shelve(self.shop, self.phone, self.branch, '351234567890123')
        with self.assertRaises(ProtectedError):
            self.phone.delete()
        self.assertTrue(Product.objects.filter(pk=self.phone.pk).exists())
        self.assertEqual(InventorySerialItem.objects.filter(product=self.phone).count(), 1)

    def test_a_product_with_no_units_deletes_as_before(self):
        self.phone.delete()
        self.assertFalse(Product.objects.filter(pk=self.phone.pk).exists())

    def test_protection_holds_for_a_sold_unit_too(self):
        """The unit that matters most is the one already off the shelf."""
        unit, = shelve(self.shop, self.phone, self.branch, '351234567890124')
        unit.status = 'sold'
        unit.save(update_fields=['status'])
        with self.assertRaises(ProtectedError):
            self.phone.delete()


class ProductSoftDeleteApiTests(TestCase):
    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Simu Duka')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.phone = Product.objects.create(
            shop=self.shop, name='Infinix Hot 40', requires_imei=True,
            selling_price=Decimal('180000'), publish_to_directory=True)
        Inventory.objects.create(product=self.phone, branch=self.branch, quantity=2)
        shelve(self.shop, self.phone, self.branch, '359876543210987', '359876543210988')

    def _retire(self):
        return self.client.delete(f'/api/v1/products/{self.phone.id}/')

    def test_delete_retires_the_row_instead_of_erasing_it(self):
        response = self._retire()
        self.assertEqual(response.status_code, 204, response.data)
        self.phone.refresh_from_db()
        self.assertFalse(self.phone.is_active)
        # The history the row anchors is untouched.
        self.assertEqual(InventorySerialItem.objects.filter(product=self.phone).count(), 2)
        self.assertEqual(Inventory.objects.filter(product=self.phone).count(), 1)

    def test_a_retired_product_leaves_the_merchant_list_and_404s_on_detail(self):
        self._retire()
        listed = self.client.get(f'/api/v1/products/?shopId={self.shop.id}')
        ids = [row['id'] for row in listed.data['results']]
        self.assertNotIn(str(self.phone.id), ids)
        self.assertEqual(
            self.client.get(f'/api/v1/products/{self.phone.id}/').status_code, 404)

    def test_a_retired_product_leaves_the_storefront_and_its_search(self):
        self._retire()
        response = self.client.get(f'/api/v1/public/shops/{self.shop.id}/products/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(list(get_public_products(shop=self.shop)), [])
        self.assertEqual(list(search_public_products(query='Infinix')), [])

    def test_a_retired_product_cannot_be_rang_up_at_the_pos(self):
        self._retire()
        response = self.client.post('/api/v1/sales/', {
            'shopId': str(self.shop.id),
            'branchId': str(self.branch.id),
            'paymentMethod': 'cash',
            'items': [{'product_id': str(self.phone.id), 'quantity': 1, 'unit_price': '180000'}],
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('items', response.data)

    def test_the_serials_route_of_a_retired_product_is_gone_too(self):
        self._retire()
        self.assertEqual(
            self.client.get(f'/api/v1/products/{self.phone.id}/serials/').status_code, 404)


class ProductDeletionRoleTests(TestCase):
    """Only an owner or a manager may make a product disappear for everyone."""

    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Simu Duka')
        self.attendant = add_staff(self.shop, 'Simu Duka', 'attendant')
        self.phone = Product.objects.create(shop=self.shop, name='Itel A70')

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_an_attendant_cannot_delete_a_product(self):
        response = self._client(self.attendant).delete(f'/api/v1/products/{self.phone.id}/')
        self.assertEqual(response.status_code, 403, response.data)
        self.phone.refresh_from_db()
        self.assertTrue(self.phone.is_active)

    def test_an_attendant_cannot_deactivate_a_product_by_patching_the_flag(self):
        """The guard has to cover the flag, or deleting it is one PATCH away."""
        response = self._client(self.attendant).patch(
            f'/api/v1/products/{self.phone.id}/', {'is_active': False}, format='json')
        self.assertEqual(response.status_code, 403, response.data)
        self.phone.refresh_from_db()
        self.assertTrue(self.phone.is_active)

    def test_a_manager_can_retire_a_product(self):
        manager = add_staff(self.shop, 'Simu Duka', 'manager')
        response = self._client(manager).delete(f'/api/v1/products/{self.phone.id}/')
        self.assertEqual(response.status_code, 204, response.data)
        self.phone.refresh_from_db()
        self.assertFalse(self.phone.is_active)

    def test_an_attendant_still_edits_a_product_normally(self):
        response = self._client(self.attendant).patch(
            f'/api/v1/products/{self.phone.id}/', {'name': 'Itel A70 (refurb)'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.phone.refresh_from_db()
        self.assertEqual(self.phone.name, 'Itel A70 (refurb)')

    def test_an_attendant_sending_the_flag_back_unchanged_is_not_a_deactivation(self):
        """The app PUTs whole product objects back; a round-trip is not an edit."""
        response = self._client(self.attendant).patch(
            f'/api/v1/products/{self.phone.id}/',
            {'is_active': True, 'name': 'Itel A70 Plus'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.phone.refresh_from_db()
        self.assertEqual(self.phone.name, 'Itel A70 Plus')

    def test_an_attendant_cannot_see_another_shop_even_to_delete(self):
        other_shop, _, _ = make_shop('Duka Nyingine')
        foreign = Product.objects.create(shop=other_shop, name='Samsung A06')
        response = self._client(self.attendant).delete(f'/api/v1/products/{foreign.id}/')
        self.assertEqual(response.status_code, 404, response.data)
        foreign.refresh_from_db()
        self.assertTrue(foreign.is_active)
