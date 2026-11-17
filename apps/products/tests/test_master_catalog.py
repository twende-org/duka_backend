"""Phase 1 of the phone & electronics niche: shared master catalog + IMEI units.

Covers the catalogue selector, the tap-to-add and serial-registration services,
their API endpoints, and — just as important — that nothing a shop does today
changed shape: the existing product write, the POS checkout and the storefront
order all still behave exactly as before.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from apps.products.models import (
    Inventory, InventoryMovement, InventorySerialItem, Product,
)
from apps.products.selectors import search_master_products
from apps.products.services import bulk_tap_to_add_products, register_serial_items
from apps.products.tests.helpers import catalog, make_shop
from apps.sales.models import Sale, SaleItem
from apps.shops.models import Shop
from apps.users.models import User


class MasterProductSelectorTests(TestCase):
    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Shop A')
        self.other_shop, _, _ = make_shop('Shop B')
        self.tecno = catalog('Spark 20', requires_imei=True)
        self.samsung = catalog('Galaxy A15', brand='Samsung')
        self.oraimo = catalog('18W Wall Charger', brand='Oraimo', category='Chargers & Cables')
        self.retired = catalog('Old Nokias', is_active=False)

    def test_browse_returns_only_active_rows(self):
        names = {row.name for row in search_master_products()}
        self.assertEqual(names, {'Spark 20', 'Galaxy A15', '18W Wall Charger'})

    def test_filters_by_niche_category_and_brand(self):
        self.assertEqual(
            [row.name for row in search_master_products(category_name='chargers & cables')],
            ['18W Wall Charger'],
        )
        self.assertEqual(
            [row.name for row in search_master_products(brand='samsung')],
            ['Galaxy A15'],
        )
        self.assertEqual(
            [row.name for row in search_master_products(niche='kitchen')],
            [],
        )

    def test_niches_scope_the_picker_and_fall_back_when_the_lane_is_empty(self):
        catalog('Rice 5kg', brand='Local', category='Groceries', niche='grocery')

        # A lane with data is honoured.
        self.assertEqual(
            {row.name for row in search_master_products(niches=['grocery'])},
            {'Rice 5kg'},
        )
        # Several lanes act as a union.
        self.assertEqual(
            {row.name for row in search_master_products(niches=['grocery', 'electronics'])},
            {'Spark 20', 'Galaxy A15', '18W Wall Charger', 'Rice 5kg'},
        )
        # A lane with nothing seeded would hand the merchant an empty picker,
        # so the filter drops and the whole catalogue shows instead.
        self.assertEqual(
            {row.name for row in search_master_products(niches=['kitchen'])},
            {'Spark 20', 'Galaxy A15', '18W Wall Charger', 'Rice 5kg'},
        )
        # No lanes at all means no scoping either.
        self.assertEqual(
            {row.name for row in search_master_products(niches=[])},
            {'Spark 20', 'Galaxy A15', '18W Wall Charger', 'Rice 5kg'},
        )

    def test_query_ranks_an_exact_barcode_and_name_prefix_first(self):
        self.samsung.barcode = '8806092000000'
        self.samsung.save(update_fields=['barcode'])
        ranked = [row.name for row in search_master_products(query='galaxy')]
        self.assertEqual(ranked[0], 'Galaxy A15')
        by_code = [row.name for row in search_master_products(query='8806092000000')]
        self.assertEqual(by_code, ['Galaxy A15'])

    def test_already_added_annotation(self):
        Product.objects.create(shop=self.shop, name='Spark 20', master_product=self.tecno)
        with_shop = {row.name: row.already_added for row in search_master_products(shop=self.shop)}
        self.assertTrue(with_shop['Spark 20'])
        self.assertFalse(with_shop['Galaxy A15'])
        # A catalogue row never links to Shop B just because Shop A carries it.
        without = {row.name: row.already_added for row in search_master_products(shop=self.other_shop)}
        self.assertFalse(without['Spark 20'])
        # No shop given still answers the key, so one payload shape serves both.
        plain = {row.name: row.already_added for row in search_master_products()}
        self.assertEqual(set(plain.values()), {False})


class TapToAddTests(TestCase):
    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Phone Corner')
        self.phone = catalog('Spark 20', requires_imei=True, default_warranty='12 months warranty',
                             barcode='6150000000001')
        self.cable = catalog('USB-C Cable 1m', brand='Oraimo', category='Chargers & Cables')

    def test_creates_shop_product_copying_identity_and_resolving_category(self):
        products = bulk_tap_to_add_products(
            self.shop, self.owner, self.branch,
            [{'master_product': self.phone, 'buying_price': Decimal('180000'),
              'selling_price': Decimal('210000'), 'quantity': 3}],
        )
        product = products[0]
        self.assertEqual(product.shop_id, self.shop.id)
        self.assertEqual(product.master_product_id, self.phone.id)
        self.assertEqual(product.name, 'Spark 20')
        self.assertEqual(product.brand, 'Tecno')
        self.assertEqual(product.barcode, '6150000000001')
        self.assertEqual(product.warranty, '12 months warranty')
        self.assertTrue(product.requires_imei)
        self.assertEqual(product.buying_price, Decimal('180000'))
        self.assertEqual(product.selling_price, Decimal('210000'))
        # Category lands in the shop's own per-shop taxonomy, not a global table.
        self.assertEqual(product.category.name, 'Smartphones')
        self.assertEqual(product.category.shop_id, self.shop.id)

    def test_stock_goes_through_the_movement_ledger(self):
        product = bulk_tap_to_add_products(
            self.shop, self.owner, self.branch,
            [{'master_product': self.phone, 'selling_price': Decimal('210000'), 'quantity': 4}],
        )[0]
        self.assertEqual(Inventory.objects.get(product=product, branch=self.branch).quantity, 4)
        movement = InventoryMovement.objects.get(product=product, branch=self.branch)
        self.assertEqual(movement.movement_type, 'in')
        self.assertEqual(movement.quantity_changed, 4)
        self.assertEqual(movement.new_qty, 4)
        self.assertEqual(movement.reason, 'Tap-to-Add Catalog Onboarding')
        self.assertEqual(movement.user_id, self.owner.id)

    def test_imei_numbers_become_in_stock_units_and_define_quantity(self):
        product = bulk_tap_to_add_products(
            self.shop, self.owner, self.branch,
            [{'master_product': self.phone, 'selling_price': Decimal('210000'),
              'imei_numbers': ['356938035643809', '356938035643810']}],
        )[0]
        serials = list(product.serial_items.order_by('imei_number'))
        self.assertEqual([s.imei_number for s in serials], ['356938035643809', '356938035643810'])
        self.assertEqual({s.status for s in serials}, {'in_stock'})
        self.assertEqual({s.shop_id for s in serials}, {self.shop.id})
        self.assertEqual({s.branch_id for s in serials}, {self.branch.id})
        self.assertIsNone(serials[0].sale_item_id)
        # Quantity was inferred from the scan, so shelf count and unit count match.
        self.assertEqual(Inventory.objects.get(product=product, branch=self.branch).quantity, 2)

    def test_quantity_that_disagrees_with_the_scan_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            bulk_tap_to_add_products(
                self.shop, self.owner, self.branch,
                [{'master_product': self.phone, 'quantity': 5,
                  'imei_numbers': ['356938035643809']}],
            )
        self.assertIn('does not match', str(ctx.exception))
        self.assertEqual(Product.objects.filter(shop=self.shop).count(), 0)
        self.assertEqual(InventorySerialItem.objects.count(), 0)

    def test_second_tap_refreshes_prices_instead_of_duplicating(self):
        payload = {'master_product': self.phone, 'buying_price': Decimal('180000'),
                   'selling_price': Decimal('210000'), 'quantity': 1}
        first = bulk_tap_to_add_products(self.shop, self.owner, self.branch, [payload])[0]
        # Merchant renames and re-prices by hand after the first add.
        first.name = 'Tecno Spark 20 (refurb)'
        first.brand = 'Changed'
        first.save(update_fields=['name', 'brand'])

        second = bulk_tap_to_add_products(self.shop, self.owner, self.branch, [{
            'master_product': self.phone, 'selling_price': Decimal('205000'), 'quantity': 2,
        }])[0]

        self.assertEqual(Product.objects.filter(shop=self.shop, master_product=self.phone).count(), 1)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.name, 'Tecno Spark 20 (refurb)')
        self.assertEqual(second.brand, 'Changed')
        self.assertEqual(second.selling_price, Decimal('205000'))
        # buying_price was not sent, so the merchant's cost survived the tap.
        self.assertEqual(second.buying_price, Decimal('180000'))
        self.assertEqual(Inventory.objects.get(product=second, branch=self.branch).quantity, 3)

    def test_duplicate_imei_in_the_same_batch_is_rejected_before_anything_is_written(self):
        with self.assertRaises(ValidationError) as ctx:
            bulk_tap_to_add_products(self.shop, self.owner, self.branch, [
                {'master_product': self.phone, 'imei_numbers': ['356938035643809']},
                {'master_product': self.cable, 'imei_numbers': ['356938035643809']},
            ])
        self.assertIn('cannot be added twice', str(ctx.exception))
        self.assertEqual(Product.objects.filter(shop=self.shop).count(), 0)
        self.assertEqual(InventorySerialItem.objects.count(), 0)

    def test_imei_already_on_the_shelf_is_rejected_and_rolls_back_the_batch(self):
        InventorySerialItem.objects.create(
            shop=self.shop, product=Product.objects.create(shop=self.shop, name='Old'),
            branch=self.branch, imei_number='356938035643809',
        )
        with self.assertRaises(ValidationError) as ctx:
            bulk_tap_to_add_products(self.shop, self.owner, self.branch, [
                {'master_product': self.cable, 'quantity': 2},
                {'master_product': self.phone, 'imei_numbers': ['356938035643809']},
            ])
        self.assertIn('Already in stock', str(ctx.exception))
        # The good line was rolled back too: the batch is all-or-nothing.
        self.assertFalse(Product.objects.filter(shop=self.shop, master_product=self.cable).exists())

    def test_the_same_imei_is_allowed_in_a_different_shop(self):
        other, other_branch, other_owner = make_shop('Rival Phones')
        bulk_tap_to_add_products(self.shop, self.owner, self.branch,
                                 [{'master_product': self.phone, 'imei_numbers': ['356938035643809']}])
        bulk_tap_to_add_products(other, other_owner, other_branch,
                                 [{'master_product': self.phone, 'imei_numbers': ['356938035643809']}])
        self.assertEqual(InventorySerialItem.objects.count(), 2)


class SerialRegistrationTests(TestCase):
    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Detox Motors')
        self.product = Product.objects.create(shop=self.shop, name='iPhone 13', requires_imei=True)
        Inventory.objects.create(product=self.product, branch=self.branch, quantity=1)

    def test_units_raise_stock_and_log_a_movement(self):
        created = register_serial_items(
            self.shop, self.product, self.branch, self.owner,
            [{'imei_number': '353008123456789'},
             {'imei_number': '353008123456790', 'serial_number': 'DNPX1234', 'notes': 'trade-in'}],
        )
        self.assertEqual(len(created), 2)
        self.assertEqual(Inventory.objects.get(product=self.product, branch=self.branch).quantity, 3)
        movement = InventoryMovement.objects.get(reason='Serial/IMEI registration')
        self.assertEqual(movement.quantity_changed, 2)
        self.assertEqual(movement.movement_type, 'in')

    def test_adding_stock_can_be_declined(self):
        register_serial_items(
            self.shop, self.product, self.branch, self.owner,
            [{'imei_number': '353008123456789'}], add_to_stock=False,
        )
        self.assertEqual(Inventory.objects.get(product=self.product, branch=self.branch).quantity, 1)

    def test_blank_numbers_and_foreign_products_are_rejected(self):
        with self.assertRaises(ValidationError):
            register_serial_items(self.shop, self.product, self.branch, self.owner,
                                  [{'imei_number': '', 'serial_number': ''}])
        other, _, _ = make_shop('Not Yours')
        with self.assertRaises(ValidationError):
            register_serial_items(other, self.product, None, self.owner,
                                  [{'imei_number': '353008123456789'}])

    def test_database_constraint_blocks_a_shelved_duplicate_even_bypassing_the_service(self):
        InventorySerialItem.objects.create(
            shop=self.shop, product=self.product, branch=self.branch, imei_number='9999888877776666')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InventorySerialItem.objects.create(
                    shop=self.shop, product=self.product, branch=self.branch,
                    imei_number='9999888877776666')

    def test_blank_imeis_do_not_collide_with_each_other(self):
        for serial in ('SN-AAA', 'SN-BBB'):
            InventorySerialItem.objects.create(
                shop=self.shop, product=self.product, branch=self.branch,
                imei_number='', serial_number=serial)
        self.assertEqual(InventorySerialItem.objects.filter(imei_number='').count(), 2)


class CatalogApiTests(TestCase):
    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Kariakoo Gadgets')
        self.outsider = User.objects.create_user(username='outsider', email='out@example.com')
        self.phone = catalog('Spark 20', requires_imei=True)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_endpoints_require_authentication(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.get('/api/v1/master-products/').status_code, 401)
        self.assertEqual(
            anonymous.post('/api/v1/products/tap-to-add/', {'shopId': 'x', 'items': []},
                           format='json').status_code, 401)

    def test_catalog_search_payload_shape(self):
        Product.objects.create(shop=self.shop, name='Spark 20', master_product=self.phone)
        response = self.client.get('/api/v1/master-products/', {'q': 'spark', 'shopId': str(self.shop.id)})
        self.assertEqual(response.status_code, 200)
        row = response.data['results'][0]
        for key in ('id', 'name', 'brand', 'categoryName', 'barcode', 'imageUrl',
                    'requiresImei', 'defaultWarranty', 'alreadyAdded'):
            self.assertIn(key, row)
        self.assertTrue(row['alreadyAdded'])
        self.assertEqual(response.data['count'], 1)

    def test_catalog_search_ignores_a_foreign_shops_already_added_flag(self):
        # Knowing which rows another merchant already carries is their data.
        response = self.client.get(
            '/api/v1/master-products/', {'shopId': str(Shop.objects.create(name='Elsewhere').id)})
        self.assertEqual(response.status_code, 403)

    def test_catalog_niches_param_scopes_and_falls_back(self):
        catalog('Rice 5kg', brand='Local', category='Groceries', niche='grocery')
        shop_id = str(self.shop.id)

        # A comma-separated param scopes to the named lanes.
        response = self.client.get('/api/v1/master-products/',
                                   {'shopId': shop_id, 'niches': 'grocery'})
        self.assertEqual([row['name'] for row in response.data['results']], ['Rice 5kg'])

        # Repeated params and commas both work, and a lane with no seeded rows
        # falls back to the full catalogue instead of an empty picker.
        response = self.client.get('/api/v1/master-products/',
                                   {'shopId': shop_id, 'niches': ['kitchen', 'phone']})
        names = {row['name'] for row in response.data['results']}
        self.assertEqual(names, {'Spark 20', 'Rice 5kg'})

    def test_tap_to_add_returns_created_products(self):
        response = self.client.post('/api/v1/products/tap-to-add/', {
            'shopId': str(self.shop.id),
            'items': [{
                'masterProductId': str(self.phone.id),
                'buyingPrice': '180000.00',
                'sellingPrice': '215000.00',
                'imeiNumbers': ['356938035643809', '356938035643810'],
            }],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        product = response.data['products'][0]
        self.assertEqual(product['name'], 'Spark 20')
        self.assertTrue(product['requiresImei'])
        self.assertEqual(str(product['masterProduct']), str(self.phone.id))
        self.assertEqual(product['buyingPrice'], '180000.00')
        self.assertEqual(
            Inventory.objects.get(product_id=product['id'], branch=self.branch).quantity, 2)

    def test_tap_to_add_accepts_a_legacy_shop_id_and_rejects_another_shops_product(self):
        self.shop.legacy_id = 'FS-SHOP-001'
        self.shop.save(update_fields=['legacy_id'])
        response = self.client.post('/api/v1/products/tap-to-add/', {
            'shopId': 'FS-SHOP-001',
            'items': [{'masterProductId': str(self.phone.id), 'quantity': 1}],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)

        # A payload naming someone else's branch cannot reach their stock.
        other, other_branch, _ = make_shop('Rival')
        blocked = self.client.post('/api/v1/products/tap-to-add/', {
            'shopId': str(self.shop.id),
            'branchId': str(other_branch.id),
            'items': [{'masterProductId': str(self.phone.id), 'quantity': 1}],
        }, format='json')
        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertEqual(Inventory.objects.filter(branch=other_branch).count(), 0)
        self.assertEqual(Product.objects.filter(shop=other).count(), 0)

    def test_tap_to_add_rejects_an_unknown_catalog_id(self):
        response = self.client.post('/api/v1/products/tap-to-add/', {
            'shopId': str(self.shop.id),
            'items': [{'masterProductId': '00000000-0000-0000-0000-000000000000', 'quantity': 1}],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_serials_list_and_bulk_add_by_status(self):
        product = Product.objects.create(shop=self.shop, name='iPhone 13')
        add = self.client.post(
            f'/api/v1/products/{product.id}/serials/',
            {'imeiNumbers': ['111111111111111', '222222222222222']}, format='json')
        self.assertEqual(add.status_code, 201, add.data)
        self.assertEqual(len(add.data), 2)
        self.assertEqual(add.data[0]['imeiNumber'], '111111111111111')

        listed = self.client.get(f'/api/v1/products/{product.id}/serials/', {'unitStatus': 'in_stock'})
        self.assertEqual(listed.data['count'], 2)
        empty = self.client.get(f'/api/v1/products/{product.id}/serials/', {'unitStatus': 'sold'})
        self.assertEqual(empty.data['count'], 0)
        bad = self.client.get(f'/api/v1/products/{product.id}/serials/', {'unitStatus': 'melted'})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(Inventory.objects.get(product=product, branch=self.branch).quantity, 2)

    def test_the_products_list_status_filter_still_filters_products(self):
        """``?status=`` belongs to the product resource; the unit filter does not."""
        product = Product.objects.create(shop=self.shop, name='Active One')
        Product.objects.create(shop=self.shop, name='Retired One', status='inactive')
        response = self.client.get(
            '/api/v1/products/', {'shopId': str(self.shop.id), 'status': 'active'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row['name'] for row in response.data['results']}, {'Active One'})

    def test_serials_of_another_shops_product_are_not_visible(self):
        product = Product.objects.create(shop=self.shop, name='iPhone 13')
        InventorySerialItem.objects.create(
            shop=self.shop, product=product, branch=self.branch, imei_number='111111111111111')
        stranger = APIClient()
        other, _, other_owner = make_shop('Other Shop')
        stranger.force_authenticate(other_owner)
        self.assertEqual(stranger.get(f'/api/v1/products/{product.id}/serials/').status_code, 404)
        self.assertEqual(
            stranger.post(f'/api/v1/products/{product.id}/serials/',
                          {'imeiNumbers': ['999999999999999']}, format='json').status_code, 404)


class BackwardCompatibilityTests(TestCase):
    """Phase 1 must not move anything an existing shop already depends on."""

    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Ndugu Duka')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_plain_product_creation_still_works_and_is_unlinked(self):
        response = self.client.post('/api/v1/products/', {
            'shopId': str(self.shop.id), 'name': 'Sukuma',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data['masterProduct'])
        self.assertFalse(response.data['requiresImei'])

    def test_a_generic_patch_cannot_repoint_the_catalog_link(self):
        product = Product.objects.create(shop=self.shop, name='iPhone 12')
        linked = catalog('iPhone 13')
        response = self.client.patch(f'/api/v1/products/{product.id}/', {
            'master_product': str(linked.id),
            'masterProduct': str(linked.id),
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        product.refresh_from_db()
        self.assertIsNone(product.master_product_id)

    def test_pos_sale_still_deducts_stock_and_writes_sale_items(self):
        product = Product.objects.create(shop=self.shop, name='Charger', selling_price=Decimal('5000'))
        Inventory.objects.create(product=product, branch=self.branch, quantity=5)
        response = self.client.post('/api/v1/sales/', {
            'shopId': str(self.shop.id),
            'branchId': str(self.branch.id),
            'paymentMethod': 'cash',
            'items': [{'product_id': str(product.id), 'quantity': 2, 'unit_price': '5000'}],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        sale = Sale.objects.get(pk=response.data['id'])
        self.assertEqual(SaleItem.objects.filter(sale=sale).count(), 1)
        self.assertEqual(SaleItem.objects.get(sale=sale).quantity, 2)
        self.assertEqual(Inventory.objects.get(product=product, branch=self.branch).quantity, 3)
        self.assertEqual(sale.total_amount, Decimal('10000'))
        # A line that names no devices still links no units: checkout only claims
        # what the cashier scanned, so an untracked sale is byte-for-byte the old one.
        self.assertEqual(InventorySerialItem.objects.filter(sale_item__sale=sale).count(), 0)

    def test_storefront_order_still_creates_without_touching_stock(self):
        shop = self.shop
        shop.setupStatus = 'completed'
        shop.save(update_fields=['setupStatus'])
        product = Product.objects.create(
            shop=shop, name='Published Phone', selling_price=Decimal('200000'),
            publish_to_directory=True)
        Inventory.objects.create(product=product, branch=self.branch, quantity=4)

        response = self.client.post(
            f'/api/v1/public/shops/{shop.id}/orders/',
            {'orderId': 'ORD-900101', 'customerName': 'Juma', 'customerPhone': '0711111111',
             'items': [{'productId': str(product.id), 'quantity': 1}]},
            format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Inventory.objects.get(product=product, branch=self.branch).quantity, 4)

    def test_existing_products_are_untouched_by_the_new_columns(self):
        Product.objects.create(shop=self.shop, name='Legacy Row')
        product = Product.objects.get(name='Legacy Row')
        self.assertIsNone(product.master_product_id)
        self.assertFalse(product.requires_imei)
