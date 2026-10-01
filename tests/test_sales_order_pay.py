"""Payment + fulfillment parity with Firestore's ``payOrder`` / ``updateOrderFulfillmentStatus``.

``Orders.tsx`` settles an order through ``usePayOrder`` and ``Fulfillment.tsx``
walks the allocation → delivery pipeline; both used to be Firestore transactions.
These tests pin the Django ports: the drawer/day-summary writes ``payOrder``
made, the guard that stops a settled order being paid twice, and the item
patches ``PickingDialog`` posts keyed by product instead of by row id.
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.crm.models import Customer
# pyrefly: ignore [missing-import]
from apps.products.models import Inventory, Product
# pyrefly: ignore [missing-import]
from apps.sales.models import DailySalesSummary, Order, Shift
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.users.models import User

ORDERS_URL = '/api/v1/orders/'


class OrderPayBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='merchant', email='merchant@test.com', password='password')
        self.client.force_authenticate(user=self.user)

        self.shop = Shop.objects.create(name='Duka A')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main', is_main=True)

        self.product = Product.objects.create(
            shop=self.shop, name='Crate', buying_price=Decimal('1000.00'),
            selling_price=Decimal('1500.00'))
        Inventory.objects.create(product=self.product, branch=self.branch, quantity=50)

    def create_order(self, **overrides):
        payload = {
            'shopId': str(self.shop.pk),
            'branchId': str(self.branch.pk),
            'items': [{
                'productId': str(self.product.pk),
                'productName': 'Crate',
                'quantity': 2,
                'price': 1500.0,
                'subtotal': 3000.0,
            }],
            'totalAmount': 3000.0,
            'paymentMethod': 'Cash',
            'idempotencyKey': 'pay-key-1',
        }
        payload.update(overrides)
        response = self.client.post(ORDERS_URL, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.json()

    def pay(self, order_id, **body):
        return self.client.post(f'{ORDERS_URL}{order_id}/pay/', body, format='json')

    def update_status(self, order_id, **body):
        return self.client.patch(
            f'{ORDERS_URL}{order_id}/update_status/', body, format='json')


class PayOrderTests(OrderPayBase):
    def test_pay_marks_order_paid_and_feeds_the_day_summary(self):
        order = self.create_order()
        response = self.pay(order['id'], paymentMethod='Mobile Money')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        row = Order.objects.get(pk=order['id'])
        self.assertEqual(row.status, 'paid')
        self.assertEqual(row.payment_method, 'Mobile Money')
        self.assertIsNotNone(row.paid_at)

        summary = DailySalesSummary.objects.get(shop=self.shop, branch=self.branch)
        self.assertEqual(summary.date, timezone.localdate())
        self.assertEqual(summary.total_sales, Decimal('3000.00'))
        self.assertEqual(summary.transactions, 1)
        self.assertEqual(summary.profit, Decimal('1000.00'))
        self.assertEqual(summary.net_profit, Decimal('1000.00'))

    def test_pay_does_not_move_inventory(self):
        # Stock left the shelf when the order was created; ``payOrder`` only
        # settled money — no second deduction.
        order = self.create_order()
        self.pay(order['id'], paymentMethod='Cash')
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 48)

    def test_second_payment_is_rejected(self):
        order = self.create_order()
        self.pay(order['id'], paymentMethod='Cash')
        again = self.pay(order['id'], paymentMethod='Cash')
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('imeghairiwa', ' '.join(again.json()['detail']))
        summary = DailySalesSummary.objects.get(shop=self.shop, branch=self.branch)
        self.assertEqual(summary.total_sales, Decimal('3000.00'))
        self.assertEqual(summary.transactions, 1)

    def test_cancelled_order_cannot_be_paid(self):
        order = self.create_order()
        self.update_status(order['id'], status='cancelled')
        response = self.pay(order['id'], paymentMethod='Cash')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.get(pk=order['id']).status, 'cancelled')
        self.assertEqual(DailySalesSummary.objects.count(), 0)

    def test_awaiting_shipment_order_can_still_be_paid(self):
        # A B2B order parked in ``awaiting_shipment`` was still payable in Firestore.
        order = self.create_order(approvalStatus='approved')
        self.update_status(order['id'], status='awaiting_shipment')
        self.assertEqual(self.pay(order['id'], paymentMethod='Cash').status_code, 200)
        self.assertEqual(Order.objects.get(pk=order['id']).status, 'paid')

    def test_cash_payment_moves_the_open_drawer(self):
        shift = Shift.objects.create(shop=self.shop, opening_cash=Decimal('100.00'))
        order = self.create_order()
        response = self.pay(order['id'], paymentMethod='Taslimu', shiftId=str(shift.pk))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        shift.refresh_from_db()
        self.assertEqual(shift.cash_sales_total, Decimal('3000.00'))

    def test_non_cash_payment_leaves_the_drawer_alone(self):
        shift = Shift.objects.create(shop=self.shop, opening_cash=Decimal('100.00'))
        order = self.create_order()
        self.pay(order['id'], paymentMethod='Mobile Money', shiftId=str(shift.pk))
        shift.refresh_from_db()
        self.assertEqual(shift.cash_sales_total, Decimal('0.00'))

    def test_unknown_shift_is_rejected(self):
        order = self.create_order()
        response = self.pay(order['id'], paymentMethod='Cash', shiftId='missing-shift')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('shiftId', response.json())
        self.assertEqual(Order.objects.get(pk=order['id']).status, 'pending')

    def test_customer_stats_follow_the_payment(self):
        customer = Customer.objects.create(
            shop=self.shop, name='Acme', legacy_id='cust-fs-1',
            total_spent=Decimal('0.00'), total_purchases=0)
        order = self.create_order(customerId='cust-fs-1')
        self.pay(order['id'], paymentMethod='Credit / Debt')
        customer.refresh_from_db()
        self.assertEqual(customer.total_spent, Decimal('3000.00'))
        self.assertEqual(customer.total_purchases, 1)

    def test_payment_defaults_to_cash(self):
        order = self.create_order()
        self.assertEqual(self.pay(order['id']).status_code, status.HTTP_200_OK)
        self.assertEqual(Order.objects.get(pk=order['id']).payment_method, 'Cash')

    def test_other_shops_order_is_not_payable(self):
        other_shop = Shop.objects.create(name='Duka B')
        other_branch = Branch.objects.create(shop=other_shop, name='Main', is_main=True)
        stranger = Order.objects.create(
            shop=other_shop, branch=other_branch, idempotency_key='foreign-1',
            total_amount=Decimal('500.00'))
        response = self.pay(stranger.pk, paymentMethod='Cash')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Order.objects.get(pk=stranger.pk).status, 'pending')

    def test_legacy_order_id_can_be_paid(self):
        order = Order.objects.create(
            shop=self.shop, branch=self.branch, idempotency_key='legacy-pay-1',
            legacy_id='ord-fs-1', total_amount=Decimal('750.00'),
            profit_estimate=Decimal('250.00'))
        response = self.pay('ord-fs-1', paymentMethod='Cash')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Order.objects.get(pk=order.pk).status, 'paid')

    def test_payment_response_keeps_the_app_shape(self):
        order = self.create_order()
        body = self.pay(order['id'], paymentMethod='Bank Transfer').json()
        self.assertEqual(body['id'], order['id'])
        self.assertEqual(body['shopId'], str(self.shop.pk))
        self.assertEqual(body['totalAmount'], '3000.00')
        self.assertEqual(body['paymentMethod'], 'Bank Transfer')
        self.assertEqual(body['status'], 'paid')
        self.assertIsNotNone(body['paidAt'])


class UpdateFulfillmentStatusTests(OrderPayBase):
    def test_camel_case_fulfillment_data_is_merged(self):
        order = self.create_order()
        response = self.update_status(order['id'], status='out_for_delivery', fulfillmentData={
            'deliveryMethod': 'delivery', 'driverName': 'Juma'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        details = Order.objects.get(pk=order['id']).fulfillment_details
        self.assertEqual(details['deliveryMethod'], 'delivery')
        self.assertEqual(details['driverName'], 'Juma')
        self.assertIn('dispatchedAt', details)

    def test_pipeline_statuses_are_accepted(self):
        # Every status ``Fulfillment.tsx`` walks through must survive validation.
        order = self.create_order()
        for value in ('approved', 'awaiting_shipment', 'confirmed', 'allocated',
                      'picking', 'packed', 'ready_for_delivery', 'out_for_delivery',
                      'in_transit', 'delivered', 'completed'):
            with self.subTest(status=value):
                response = self.update_status(order['id'], status=value)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(Order.objects.get(pk=order['id']).status, value)

    def test_picking_by_product_id_updates_the_row(self):
        # PickingDialog posts the whole OrderItem list without row ids.
        order = self.create_order()
        response = self.update_status(order['id'], status='picking', items=[{
            'productId': str(self.product.pk),
            'productName': 'Crate',
            'quantity': 2,
            'pickedQty': 1,
        }])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['items'][0]['pickedQty'], 1)
        self.assertEqual(Order.objects.get(pk=order['id']).items.get().picked_qty, 1)

    def test_picking_by_product_legacy_id_updates_the_row(self):
        legacy_product = Product.objects.create(
            shop=self.shop, name='Legacy Soda', legacy_id='prod-fs-9',
            buying_price=Decimal('500.00'), selling_price=Decimal('800.00'))
        Inventory.objects.create(product=legacy_product, branch=self.branch, quantity=10)
        order = self.create_order(items=[{
            'productId': 'prod-fs-9', 'productName': 'Legacy Soda',
            'quantity': 3, 'price': 800.0}])
        response = self.update_status(order['id'], status='picking', items=[{
            'productId': 'prod-fs-9', 'quantity': 3, 'pickedQty': 2}])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Order.objects.get(pk=order['id']).items.get().picked_qty, 2)

    def test_blank_picked_qty_counts_as_zero(self):
        order = self.create_order()
        self.update_status(order['id'], status='picking', items=[
            {'id': order['items'][0]['id'], 'picked_qty': 2}])
        response = self.update_status(order['id'], status='picking', items=[
            {'id': order['items'][0]['id']}])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Order.objects.get(pk=order['id']).items.get().picked_qty, 0)


class OrderLegacyIdTests(OrderPayBase):
    def test_list_filters_by_legacy_shop_and_branch(self):
        shop = Shop.objects.create(name='Legacy Shop', legacy_id='shop-fs-1')
        UserRole.objects.create(user=self.user, shop=shop, role='owner')
        branch = Branch.objects.create(
            shop=shop, name='Main', legacy_id='branch-fs-1', is_main=True)
        order = Order.objects.create(
            shop=shop, branch=branch, idempotency_key='legacy-list-1',
            total_amount=Decimal('500.00'))
        response = self.client.get(
            ORDERS_URL, {'shop_id': 'shop-fs-1', 'branch_id': 'branch-fs-1'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.json()['results']
        self.assertEqual([row['id'] for row in rows], [str(order.pk)])

    def test_detail_uses_the_legacy_id(self):
        order = Order.objects.create(
            shop=self.shop, branch=self.branch, idempotency_key='legacy-get-1',
            legacy_id='ord-fs-get', total_amount=Decimal('1200.00'))
        response = self.client.get(f'{ORDERS_URL}ord-fs-get/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['id'], str(order.pk))

    def test_unknown_legacy_id_is_a_404(self):
        self.assertEqual(
            self.client.get(f'{ORDERS_URL}ord-missing/').status_code,
            status.HTTP_404_NOT_FOUND)
