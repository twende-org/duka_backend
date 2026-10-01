from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.core import insights
# pyrefly: ignore [missing-import]
from apps.expenses.models import Expense
# pyrefly: ignore [missing-import]
from apps.products.models import Inventory, Product
# pyrefly: ignore [missing-import]
from apps.sales.models import DailySalesSummary
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.users.models import User


class ParseInsightsTests(SimpleTestCase):
    """The parser mirrors the browser-side AIBusinessCoach line handling."""

    def test_cleans_markup_and_prefixes(self):
        text = (
            "Hongera! Mauzo yamepanda leo.\n"
            "1. **Tahadhari:** Bidhaa zimepungua stoo.\n"
            "- Ushauri: ongeza stock ya soda.\n"
        )
        result = insights.parse_insights(text)
        self.assertEqual(result, [
            {'type': 'success', 'text': 'Hongera! Mauzo yamepanda leo.'},
            {'type': 'warning', 'text': 'Tahadhari: Bidhaa zimepungua stoo.'},
            {'type': 'info', 'text': 'Ushauri: ongeza stock ya soda.'},
        ])

    def test_skips_short_lines_and_caps_at_three(self):
        text = "\n".join([
            "Ah",                  # 2 chars: dropped like the legacy length filter
            "Vizuri",              # 6 chars: survives the >5 filter
            "Line one is long enough.",
            "Line two is long enough.",
            "Line three is long enough.",
            "Line four should be dropped.",
        ])
        result = insights.parse_insights(text)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]['text'], 'Vizuri')
        self.assertEqual(result[0]['type'], 'success')

    def test_blank_input_gives_no_insights(self):
        self.assertEqual(insights.parse_insights(''), [])
        self.assertEqual(insights.parse_insights(None), [])


class BuildPromptTests(SimpleTestCase):
    def test_prompt_carries_the_numbers_and_language(self):
        snapshot = {
            'todaySales': 5000.0,
            'yesterdaySales': 4000.5,
            'todayExpenses': 200.0,
            'lowStockItems': ['Soda (2)'],
        }
        prompt = insights.build_prompt(snapshot, lang='sw')
        self.assertIn('Kiswahili Sanifu', prompt)
        self.assertIn('Sales Today: TZS 5000', prompt)
        self.assertIn('Sales Yesterday: TZS 4000.5', prompt)
        self.assertIn('Items with low stock: Soda (2)', prompt)

        english = insights.build_prompt(snapshot, lang='en')
        self.assertIn('English', english)

    def test_prompt_says_none_when_nothing_is_low(self):
        prompt = insights.build_prompt(
            {'todaySales': 0, 'yesterdaySales': 0, 'todayExpenses': 0, 'lowStockItems': []}
        )
        self.assertIn('Items with low stock: None', prompt)


class GatherSnapshotTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name='Test Shop')
        self.branch = Branch.objects.create(shop=self.shop, name='Main')
        self.other_branch = Branch.objects.create(shop=self.shop, name='Mbezi')
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)

    def _summary(self, branch, day, total):
        return DailySalesSummary.objects.create(
            shop=self.shop, branch=branch, date=day, total_sales=Decimal(total)
        )

    def test_sales_sum_across_branches_per_day(self):
        self._summary(self.branch, self.today, '3000')
        self._summary(self.other_branch, self.today, '2000')
        self._summary(self.branch, self.yesterday, '1500')
        self._summary(self.branch, self.today - timedelta(days=5), '999')

        snapshot = insights.gather_snapshot(self.shop.id)
        self.assertEqual(snapshot['todaySales'], 5000.0)
        self.assertEqual(snapshot['yesterdaySales'], 1500.0)

    def test_expenses_are_today_only(self):
        Expense.objects.create(shop=self.shop, category='Usafiri', description='d',
                               amount=Decimal('250.50'), date=self.today)
        Expense.objects.create(shop=self.shop, category='Usafiri', description='d',
                               amount=Decimal('99.99'), date=self.today)
        Expense.objects.create(shop=self.shop, category='Usafiri', description='d',
                               amount=Decimal('777'), date=self.yesterday)

        snapshot = insights.gather_snapshot(self.shop.id)
        self.assertEqual(snapshot['todayExpenses'], 350.49)

    def test_low_stock_sums_branches_and_skips_products_without_rows(self):
        low = Product.objects.create(shop=self.shop, name='Soda', selling_price=1000)
        Inventory.objects.create(product=low, branch=self.branch, quantity=1, low_stock_threshold=3)
        Inventory.objects.create(product=low, branch=self.other_branch, quantity=1, low_stock_threshold=3)
        # Shop-wide: 2 < 6 → low, label carries the summed quantity.
        healthy = Product.objects.create(shop=self.shop, name='Sugar', selling_price=2000)
        Inventory.objects.create(product=healthy, branch=self.branch, quantity=50, low_stock_threshold=3)
        no_rows = Product.objects.create(shop=self.shop, name='Salt', selling_price=500)

        snapshot = insights.gather_snapshot(self.shop.id)
        self.assertEqual(snapshot['lowStockItems'], ['Soda (2)'])
        self.assertNotIn(no_rows.name, ' '.join(snapshot['lowStockItems']))

    def test_zero_threshold_falls_back_to_five(self):
        product = Product.objects.create(shop=self.shop, name='Milk', selling_price=1000)
        Inventory.objects.create(product=product, branch=self.branch, quantity=4, low_stock_threshold=0)
        self.assertEqual(insights.gather_snapshot(self.shop.id)['lowStockItems'], ['Milk (4)'])

    def test_scan_is_capped_at_ten_products(self):
        for i in range(12):
            product = Product.objects.create(shop=self.shop, name=f'P{i:02d}', selling_price=100)
            Inventory.objects.create(product=product, branch=self.branch, quantity=0, low_stock_threshold=5)
        self.assertEqual(len(insights.gather_snapshot(self.shop.id)['lowStockItems']), 10)


class GenerateInsightsTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name='Test Shop')
        self.branch = Branch.objects.create(shop=self.shop, name='Main')
        DailySalesSummary.objects.create(
            shop=self.shop, branch=self.branch, date=timezone.localdate(),
            total_sales=Decimal('4200'),
        )

    def test_without_api_key_data_still_comes_back(self):
        with override_settings(OPENROUTER_API_KEY=''):
            result = insights.generate_insights(self.shop.id)
        self.assertEqual(result['insights'], [])
        self.assertEqual(result['data']['todaySales'], 4200.0)

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_successful_call_is_parsed(self):
        payload = {'choices': [{'message': {'content': 'Hongera! Mauzo ni mazuri leo.\nTahadhari: angalia stoo yako.'}}]}
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = payload
        with mock.patch.object(insights.requests, 'post', return_value=response) as post:
            result = insights.generate_insights(self.shop.id, lang='en')

        self.assertEqual(result['insights'], [
            {'type': 'success', 'text': 'Hongera! Mauzo ni mazuri leo.'},
            {'type': 'warning', 'text': 'Tahadhari: angalia stoo yako.'},
        ])
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-key')
        self.assertEqual(kwargs['json']['model'], insights.OPENROUTER_MODEL)
        self.assertIn('English', kwargs['json']['messages'][0]['content'])

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_http_error_yields_empty_insights(self):
        response = mock.Mock(ok=False, status_code=502)
        response.raise_for_status.side_effect = insights.requests.HTTPError('bad gateway')
        with mock.patch.object(insights.requests, 'post', return_value=response):
            result = insights.generate_insights(self.shop.id)
        self.assertEqual(result['insights'], [])
        self.assertEqual(result['data']['todaySales'], 4200.0)

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_malformed_payload_yields_empty_insights(self):
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = {'choices': 'nope'}
        with mock.patch.object(insights.requests, 'post', return_value=response):
            result = insights.generate_insights(self.shop.id)
        self.assertEqual(result['insights'], [])


class ShopInsightsViewTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name='Test Shop')
        self.branch = Branch.objects.create(shop=self.shop, name='Main')
        self.member = User.objects.create_user(username='owner', email='o@test.com', password='pw')
        UserRole.objects.create(user=self.member, shop=self.shop, role='owner')
        self.outsider = User.objects.create_user(username='other', email='x@test.com', password='pw')
        self.url = reverse('shop-insights', args=[self.shop.id])

    def test_anonymous_is_rejected(self):
        self.assertEqual(APIClient().get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_member_is_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=self.outsider)
        self.assertEqual(client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    def test_unknown_shop_is_404(self):
        client = APIClient()
        client.force_authenticate(user=self.member)
        self.assertEqual(
            client.get(reverse('shop-insights', args=['does-not-exist'])).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    @override_settings(OPENROUTER_API_KEY='')
    def test_member_gets_insights_and_data(self):
        DailySalesSummary.objects.create(
            shop=self.shop, branch=self.branch, date=timezone.localdate(),
            total_sales=Decimal('1234.50'),
        )
        client = APIClient()
        client.force_authenticate(user=self.member)
        response = client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body['insights'], [])
        self.assertEqual(body['data']['todaySales'], 1234.5)
        self.assertEqual(body['data']['todayExpenses'], 0.0)
        self.assertEqual(body['data']['lowStockItems'], [])
        self.assertIn('yesterdaySales', body['data'])

    @override_settings(OPENROUTER_API_KEY='')
    def test_legacy_shop_id_resolves(self):
        self.shop.legacy_id = 'legacy-shop-1'
        self.shop.save(update_fields=['legacy_id'])
        client = APIClient()
        client.force_authenticate(user=self.member)
        response = client.get(reverse('shop-insights', args=['legacy-shop-1']))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('data', response.json())

    @override_settings(OPENROUTER_API_KEY='')
    def test_unknown_legacy_shop_id_is_404(self):
        client = APIClient()
        client.force_authenticate(user=self.member)
        self.assertEqual(
            client.get(reverse('shop-insights', args=['missing-legacy'])).status_code,
            status.HTTP_404_NOT_FOUND,
        )
