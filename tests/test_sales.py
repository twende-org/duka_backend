from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from decimal import Decimal
from django.utils import timezone

# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch, UserRole
# pyrefly: ignore [missing-import]
from apps.products.models import Product, Inventory
# pyrefly: ignore [missing-import]
from apps.sales.models import Sale, DailySalesSummary, Shift

User = get_user_model()

class SalesAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='salesuser', password='password')
        self.client.force_authenticate(user=self.user)
        
        self.shop = Shop.objects.create(name='Test Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main Branch')
        
        self.product = Product.objects.create(
            shop=self.shop, 
            name='Laptop', 
            buying_price=Decimal('1000.00'), 
            selling_price=Decimal('1500.00')
        )
        # Give it 10 units in stock
        self.inventory = Inventory.objects.create(
            product=self.product,
            branch=self.branch,
            quantity=10
        )
        
    def test_pos_sale_success(self):
        payload = {
            'branch_id': str(self.branch.id),
            'payment_method': 'cash',
            'customer_name': 'John Doe',
            'items': [
                {
                    'product_id': str(self.product.id),
                    'quantity': 2,
                    'unit_price': '1500.00'
                }
            ]
        }
        
        response = self.client.post('/api/v1/sales/', payload, format='json')
        
        self.assertEqual(response.status_code, 201)
        
        # Check stock deducted (10 - 2 = 8)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 8)
        
        # Check sale was created with correct profit
        # 2 units * (1500 - 1000) = 1000 profit
        sale = Sale.objects.first()
        self.assertEqual(sale.total_amount, Decimal('3000.00'))
        self.assertEqual(sale.profit, Decimal('1000.00'))
        
        # Check daily summary
        summary = DailySalesSummary.objects.first()
        self.assertIsNotNone(summary)
        self.assertEqual(summary.total_sales, Decimal('3000.00'))
        self.assertEqual(summary.profit, Decimal('1000.00'))
        self.assertEqual(summary.transactions, 1)

    def test_pos_sale_insufficient_stock(self):
        payload = {
            'branch_id': str(self.branch.id),
            'payment_method': 'mpesa',
            'items': [
                {
                    'product_id': str(self.product.id),
                    'quantity': 20, # Only 10 in stock
                    'unit_price': '1500.00'
                }
            ]
        }
        
        response = self.client.post('/api/v1/sales/', payload, format='json')
        
        self.assertEqual(response.status_code, 400)
        self.assertIn('Insufficient stock', response.data['detail'][0])
        
        # Check rollback occurred (stock still 10, no sale, no summary)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 10)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(DailySalesSummary.objects.count(), 0)

class OrderAPITests(TestCase):
    def setUp(self):
        # pyrefly: ignore [missing-import]
        from apps.crm.models import Customer
        self.client = APIClient()
        self.user = User.objects.create_user(username='orderuser', password='password')
        self.client.force_authenticate(user=self.user)
        
        self.shop = Shop.objects.create(name='Test Shop B2B')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main Branch')
        
        self.product = Product.objects.create(
            shop=self.shop, 
            name='B2B Laptop', 
            buying_price=Decimal('1000.00'), 
            selling_price=Decimal('1500.00')
        )
        self.inventory = Inventory.objects.create(
            product=self.product,
            branch=self.branch,
            quantity=50
        )
        
        self.customer = Customer.objects.create(
            shop=self.shop,
            name="Acme Corp",
            customer_type="corporate",
            credit_limit=Decimal('5000.00')
        )
        
    def test_b2b_order_success(self):
        # pyrefly: ignore [missing-import]
        from apps.sales.models import Order
        payload = {
            'branch_id': str(self.branch.id),
            'idempotency_key': 'test-b2b-success-123',
            'payment_method': 'credit',
            'customer_id': str(self.customer.id),
            'items': [
                {
                    'product_id': str(self.product.id),
                    'quantity': 2,
                    'unit_price': '1500.00'
                }
            ]
        }
        response = self.client.post('/api/v1/orders/', payload, format='json')
        self.assertEqual(response.status_code, 201)
        
        # Check stock deducted (50 - 2 = 48)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 48)
        
        # Check customer balance updated
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_balance, Decimal('3000.00'))
        
        # Check order created correctly
        order = Order.objects.first()
        self.assertIsNotNone(order)
        self.assertEqual(order.profit_estimate, Decimal('1000.00'))
        self.assertEqual(order.status, 'pending')

    def test_b2b_order_credit_limit_exceeded(self):
        # pyrefly: ignore [missing-import]
        from apps.sales.models import Order
        payload = {
            'branch_id': str(self.branch.id),
            'idempotency_key': 'test-b2b-fail-123',
            'payment_method': 'credit',
            'customer_id': str(self.customer.id),
            'items': [
                {
                    'product_id': str(self.product.id),
                    'quantity': 10, # 15,000 exceeds 5,000 limit
                    'unit_price': '1500.00'
                }
            ]
        }
        response = self.client.post('/api/v1/orders/', payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('exceeds customer credit limit', response.data['detail'][0])
        
        # Check rollback
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 50)
        self.assertEqual(Order.objects.count(), 0)

    def test_fulfillment_cancellation_restores_inventory(self):
        # pyrefly: ignore [missing-import]
        from apps.sales.models import Order
        payload = {
            'branch_id': str(self.branch.id),
            'idempotency_key': 'test-fulfillment-123',
            'payment_method': 'credit',
            'customer_id': str(self.customer.id),
            'items': [{'product_id': str(self.product.id), 'quantity': 2, 'unit_price': '1500.00'}]
        }
        resp = self.client.post('/api/v1/orders/', payload, format='json')
        self.assertEqual(resp.status_code, 201)
        order_id = resp.data['id']
        
        # Verify initial state
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 48) # 50 - 2
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_balance, Decimal('3000.00'))
        
        # Update status to packed
        patch_resp = self.client.patch(f'/api/v1/orders/{order_id}/update_status/', {
            'status': 'packed',
            'fulfillment_data': {'packedBy': 'John Doe'}
        }, format='json')
        self.assertEqual(patch_resp.status_code, 200)
        
        # Cancel order
        cancel_resp = self.client.patch(f'/api/v1/orders/{order_id}/update_status/', {
            'status': 'cancelled'
        }, format='json')
        self.assertEqual(cancel_resp.status_code, 200)
        
        # Verify inventory restored
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 50)
        
        # Verify customer balance restored
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_balance, Decimal('0.00'))

    def test_fulfillment_picking_updates(self):
        payload = {
            'branch_id': str(self.branch.id),
            'idempotency_key': 'test-picking-123',
            'payment_method': 'cash',
            'items': [{'product_id': str(self.product.id), 'quantity': 10, 'unit_price': '1500.00'}]
        }
        resp = self.client.post('/api/v1/orders/', payload, format='json')
        order_id = resp.data['id']
        item_id = resp.data['items'][0]['id']
        
        # Update picking qty
        patch_resp = self.client.patch(f'/api/v1/orders/{order_id}/update_status/', {
            'status': 'picking',
            'items': [{'id': item_id, 'picked_qty': 8}]
        }, format='json')
        self.assertEqual(patch_resp.status_code, 200)
        self.assertEqual(patch_resp.data['items'][0]['picked_qty'], 8)


class ShiftAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='shiftuser', password='password')
        self.client.force_authenticate(user=self.user)

        self.shop = Shop.objects.create(name='Shift Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main Branch')

        self.product = Product.objects.create(
            shop=self.shop,
            name='Kettle',
            buying_price=Decimal('1000.00'),
            selling_price=Decimal('1500.00'),
        )
        self.inventory = Inventory.objects.create(
            product=self.product,
            branch=self.branch,
            quantity=100,
        )

    def _open_shift(self, **overrides):
        payload = {
            'shopId': str(self.shop.id),
            'openedByName': 'Amina',
            'openingCash': '10000.00',
        }
        payload.update(overrides)
        return self.client.post('/api/v1/shifts/', payload, format='json')

    def _sale_payload(self, **overrides):
        payload = {
            'branch_id': str(self.branch.id),
            'payment_method': 'cash',
            'items': [
                {
                    'product_id': str(self.product.id),
                    'quantity': 2,
                    'unit_price': '1500.00',
                }
            ],
        }
        payload.update(overrides)
        return payload

    def _create_open_shift(self, **overrides):
        defaults = {
            'shop': self.shop,
            'status': 'OPEN',
            'opened_by_name': 'Amina',
            'opening_cash': Decimal('10000.00'),
            'expected_closing_cash': Decimal('10000.00'),
        }
        defaults.update(overrides)
        return Shift.objects.create(**defaults)

    def test_open_shift_via_camelcase_payload(self):
        response = self._open_shift()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'OPEN')
        self.assertEqual(str(response.data['shopId']), str(self.shop.id))
        self.assertEqual(response.data['openedByName'], 'Amina')
        self.assertEqual(Decimal(str(response.data['openingCash'])), Decimal('10000.00'))
        self.assertEqual(Decimal(str(response.data['cashSalesTotal'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(response.data['cashExpensesTotal'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(response.data['expectedClosingCash'])), Decimal('10000.00'))
        self.assertEqual(response.data['ownerApprovalStatus'], 'PENDING')
        self.assertIsNotNone(response.data['openedAt'])

    def test_open_shift_blocked_when_one_is_already_open(self):
        first = self._open_shift()
        self.assertEqual(first.status_code, 201)

        response = self._open_shift(openedByName='Second')

        self.assertEqual(response.status_code, 400)
        self.assertIn('open shift already exists', response.data['detail'][0])
        self.assertEqual(Shift.objects.filter(shop=self.shop, status='OPEN').count(), 1)

    def test_pos_sale_ties_cash_to_shift_and_close_computes_discrepancy(self):
        shift = self._create_open_shift()

        response = self.client.post(
            '/api/v1/sales/', self._sale_payload(shiftId=str(shift.id)), format='json'
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(str(response.data['shiftId']), str(shift.id))

        shift.refresh_from_db()
        self.assertEqual(shift.cash_sales_total, Decimal('3000.00'))

        # Closing: expected = 10000 opening + 3000 cash sales - 0 cash expenses.
        close = self.client.post(f'/api/v1/shifts/{shift.id}/close/', {
            'actualClosingCash': '12500.00',
            'cashLeftForNextDay': '12000.00',
            'cashSubmittedToOwner': '500.00',
            'closedByName': 'Amina',
        }, format='json')

        self.assertEqual(close.status_code, 200)
        self.assertEqual(Decimal(str(close.data['expectedClosingCash'])), Decimal('13000.00'))
        self.assertEqual(Decimal(str(close.data['discrepancy'])), Decimal('-500.00'))
        self.assertEqual(Decimal(str(close.data['cashLeftForNextDay'])), Decimal('12000.00'))
        self.assertEqual(close.data['status'], 'CLOSED')
        self.assertEqual(close.data['ownerApprovalStatus'], 'PENDING')
        self.assertIsNotNone(close.data['closedAt'])

    def test_non_cash_sale_does_not_touch_drawer(self):
        shift = self._create_open_shift()

        response = self.client.post(
            '/api/v1/sales/',
            self._sale_payload(shiftId=str(shift.id), payment_method='mpesa'),
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        shift.refresh_from_db()
        self.assertEqual(shift.cash_sales_total, Decimal('0.00'))

    def test_sale_with_closed_shift_rejected(self):
        shift = self._create_open_shift(status='CLOSED')

        response = self.client.post(
            '/api/v1/sales/', self._sale_payload(shiftId=str(shift.id)), format='json'
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('Shift is not open', response.data['detail'][0])

        # Rejected before any write: stock, sale and summary untouched.
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 100)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(DailySalesSummary.objects.count(), 0)

    def test_cannot_close_shift_twice(self):
        shift = self._create_open_shift()

        first = self.client.post(
            f'/api/v1/shifts/{shift.id}/close/', {'actualClosingCash': '10000.00'}, format='json'
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            f'/api/v1/shifts/{shift.id}/close/', {'actualClosingCash': '10000.00'}, format='json'
        )
        self.assertEqual(second.status_code, 400)
        self.assertIn('already closed', second.data['detail'][0])

    def test_shift_scoped_to_own_shop(self):
        own_shift = self._create_open_shift()
        foreign_shop = Shop.objects.create(name='Foreign Shift Shop')
        foreign_shift = Shift.objects.create(shop=foreign_shop, status='OPEN', opened_by_name='Them')

        listing = self.client.get('/api/v1/shifts/')
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data['count'], 1)
        self.assertEqual(str(listing.data['results'][0]['id']), str(own_shift.id))

        detail = self.client.get(f'/api/v1/shifts/{foreign_shift.id}/')
        self.assertEqual(detail.status_code, 404)

        close = self.client.post(
            f'/api/v1/shifts/{foreign_shift.id}/close/', {'actualClosingCash': '0.00'}, format='json'
        )
        self.assertEqual(close.status_code, 404)

        open_foreign = self._open_shift(shopId=str(foreign_shop.id))
        self.assertEqual(open_foreign.status_code, 403)
