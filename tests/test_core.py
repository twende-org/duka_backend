from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from google.api_core.exceptions import ServiceUnavailable
from rest_framework.test import APIClient
from rest_framework import status
# pyrefly: ignore [missing-import]
from apps.core import firestore_import as mapping
# pyrefly: ignore [missing-import]
from apps.core.management.commands import import_firestore as command
# pyrefly: ignore [missing-import]
from apps.users.models import User
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.shops.models import UserRole
# pyrefly: ignore [missing-import]
from apps.crm.models import Customer, Supplier

class AnalyticsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='admin', email='admin@test.com', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Test Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        
        self.customer = Customer.objects.create(shop=self.shop, name="John Doe", outstanding_balance=100.50)
        self.supplier = Supplier.objects.create(shop=self.shop, name="Acme Corp", outstanding_balance=300.00)

    def test_command_center_endpoint(self):
        url = reverse('command-center-list')
        response = self.client.get(f"{url}?shop_id={self.shop.id}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        data = response.json()
        
        # Verify structure
        self.assertIn('sales', data)
        self.assertIn('orderStats', data)
        self.assertIn('finance', data)
        self.assertIn('productInt', data)
        self.assertIn('customerInt', data)
        self.assertIn('supplierInt', data)

        # Verify finance sums based on setup
        self.assertEqual(data['finance']['customerDebt'], 100.50)
        self.assertEqual(data['finance']['supplierDebt'], 300.00)

    def test_command_center_no_shop(self):
        url = reverse('command-center-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class FirestoreMappingTests(SimpleTestCase):
    """The import mappers must coerce whatever Firestore holds, never abort."""

    def test_text_trims_and_truncates(self):
        self.assertEqual(mapping.text('  hi  '), 'hi')
        self.assertEqual(mapping.text(None), '')
        self.assertEqual(mapping.text('abcdef', 4), 'abcd')
        self.assertEqual(mapping.text(12), '12')

    def test_optional_text_empties_become_none(self):
        self.assertIsNone(mapping.optional_text(None))
        self.assertIsNone(mapping.optional_text('   '))
        self.assertEqual(mapping.optional_text(' ok '), 'ok')

    def test_decimal_value_rounds_clamps_and_defaults(self):
        self.assertEqual(mapping.decimal_value('10.555'), Decimal('10.56'))
        self.assertEqual(mapping.decimal_value('10.554'), Decimal('10.55'))
        self.assertEqual(mapping.decimal_value(-5), Decimal('0'))
        self.assertEqual(mapping.decimal_value('1e12'), mapping.MONEY_CEILING)
        self.assertEqual(mapping.decimal_value('nonsense'), Decimal('0'))
        self.assertEqual(mapping.decimal_value(None, default=Decimal('1')), Decimal('1'))
        self.assertEqual(mapping.decimal_value('1500.005', ceiling=mapping.RATE_CEILING),
                         mapping.RATE_CEILING)

    def test_int_value_floors_booleans_and_clamps(self):
        self.assertEqual(mapping.int_value('7'), 7)
        self.assertEqual(mapping.int_value('7.9'), 7)
        self.assertEqual(mapping.int_value(True), 0)  # bool is not a count
        self.assertEqual(mapping.int_value(0, default=1, minimum=1), 1)
        self.assertEqual(mapping.int_value(999, maximum=100), 100)
        self.assertEqual(mapping.int_value('x', default=3), 3)

    def test_float_bool_list_dict_coercion(self):
        self.assertEqual(mapping.float_value('2.5'), 2.5)
        self.assertIsNone(mapping.float_or_none(''))
        self.assertEqual(mapping.float_or_none('-1.2'), -1.2)
        self.assertTrue(mapping.bool_value(True))
        self.assertFalse(mapping.bool_value('true'))  # strings are not booleans
        self.assertEqual(mapping.list_value(('a', 'b')), ['a', 'b'])
        self.assertEqual(mapping.list_value('nope'), [])
        self.assertEqual(mapping.dict_value({'a': 1}), {'a': 1})
        self.assertEqual(mapping.dict_value([1]), {})

    def test_date_value_accepts_legacy_shapes(self):
        self.assertIsNone(mapping.date_value(''))
        self.assertIsNone(mapping.date_value('not a date'))
        self.assertEqual(mapping.date_value(date(2026, 1, 2)), date(2026, 1, 2))
        self.assertEqual(mapping.date_value(datetime(2026, 1, 2, 10, 30)), date(2026, 1, 2))
        self.assertEqual(mapping.date_value('2026-01-02T10:30:00Z'), date(2026, 1, 2))
        self.assertEqual(mapping.date_value('02/01/2026'), date(2026, 1, 2))
        self.assertEqual(mapping.date_value('02-01-2026'), date(2026, 1, 2))

    def test_datetime_value_parses_strings_and_makes_aware(self):
        aware = datetime(2026, 1, 2, 10, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(mapping.datetime_value(aware), aware)
        self.assertIsNone(mapping.datetime_value('garbage'))
        self.assertIsNone(mapping.datetime_value(''))
        parsed = mapping.datetime_value('2026-01-02T10:30:00Z')
        self.assertTrue(parsed.tzinfo is not None)
        self.assertEqual(parsed.astimezone(dt_timezone.utc), aware)

    def test_choice_value_falls_back(self):
        self.assertEqual(mapping.choice_value('ACTIVE', mapping.PRODUCT_STATUSES, 'active'), 'active')
        self.assertEqual(mapping.choice_value('bogus', mapping.PRODUCT_STATUSES, 'active'), 'active')
        self.assertEqual(mapping.choice_value(None, mapping.PRODUCT_CONDITIONS, None), None)
        self.assertEqual(mapping.role_value('Manager'), 'manager')
        self.assertEqual(mapping.role_value('bogus'), 'attendant')

    def test_unique_slug_dedupes_and_reserves(self):
        taken = {'my-shop'}
        self.assertEqual(mapping.unique_slug('My Shop', taken), 'my-shop-2')
        self.assertEqual(mapping.unique_slug('My Shop', taken), 'my-shop-3')
        self.assertEqual(mapping.unique_slug('', taken), 'shop')
        self.assertIn('my-shop-2', taken)

    def test_shop_kwargs_maps_legal_money_and_slug(self):
        kwargs = mapping.shop_kwargs('abc12345', {
            'name': 'Mama Lishe', 'slug': 'mama-lishe', 'whatsappNumber': '255712345678',
            'legal': {'tin': '123', 'licenseNumber': 'L-9'},
            'lat': '-6.8', 'lon': 'not-a-number',
            'productCategories': ['food'],
        })
        self.assertEqual(kwargs['name'], 'Mama Lishe')
        self.assertEqual(kwargs['slug'], 'mama-lishe')
        self.assertEqual(kwargs['whatsapp'], '255712345678')
        self.assertEqual(kwargs['tin_number'], '123')
        self.assertEqual(kwargs['license_number'], 'L-9')
        self.assertIsNone(kwargs['vrn_number'])
        self.assertEqual(kwargs['lat'], -6.8)
        self.assertIsNone(kwargs['lon'])
        self.assertEqual(kwargs['productCategories'], ['food'])
        self.assertEqual(kwargs['currency'], 'TZS')

    def test_shop_kwargs_survives_junk_values(self):
        kwargs = mapping.shop_kwargs('zzz', {'name': None, 'legal': 'nope',
                                             'shopTypes': 'nope', 'lat': True})
        self.assertEqual(kwargs['name'], 'Shop zzz')  # placeholder keeps NOT NULL happy
        self.assertIsNone(kwargs['tin_number'])
        self.assertEqual(kwargs['shopTypes'], [])
        self.assertIsNone(kwargs['lat'])

    def test_product_kwargs_maps_field_names(self):
        kwargs = mapping.product_kwargs('p1', {
            'name': 'Soda', 'categories': ['drinks'], 'category': 'Beverages',
            'buyingPrice': '1.25', 'sellingPrice': 2, 'taxRate': '18',
            'expiryDate': '', 'moq': 0, 'status': 'inactive',
            'tags': ['cold'], 'imageUrl': 'data:image/png;base64,AAA',
            'publishToFacebook': True, 'attributes': {'size': '1L'},
        }, shop='shop-obj', category='cat-obj')
        self.assertEqual(kwargs['name'], 'Soda')
        self.assertEqual(kwargs['marketplace_categories'], ['drinks'])
        self.assertEqual(kwargs['buying_price'], Decimal('1.25'))
        self.assertEqual(kwargs['selling_price'], Decimal('2'))
        self.assertEqual(kwargs['tax_rate'], Decimal('18'))
        self.assertIsNone(kwargs['expiry_date'])
        self.assertEqual(kwargs['moq'], 1)  # clamped up
        self.assertEqual(kwargs['status'], 'inactive')
        self.assertFalse(kwargs['is_active'])
        self.assertEqual(kwargs['tags'], ['cold'])
        self.assertEqual(kwargs['image_url'], 'data:image/png;base64,AAA')
        self.assertTrue(kwargs['publish_to_facebook'])
        self.assertEqual(kwargs['attributes'], {'size': '1L'})

    def test_product_kwargs_keeps_tags_shape(self):
        untouched = mapping.product_kwargs('p2', {}, None)
        self.assertEqual(untouched['tags'], '')  # the form posts '' when untouched
        self.assertEqual(untouched['status'], 'active')
        self.assertTrue(untouched['is_active'])

    def test_delivery_flag_defaults_on_when_firestore_field_is_absent(self):
        # deliverySync.js only skips the sync on an explicit false, so a missing
        # field means the product was published to the Delivery App.
        absent = mapping.product_kwargs('p3', {}, None)
        self.assertTrue(absent['publish_to_delivery_app'])
        explicit_off = mapping.product_kwargs('p4', {'publishToDeliveryApp': False}, None)
        self.assertFalse(explicit_off['publish_to_delivery_app'])

    def test_directory_flag_defaults_on_when_firestore_field_is_absent(self):
        # getProductsByShop reads `publishToDirectory !== false`, so a missing
        # field means the product is listed in the directory.
        absent = mapping.product_kwargs('p5', {}, None)
        self.assertTrue(absent['publish_to_directory'])
        explicit_off = mapping.product_kwargs('p6', {'publishToDirectory': False}, None)
        self.assertFalse(explicit_off['publish_to_directory'])

    def test_facebook_flag_stays_off_when_firestore_field_is_absent(self):
        # The auto-poster only fires on an explicit true (`=== true` in
        # facebookPost.js), so a missing field must not import as opted-in.
        absent = mapping.product_kwargs('p7', {}, None)
        self.assertFalse(absent['publish_to_facebook'])
        explicit_on = mapping.product_kwargs('p8', {'publishToFacebook': True}, None)
        self.assertTrue(explicit_on['publish_to_facebook'])

    def test_category_name_reads_display_string(self):
        self.assertEqual(mapping.category_name({'category': ' Beverages '}), 'Beverages')
        self.assertIsNone(mapping.category_name({'category': ''}))
        self.assertIsNone(mapping.category_name({}))

    def test_merchant_category_kwargs_slug_fallback(self):
        kwargs = mapping.merchant_category_kwargs('m1', {'name': 'Fresh Produce'}, shop=None)
        self.assertEqual(kwargs['slug'], 'fresh-produce')
        self.assertEqual(kwargs['status'], 'active')
        self.assertEqual(kwargs['sort_order'], 0)
        blank = mapping.merchant_category_kwargs('m2', {}, shop=None)
        self.assertTrue(blank['slug'])
        self.assertEqual(blank['name'], 'Category m2')

    def test_user_kwargs_uses_email_as_username(self):
        kwargs = mapping.user_kwargs('uid1', {'displayName': 'Amina', 'accountType': 'staff'}, 'a@x.co')
        self.assertEqual(kwargs['username'], 'a@x.co')
        self.assertEqual(kwargs['email'], 'a@x.co')
        self.assertEqual(kwargs['display_name'], 'Amina')
        self.assertEqual(kwargs['account_type'], 'staff')
        self.assertEqual(kwargs['firebase_uid'], 'uid1')

    def test_payment_method_value_normalizes_pos_spellings(self):
        self.assertEqual(mapping.payment_method_value('Taslimu'), 'cash')
        self.assertEqual(mapping.payment_method_value('  M-PESA '), 'mpesa')
        self.assertEqual(mapping.payment_method_value('Tigo Pesa'), 'tigopesa')
        self.assertEqual(mapping.payment_method_value('Airtel Money'), 'airtel_money')
        self.assertEqual(mapping.payment_method_value('Halopesa'), 'halopesa')
        self.assertEqual(mapping.payment_method_value('Benki'), 'bank')
        self.assertEqual(mapping.payment_method_value('Mkopo'), 'credit')
        self.assertEqual(mapping.payment_method_value('credit / debt'), 'credit')
        self.assertEqual(mapping.payment_method_value('airtel_money'), 'airtel_money')
        self.assertEqual(mapping.payment_method_value('Split'), 'split')  # unknown kept
        self.assertEqual(mapping.payment_method_value(None), 'cash')
        self.assertEqual(mapping.payment_method_value('', default='credit'), 'credit')

    def test_sale_status_value_only_draft_survives(self):
        self.assertEqual(mapping.sale_status_value('draft'), 'draft')
        self.assertEqual(mapping.sale_status_value('Draft'), 'draft')
        self.assertEqual(mapping.sale_status_value('completed'), 'completed')
        self.assertEqual(mapping.sale_status_value(None), 'completed')

    def test_order_status_value_passes_through(self):
        self.assertEqual(mapping.order_status_value('allocated'), 'allocated')
        self.assertEqual(mapping.order_status_value('In_Transit'), 'in_transit')
        self.assertEqual(mapping.order_status_value(''), 'pending')
        self.assertEqual(mapping.order_status_value(None, default='confirmed'), 'confirmed')

    def test_line_items_normalizes_modern_and_legacy_shapes(self):
        modern = mapping.line_items({'items': [
            {'productId': 'p1', 'productName': 'Soda', 'quantity': 2,
             'price': '800', 'subtotal': 1600, 'pickedQty': 1},
            'not-a-doc',
        ]})
        self.assertEqual(len(modern), 1)
        self.assertEqual(modern[0], {'productId': 'p1', 'productName': 'Soda', 'quantity': 2,
                                     'picked_qty': 1, 'unit_price': Decimal('800'),
                                     'subtotal': Decimal('1600')})

        legacy = mapping.line_items({'productId': 'p2', 'productName': 'Sugar',
                                     'quantity': 4, 'totalPrice': 10000})
        self.assertEqual(legacy[0]['quantity'], 4)
        self.assertEqual(legacy[0]['unit_price'], Decimal('2500'))  # totalPrice / quantity
        self.assertEqual(legacy[0]['subtotal'], Decimal('10000'))

        self.assertEqual(mapping.line_items({}), [])
        self.assertEqual(mapping.line_items({'items': 'nope'}), [])

    def test_line_items_fall_back_to_each_other(self):
        unit_only = mapping.line_items({'items': [{'productId': 'p', 'quantity': 2, 'unitPrice': '25'}]})[0]
        self.assertEqual(unit_only['subtotal'], Decimal('50'))
        subtotal_only = mapping.line_items({'items': [{'productId': 'p', 'quantity': 3, 'price': 10}]})[0]
        self.assertEqual(subtotal_only['subtotal'], Decimal('30'))
        zero_qty = mapping.line_items({'items': [{'productId': 'p', 'quantity': 0, 'totalPrice': 80}]})[0]
        self.assertEqual(zero_qty['quantity'], 1)  # never divides by zero
        self.assertEqual(zero_qty['unit_price'], Decimal('80'))

    def test_sale_and_order_item_kwargs_map_target_fields(self):
        item = {'productName': 'Soda', 'quantity': 2, 'picked_qty': 1,
                'unit_price': Decimal('800'), 'subtotal': Decimal('1600')}
        sale_item = mapping.sale_item_kwargs(item, product='prod')
        self.assertEqual(sale_item['product'], 'prod')
        self.assertEqual(sale_item['unit_price'], Decimal('800'))
        self.assertEqual(sale_item['total_price'], Decimal('1600'))
        order_item = mapping.order_item_kwargs(item, product='prod')
        self.assertEqual(order_item['picked_qty'], 1)
        self.assertEqual(order_item['subtotal'], Decimal('1600'))

    def test_branch_kwargs_maps_top_level_document(self):
        kwargs = mapping.branch_kwargs('b1', {
            'name': 'Kariakoo', 'location': 'Dar', 'phone': '0712000111',
            'isMain': True, 'isActive': False, 'type': 'Warehouse',
            'timezone': 'Africa/Dar_es_Salaam', 'features': {'acceptsPOS': True},
        }, shop='shop-obj', manager='mgr')
        self.assertEqual(kwargs['shop'], 'shop-obj')
        self.assertEqual(kwargs['name'], 'Kariakoo')
        self.assertTrue(kwargs['is_main'])
        self.assertFalse(kwargs['is_active'])
        self.assertEqual(kwargs['branch_type'], 'Warehouse')
        self.assertEqual(kwargs['manager'], 'mgr')
        self.assertEqual(kwargs['features'], {'acceptsPOS': True})

        blank = mapping.branch_kwargs('abcdefgh', {}, None)
        self.assertEqual(blank['name'], 'Branch abcdefgh')
        self.assertTrue(blank['is_active'])  # missing isActive defaults to live
        self.assertEqual(blank['branch_type'], 'Storefront')

    def test_customer_kwargs_balance_document_wins_for_money(self):
        kwargs = mapping.customer_kwargs('c1', {
            'name': 'Amina', 'phone': '0712345678', 'customerType': 'Wholesale',
            'businessName': 'Amina Ltd', 'totalPurchases': '9', 'totalSpent': '5000',
            'lastPurchaseDate': '2026-01-02', 'userId': 'uid-9', 'outstandingBalance': 111,
        }, shop='shop-obj', balance={'outstandingBalance': '250.5'})
        self.assertEqual(kwargs['customer_type'], 'wholesale')
        self.assertEqual(kwargs['outstanding_balance'], Decimal('250.50'))
        self.assertEqual(kwargs['total_purchases'], 9)
        self.assertEqual(kwargs['total_spent'], Decimal('5000'))
        self.assertEqual(kwargs['last_purchase_date'], date(2026, 1, 2))
        self.assertEqual(kwargs['user_id'], 'uid-9')  # Firebase uid kept verbatim

        blank = mapping.customer_kwargs('c2', {'customerType': 'bogus'}, None)
        self.assertEqual(blank['name'], 'Customer c2')
        self.assertEqual(blank['customer_type'], 'retail')
        self.assertIsNone(blank['user_id'])

    def test_sale_kwargs_maps_pos_document(self):
        kwargs = mapping.sale_kwargs({
            'totalPrice': '5000', 'discount': 200, 'paymentMethod': 'Taslimu',
            'customerId': 'c1', 'customerName': 'Amina', 'customerPhone': '0712345678',
            'notes': 'ok', 'status': 'draft',
        }, shop='s', branch='b', attendant='att', customer_user='u')
        self.assertEqual(kwargs['total_amount'], Decimal('5000'))
        self.assertEqual(kwargs['discount_amount'], Decimal('200'))
        self.assertEqual(kwargs['payment_method'], 'cash')
        self.assertEqual(kwargs['customer_id'], 'c1')
        self.assertEqual(kwargs['customer_user'], 'u')
        self.assertEqual(kwargs['status'], 'draft')

    def test_order_kwargs_maps_checkout_document(self):
        kwargs = mapping.order_kwargs({
            'idempotencyKey': 'k1', 'totalAmount': '1000', 'profitEstimate': 250,
            'status': 'allocated', 'source': 'public_storefront',
            'fulfillment': {'deliveryMethod': 'pickup'}, 'customerId': 'c1',
        }, shop='s', branch='b', customer_user='u')
        self.assertEqual(kwargs['idempotency_key'], 'k1')
        self.assertEqual(kwargs['status'], 'allocated')
        self.assertEqual(kwargs['source'], 'public_storefront')
        self.assertEqual(kwargs['profit_estimate'], Decimal('250'))
        self.assertEqual(kwargs['fulfillment_details'], {'deliveryMethod': 'pickup'})
        self.assertEqual(kwargs['customer_user'], 'u')

        blank = mapping.order_kwargs({}, 's', 'b')
        self.assertEqual(blank['payment_method'], 'Cash')
        self.assertEqual(blank['status'], 'pending')
        self.assertEqual(blank['source'], 'in_app')


class ImportStreamRetryTests(SimpleTestCase):
    """The importer reopens dropped Firestore streams instead of crashing."""

    class FakeReference:
        """``stream()`` replaying a scripted list of documents or exceptions."""

        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.calls = 0

        def stream(self):
            self.calls += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return iter(outcome)

    def test_transient_errors_are_recognized(self):
        self.assertTrue(command._is_transient_stream_error(ServiceUnavailable('drop')))
        self.assertTrue(command._is_transient_stream_error(
            AttributeError("'_UnaryStreamMultiCallable' object has no attribute '_retry'")))
        self.assertFalse(command._is_transient_stream_error(ValueError('boom')))

    def test_fetch_all_retries_then_returns_documents(self):
        reference = self.FakeReference([ServiceUnavailable('drop'), ['a', 'b']])
        with mock.patch.object(command.time, 'sleep'):
            self.assertEqual(command.fetch_all(reference), ['a', 'b'])
        self.assertEqual(reference.calls, 2)

    def test_fetch_all_gives_up_after_attempts(self):
        outcome = AttributeError("object has no attribute '_retry'")
        reference = self.FakeReference([outcome] * command.STREAM_ATTEMPTS)
        with mock.patch.object(command.time, 'sleep'):
            with self.assertRaises(AttributeError):
                command.fetch_all(reference)
        self.assertEqual(reference.calls, command.STREAM_ATTEMPTS)

    def test_fetch_all_does_not_retry_real_errors(self):
        reference = self.FakeReference([ValueError('boom'), ['a']])
        with self.assertRaises(ValueError):
            command.fetch_all(reference)
        self.assertEqual(reference.calls, 1)
