"""Order creation parity with Firestore's ``createOrder`` callable.

The wizard posts a camelCase payload built in ``Orders.tsx`` /
``CreateOrderWizardV2.tsx``: blank ``branchId``, item ``price`` (tier-derived),
``fulfillment``, plus keys Django ignores (``subtotal``, ``createdBy``…). These
tests pin the accepted contract and the legacy fallbacks: pickup fulfilment,
``Unknown`` customer name, ``retail`` tier, ``createdBy`` salesperson.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.crm.models import Customer
# pyrefly: ignore [missing-import]
from apps.products.models import Inventory, Product
# pyrefly: ignore [missing-import]
from apps.sales.models import Order
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.users.models import User

ORDERS_URL = '/api/v1/orders/'


class OrderParityBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='merchant', email='merchant@test.com', password='password')
        self.client.force_authenticate(user=self.user)

        self.shop = Shop.objects.create(name='Duka A')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.main_branch = Branch.objects.create(shop=self.shop, name='Main', is_main=True)
        self.other_branch = Branch.objects.create(shop=self.shop, name='Kariakoo')

        self.product = Product.objects.create(
            shop=self.shop, name='Crate', buying_price=Decimal('1000.00'),
            selling_price=Decimal('1500.00'))
        Inventory.objects.create(product=self.product, branch=self.main_branch, quantity=50)

    def wizard_payload(self, **overrides):
        """The exact shape ``Orders.tsx`` builds for the createOrder callable."""
        payload = {
            'shopId': str(self.shop.pk),
            'branchId': '',
            'items': [{
                'productId': str(self.product.pk),
                'productName': 'Crate',
                'quantity': 2,
                'price': 1500.0,
                'subtotal': 3000.0,
            }],
            'subtotal': 3000.0,
            'tax': 0,
            'discount': 0,
            'totalAmount': 3000.0,
            'status': 'pending',
            'approvalStatus': 'approved',
            'paymentMethod': 'Cash',
            'customerName': None,
            'customerPhone': None,
            'customerId': None,
            'customerType': None,
            'customerPoNumber': None,
            'requiredDeliveryDate': '',
            'salespersonId': None,
            'internalNotes': None,
            'notes': None,
            'profitEstimate': 1000.0,
            'createdAt': '2026-09-01T10:00:00.000Z',
            'createdBy': str(self.user.pk),
            'createdByName': 'Merchant',
            'fulfillment': {'deliveryMethod': 'pickup'},
            'source': 'in_app',
            'idempotencyKey': 'wizard-key-1',
        }
        payload.update(overrides)
        return payload

    def post(self, payload):
        return self.client.post(ORDERS_URL, payload, format='json')


class WizardPayloadTests(OrderParityBase):
    def test_wizard_payload_is_accepted(self):
        response = self.post(self.wizard_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = Order.objects.get()
        self.assertEqual(order.total_amount, Decimal('3000.00'))
        self.assertEqual(order.profit_estimate, Decimal('1000.00'))
        self.assertEqual(order.payment_method, 'Cash')
        self.assertEqual(order.status, 'pending')
        self.assertEqual(order.approval_status, 'approved')
        self.assertEqual(order.fulfillment_details, {'deliveryMethod': 'pickup'})
        self.assertEqual(order.salesperson_id, str(self.user.pk))
        self.assertEqual(order.source, 'in_app')
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.items.get().unit_price, Decimal('1500.00'))

    def test_blank_branch_falls_back_to_main_branch(self):
        self.post(self.wizard_payload())
        self.assertEqual(Order.objects.get().branch, self.main_branch)

    def test_shop_and_branch_together_resolve(self):
        Inventory.objects.create(product=self.product, branch=self.other_branch, quantity=5)
        payload = self.wizard_payload(branchId=str(self.other_branch.pk))
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Order.objects.get().branch, self.other_branch)

    def test_explicit_branch_of_another_shop_is_rejected(self):
        # The caller names a shop they belong to but pairs it with a foreign
        # branch: a payload inconsistency (400), not an access denial.
        other_shop = Shop.objects.create(name='Duka B')
        stranger_branch = Branch.objects.create(shop=other_shop, name='Main', is_main=True)
        payload = self.wizard_payload(branchId=str(stranger_branch.pk))
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('branchId', response.json())
        self.assertEqual(Order.objects.count(), 0)

    def test_branch_only_payload_from_foreign_shop_is_rejected(self):
        # No shopId at all: the branch's own shop drives the access check.
        other_shop = Shop.objects.create(name='Duka B')
        stranger_branch = Branch.objects.create(shop=other_shop, name='Main', is_main=True)
        payload = self.wizard_payload()
        payload.pop('shopId')
        payload['branchId'] = str(stranger_branch.pk)
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Order.objects.count(), 0)

    def test_shop_without_membership_is_rejected(self):
        assert_shop = Shop.objects.create(name='Duka B')
        UserRole.objects.filter(user=self.user).delete()
        payload = self.wizard_payload(shopId=str(assert_shop.pk))
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Order.objects.count(), 0)

    def test_legacy_product_id_resolves(self):
        legacy_product = Product.objects.create(
            shop=self.shop, name='Legacy Soda', legacy_id='prod-fs-1',
            buying_price=Decimal('500.00'), selling_price=Decimal('800.00'))
        Inventory.objects.create(product=legacy_product, branch=self.main_branch, quantity=10)
        payload = self.wizard_payload(items=[{
            'productId': 'prod-fs-1', 'productName': 'Legacy Soda',
            'quantity': 3, 'price': 800.0, 'subtotal': 2400.0}])
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Order.objects.get().items.get().product, legacy_product)

    def test_unknown_product_is_rejected(self):
        payload = self.wizard_payload(items=[{
            'productId': 'missing-prod', 'quantity': 1, 'price': 100.0}])
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 0)

    def test_pickup_fulfilment_defaults_when_omitted(self):
        payload = self.wizard_payload()
        payload.pop('fulfillment')
        self.post(payload)
        self.assertEqual(Order.objects.get().fulfillment_details, {'deliveryMethod': 'pickup'})

    def test_unknown_customer_name_falls_back_to_unknown(self):
        self.post(self.wizard_payload())
        self.assertEqual(Order.objects.get().customer_name, 'Unknown')

    def test_customer_name_falls_back_to_crm_row(self):
        customer = Customer.objects.create(shop=self.shop, name='Acme Corp')
        payload = self.wizard_payload(customerId=str(customer.pk))
        self.post(payload)
        self.assertEqual(Order.objects.get().customer_name, 'Acme Corp')

    def test_customer_type_defaults_to_retail(self):
        self.post(self.wizard_payload())
        self.assertEqual(Order.objects.get().customer_type, 'retail')

    def test_customer_type_reads_commercial_settings_price_tier(self):
        customer = Customer.objects.create(
            shop=self.shop, name='Acme', customer_type='corporate',
            commercial_settings={'priceTier': 'wholesale'})
        self.post(self.wizard_payload(customerId=str(customer.pk)))
        self.assertEqual(Order.objects.get().customer_type, 'wholesale')

    def test_customer_type_falls_back_to_crm_type(self):
        customer = Customer.objects.create(shop=self.shop, name='Acme', customer_type='corporate')
        self.post(self.wizard_payload(customerId=str(customer.pk)))
        self.assertEqual(Order.objects.get().customer_type, 'corporate')

    def test_blank_required_delivery_date_is_accepted(self):
        response = self.post(self.wizard_payload(requiredDeliveryDate=''))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(Order.objects.get().required_delivery_date)

    def test_idempotent_replay_keeps_single_order_and_stock(self):
        payload = self.wizard_payload()
        first = self.post(payload)
        second = self.post(payload)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.json()['id'], first.json()['id'])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 48)

    def test_create_response_matches_callable_contract(self):
        # ``useCreateOrder`` reads ``responseData.orderId``; Firestore returned the
        # order doc id, so Django answers with the order pk under the same key.
        first = self.post(self.wizard_payload()).json()
        self.assertTrue(first['success'])
        self.assertEqual(first['orderId'], str(Order.objects.get().pk))
        replay = self.post(self.wizard_payload()).json()
        self.assertEqual(replay['orderId'], first['orderId'])


class WizardPayloadValidationTests(OrderParityBase):
    def test_missing_shop_and_branch_is_rejected(self):
        payload = self.wizard_payload()
        payload.pop('shopId')
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('shopId', response.json())

    def test_missing_idempotency_key_is_rejected(self):
        payload = self.wizard_payload()
        payload.pop('idempotencyKey')
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('idempotencyKey', response.json())

    def test_empty_items_are_rejected(self):
        response = self.post(self.wizard_payload(items=[]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_item_without_price_is_rejected(self):
        response = self.post(self.wizard_payload(items=[{
            'productId': str(self.product.pk), 'quantity': 1}]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # DRF 3.18 indexes nested list errors by string index.
        self.assertIn('price', response.json()['items']['0'])

    def test_authentication_is_required(self):
        response = APIClient().post(ORDERS_URL, self.wizard_payload(), format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
