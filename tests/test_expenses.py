from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.expenses.models import Expense
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch, UserRole
# pyrefly: ignore [missing-import]
from apps.sales.models import DailySalesSummary, Shift

User = get_user_model()


class ExpenseAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='expenseuser', password='password')
        self.client.force_authenticate(user=self.user)

        self.shop = Shop.objects.create(name='Expense Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main Branch')
        self.shift = Shift.objects.create(
            shop=self.shop,
            status='OPEN',
            opened_by_name='Attendant',
            opening_cash=Decimal('10000.00'),
            expected_closing_cash=Decimal('10000.00'),
        )
        self.today = timezone.localdate()

    def _payload(self, **overrides):
        payload = {
            'shopId': str(self.shop.id),
            'branchId': str(self.branch.id),
            'category': 'Usafiri',
            'description': 'Fuel',
            'amount': '500.00',
            'date': self.today.isoformat(),
            'paymentMethod': 'Taslimu',
            'reference': 'RCPT-1',
            'paidTo': 'Shell',
            'isRecurring': False,
        }
        payload.update(overrides)
        return payload

    def test_create_expense_with_camelcase_updates_summary_and_shift(self):
        response = self.client.post(
            '/api/v1/expenses/', self._payload(shiftId=str(self.shift.id)), format='json'
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(str(response.data['shopId']), str(self.shop.id))
        self.assertEqual(response.data['paymentMethod'], 'Taslimu')
        self.assertEqual(response.data['paidTo'], 'Shell')
        # DecimalField serializes to a string (COERCE_DECIMAL_TO_STRING default).
        self.assertEqual(Decimal(str(response.data['amount'])), Decimal('500.00'))
        self.assertIsNotNone(response.data['createdAt'])
        self.assertEqual(str(response.data['recordedBy']), str(self.user.id))

        summary = DailySalesSummary.objects.get(shop=self.shop, branch=self.branch, date=self.today)
        self.assertEqual(summary.total_expenses, Decimal('500.00'))
        self.assertEqual(summary.net_profit, Decimal('-500.00'))

        # "Taslimu" counts as cash: Firebase only matched the literal "cash" here.
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_expenses_total, Decimal('500.00'))

    def test_create_expense_without_branch_skips_summary(self):
        response = self.client.post('/api/v1/expenses/', self._payload(branchId=None), format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(DailySalesSummary.objects.count(), 0)

    def test_update_expense_reverses_old_effects(self):
        create = self.client.post(
            '/api/v1/expenses/', self._payload(shiftId=str(self.shift.id)), format='json'
        )
        expense_id = create.data['id']

        response = self.client.patch(
            f'/api/v1/expenses/{expense_id}/',
            {'amount': '300.00', 'paymentMethod': 'mpesa'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)

        summary = DailySalesSummary.objects.get(shop=self.shop, branch=self.branch, date=self.today)
        self.assertEqual(summary.total_expenses, Decimal('300.00'))
        self.assertEqual(summary.net_profit, Decimal('-300.00'))

        # No longer a cash expense, so the drawer total is restored.
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_expenses_total, Decimal('0.00'))

    def test_delete_expense_reverses_effects(self):
        create = self.client.post(
            '/api/v1/expenses/', self._payload(shiftId=str(self.shift.id)), format='json'
        )
        expense_id = create.data['id']

        response = self.client.delete(f'/api/v1/expenses/{expense_id}/')
        self.assertEqual(response.status_code, 204)

        summary = DailySalesSummary.objects.get(shop=self.shop, branch=self.branch, date=self.today)
        self.assertEqual(summary.total_expenses, Decimal('0.00'))
        self.assertEqual(summary.net_profit, Decimal('0.00'))

        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_expenses_total, Decimal('0.00'))
        self.assertEqual(Expense.objects.count(), 0)

    def test_list_only_returns_own_shop_expenses(self):
        Expense.objects.create(
            shop=self.shop, category='A', description='Mine', amount=Decimal('100.00'), date=self.today
        )
        other_shop = Shop.objects.create(name='Other Shop')
        other_expense = Expense.objects.create(
            shop=other_shop, category='B', description='Theirs', amount=Decimal('200.00'), date=self.today
        )

        response = self.client.get('/api/v1/expenses/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['description'], 'Mine')

        detail = self.client.get(f'/api/v1/expenses/{other_expense.id}/')
        self.assertEqual(detail.status_code, 404)

    def test_create_expense_for_foreign_shop_forbidden(self):
        other_shop = Shop.objects.create(name='Not Mine')

        response = self.client.post(
            '/api/v1/expenses/', self._payload(shopId=str(other_shop.id)), format='json'
        )
        self.assertEqual(response.status_code, 403)

    def test_create_expense_without_shop_rejected(self):
        payload = self._payload()
        payload.pop('shopId')

        response = self.client.post('/api/v1/expenses/', payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('shopId', response.data)

    def test_filter_by_date_range_and_category(self):
        Expense.objects.create(
            shop=self.shop, category='A', description='Old',
            amount=Decimal('100.00'), date=self.today - timedelta(days=10),
        )
        Expense.objects.create(
            shop=self.shop, category='B', description='New',
            amount=Decimal('100.00'), date=self.today,
        )

        by_date = self.client.get('/api/v1/expenses/', {'dateFrom': self.today.isoformat()})
        self.assertEqual(by_date.data['count'], 1)
        self.assertEqual(by_date.data['results'][0]['description'], 'New')

        by_category = self.client.get('/api/v1/expenses/', {'category': 'A'})
        self.assertEqual(by_category.data['count'], 1)
        self.assertEqual(by_category.data['results'][0]['description'], 'Old')
