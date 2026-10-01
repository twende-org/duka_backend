"""Customer-portal reads: identity resolution and legacy payload shapes.

The portal is cross-shop by design, so these tests pin down *who* sees a row —
the account FK, CRM customer rows (claimed or phone-matched), stored phone
spellings — and that the JSON keeps the Firestore field names the pages read.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.crm.models import Customer
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.sales.models import Order, OrderItem, Sale, SaleItem
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop
# pyrefly: ignore [missing-import]
from apps.users.models import User


class PortalTestBase(TestCase):
    """A buyer with one shop of history, plus an unrelated second customer."""

    def setUp(self):
        self.client = APIClient()
        self.buyer = User.objects.create_user(
            username='buyer', email='buyer@test.com', password='password', phone='0712345678')
        self.client.force_authenticate(user=self.buyer)

        self.shop = Shop.objects.create(name='Duka A')
        self.branch = Branch.objects.create(shop=self.shop, name='Main', is_main=True)
        self.other_shop = Shop.objects.create(name='Duka B')
        self.other_branch = Branch.objects.create(shop=self.other_shop, name='Main', is_main=True)

        self.other = User.objects.create_user(
            username='other', email='other@test.com', password='password', phone='0655999999')

    def make_sale(self, shop=None, branch=None, **overrides):
        data = dict(shop=shop or self.shop, branch=branch or self.branch,
                    payment_method='cash', total_amount=Decimal('5000.00'), status='completed')
        data.update(overrides)
        return Sale.objects.create(**data)

    def make_order(self, shop=None, branch=None, **overrides):
        data = dict(shop=shop or self.shop, branch=branch or self.branch,
                    total_amount=Decimal('12000.00'), status='pending')
        data.update(overrides)
        return Order.objects.create(**data)

    def receipt_ids(self):
        response = self.client.get('/api/v1/portal/receipts/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row['id'] for row in response.json()['results']]

    def order_ids(self):
        response = self.client.get('/api/v1/portal/orders/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row['id'] for row in response.json()['results']]


class PortalReceiptIdentityTests(PortalTestBase):
    def test_receipt_matches_on_account_fk(self):
        sale = self.make_sale(customer_user=self.buyer)
        self.assertEqual(self.receipt_ids(), [str(sale.pk)])

    def test_receipt_matches_on_customer_legacy_id(self):
        Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                user_id=str(self.buyer.pk), legacy_id='cust-1')
        sale = self.make_sale(customer_id='cust-1')
        self.assertEqual(self.receipt_ids(), [str(sale.pk)])

    def test_receipt_matches_on_firebase_uid_link(self):
        self.buyer.firebase_uid = 'fb-uid-1'
        self.buyer.save(update_fields=['firebase_uid'])
        Customer.objects.create(shop=self.shop, name='Buyer', user_id='fb-uid-1',
                                legacy_id='cust-fb')
        sale = self.make_sale(customer_id='cust-fb')
        self.assertEqual(self.receipt_ids(), [str(sale.pk)])

    def test_receipt_matches_on_django_uuid_customer_id(self):
        customer = Customer.objects.create(shop=self.shop, name='Buyer',
                                           user_id=str(self.buyer.pk), legacy_id='cust-2')
        sale = self.make_sale(customer_id=str(customer.pk))
        self.assertEqual(self.receipt_ids(), [str(sale.pk)])

    def test_receipt_matches_on_every_phone_spelling(self):
        for phone in ('0712345678', '255712345678', '+255712345678'):
            sale = self.make_sale(customer_phone=phone)
            self.assertIn(str(sale.pk), self.receipt_ids())

    def test_unclaimed_customer_row_is_claimed_by_phone(self):
        Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                user_id='', legacy_id='cust-3')
        sale = self.make_sale(customer_id='cust-3')
        self.assertEqual(self.receipt_ids(), [str(sale.pk)])

    def test_customer_row_claimed_by_someone_else_stays_hidden(self):
        Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                user_id=str(self.other.pk), legacy_id='cust-4')
        sale = self.make_sale(customer_id='cust-4', customer_phone='')
        self.assertEqual(self.receipt_ids(), [])

    def test_drafts_are_not_receipts(self):
        draft = self.make_sale(customer_user=self.buyer, status='draft')
        completed = self.make_sale(customer_user=self.buyer)
        self.assertEqual(self.receipt_ids(), [str(completed.pk)])
        self.assertNotIn(str(draft.pk), self.receipt_ids())

    def test_receipts_span_every_shop(self):
        first = self.make_sale(customer_user=self.buyer)
        second = self.make_sale(shop=self.other_shop, branch=self.other_branch,
                                customer_user=self.buyer)
        self.assertCountEqual(self.receipt_ids(), [str(first.pk), str(second.pk)])

    def test_other_customers_receipts_stay_hidden(self):
        self.make_sale(customer_phone='0655999999', customer_id='someone-else',
                       customer_user=self.other)
        self.make_sale(shop=self.other_shop, branch=self.other_branch)
        self.assertEqual(self.receipt_ids(), [])

    def test_receipt_payload_keeps_firestore_shape(self):
        product = Product.objects.create(shop=self.shop, name='Soda',
                                         buying_price=Decimal('500'), selling_price=Decimal('800'))
        sale = self.make_sale(customer_user=self.buyer, legacy_id='fs-sale-1',
                              payment_method='mpesa', total_amount=Decimal('1600.00'))
        SaleItem.objects.create(sale=sale, product=product, quantity=2,
                                unit_price=Decimal('800.00'), total_price=Decimal('1600.00'))

        row = self.client.get('/api/v1/portal/receipts/').json()['results'][0]
        self.assertEqual(row['id'], 'fs-sale-1')  # legacy id first
        self.assertEqual(row['shopId'], str(self.shop.pk))
        self.assertEqual(row['shopName'], 'Duka A')
        self.assertEqual(row['total'], 1600.0)
        self.assertEqual(row['paymentMethod'], 'mpesa')
        self.assertEqual(row['itemsCount'], 1)
        self.assertRegex(row['date'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertIn('createdAt', row)
        item = row['items'][0]
        self.assertEqual(item['productId'], str(product.pk))
        self.assertEqual(item['productName'], product.name)
        self.assertEqual(item['quantity'], 2)
        self.assertEqual(item['price'], 800.0)
        self.assertEqual(item['subtotal'], 1600.0)

    def test_receipt_item_survives_deleted_product(self):
        sale = self.make_sale(customer_user=self.buyer)
        SaleItem.objects.create(sale=sale, product=None, product_name='Gone Item',
                                quantity=1, unit_price=Decimal('100'), total_price=Decimal('100'))
        item = self.client.get('/api/v1/portal/receipts/').json()['results'][0]['items'][0]
        self.assertIsNone(item['productId'])
        self.assertEqual(item['productName'], 'Gone Item')


class PortalOrderIdentityTests(PortalTestBase):
    def test_order_matches_on_account_fk(self):
        order = self.make_order(customer_user=self.buyer)
        self.assertEqual(self.order_ids(), [str(order.pk)])

    def test_order_matches_on_customer_legacy_id(self):
        Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                user_id=str(self.buyer.pk), legacy_id='cust-1')
        order = self.make_order(customer_id='cust-1')
        self.assertEqual(self.order_ids(), [str(order.pk)])

    def test_phone_alone_does_not_surface_orders(self):
        """Guest checkouts carry no ``customerUserId``; phone matching would leak them."""
        self.make_order(customer_phone='0712345678')
        self.assertEqual(self.order_ids(), [])

    def test_orders_span_every_shop(self):
        first = self.make_order(customer_user=self.buyer)
        second = self.make_order(shop=self.other_shop, branch=self.other_branch,
                                 customer_user=self.buyer)
        self.assertCountEqual(self.order_ids(), [str(first.pk), str(second.pk)])

    def test_other_customers_orders_stay_hidden(self):
        self.make_order(customer_user=self.other, customer_id='someone-else',
                        customer_phone='0655999999')
        self.assertEqual(self.order_ids(), [])

    def test_order_payload_keeps_firestore_shape(self):
        order = self.make_order(customer_user=self.buyer, legacy_id='fs-order-1',
                                status='confirmed', total_amount=Decimal('9999.00'),
                                fulfillment_details={'deliveryMethod': 'pickup'})
        OrderItem.objects.create(order=order, quantity=3, unit_price=Decimal('3333.00'),
                                 subtotal=Decimal('9999.00'), product_name='Crate')

        row = self.client.get('/api/v1/portal/orders/').json()['results'][0]
        self.assertEqual(row['id'], 'fs-order-1')
        self.assertEqual(row['orderId'], 'fs-order-1')
        self.assertEqual(row['shopId'], str(self.shop.pk))
        self.assertEqual(row['shopName'], 'Duka A')
        self.assertEqual(row['totalAmount'], 9999.0)
        self.assertEqual(row['status'], 'confirmed')
        self.assertEqual(row['fulfillment'], {'deliveryMethod': 'pickup'})
        self.assertIn('createdAt', row)
        item = row['items'][0]
        self.assertIsNone(item['productId'])
        self.assertEqual(item['productName'], 'Crate')
        self.assertEqual(item['subtotal'], 9999.0)


class PortalAccessTests(TestCase):
    def test_endpoints_require_authentication(self):
        client = APIClient()
        for url in ('/api/v1/portal/receipts/', '/api/v1/portal/orders/'):
            response = client.get(url)
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
