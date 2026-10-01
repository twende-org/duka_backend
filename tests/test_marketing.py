from django.test import TestCase
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.marketing.models import DiscountCode, Campaign

User = get_user_model()


class MarketingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mktest', password='password')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Marketing Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')

    # ── Discount Code Tests ────────────────────────────────────────────────────
    def test_create_discount_code(self):
        response = self.client.post('/api/v1/discounts/', {
            'shop': self.shop.id,
            'code': 'SAVE20',
            'type': 'percentage',
            'value': '20.00',
            'status': 'active',
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['code'], 'SAVE20')
        self.assertEqual(response.data['used_count'], 0)

    def test_list_discounts_by_shop(self):
        DiscountCode.objects.create(shop=self.shop, code='D1', type='fixed', value=5000)
        DiscountCode.objects.create(shop=self.shop, code='D2', type='percentage', value=15)
        response = self.client.get(f'/api/v1/discounts/?shop_id={self.shop.id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 2)

    def test_increment_usage(self):
        discount = DiscountCode.objects.create(shop=self.shop, code='USE1', type='fixed', value=1000)
        response = self.client.post(f'/api/v1/discounts/{discount.id}/increment_usage/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['used_count'], 1)

    # ── Campaign Tests ─────────────────────────────────────────────────────────
    def test_create_campaign(self):
        response = self.client.post('/api/v1/campaigns/', {
            'shop': self.shop.id,
            'name': 'July Blast',
            'source': 'Dashboard',
            'platform': 'biashara',
            'status': 'completed',
            'reach': 350,
            'channel': 'sms',
            'audience_filter': 'all',
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['name'], 'July Blast')
        self.assertEqual(response.data['channel'], 'sms')

    def test_list_campaigns_by_shop(self):
        Campaign.objects.create(shop=self.shop, name='Camp A', source='Dashboard')
        Campaign.objects.create(shop=self.shop, name='AI Post 1', source='ai_auto_pilot')
        response = self.client.get(f'/api/v1/campaigns/?shop_id={self.shop.id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 2)

    def test_filter_campaigns_by_source(self):
        Campaign.objects.create(shop=self.shop, name='Camp A', source='Dashboard')
        Campaign.objects.create(shop=self.shop, name='AI Post 1', source='ai_auto_pilot')
        response = self.client.get(f'/api/v1/campaigns/?shop_id={self.shop.id}&source=ai_auto_pilot')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'AI Post 1')

    # ── AI Marketing Settings Tests ────────────────────────────────────────────
    def test_shop_ai_marketing_settings(self):
        self.shop.ai_marketing_settings = {'enabled': True, 'tone': 'fun', 'musicVibe': 'upbeat'}
        self.shop.save()
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.ai_marketing_settings['enabled'])
        self.assertEqual(self.shop.ai_marketing_settings['tone'], 'fun')

    # ── camelCase dashboard payloads ───────────────────────────────────────────
    def test_create_discount_code_via_camelcase_shop_id(self):
        response = self.client.post('/api/v1/discounts/', {
            'shopId': str(self.shop.id),
            'code': 'CAMEL10',
            'type': 'percentage',
            'value': '10.00',
            'status': 'active',
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['code'], 'CAMEL10')
        self.assertEqual(response.data['usedCount'], 0)
        self.assertEqual(str(response.data['shopId']), str(self.shop.id))

    def test_duplicate_discount_code_for_shop_is_400(self):
        DiscountCode.objects.create(shop=self.shop, code='DUP', type='fixed', value=1000)
        response = self.client.post('/api/v1/discounts/', {
            'shopId': str(self.shop.id), 'code': 'DUP', 'type': 'fixed', 'value': '1000.00',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_create_campaign_via_camelcase_payload(self):
        discount = DiscountCode.objects.create(
            shop=self.shop, code='BLAST5', type='percentage', value=5
        )
        response = self.client.post('/api/v1/campaigns/', {
            'shopId': str(self.shop.id),
            'name': 'September Blast',
            'source': 'Dashboard',
            'platform': 'biashara',
            'status': 'completed',
            'reach': 350,
            'channel': 'whatsapp',
            'audienceFilter': 'vip',
            'promoCodeId': str(discount.id),
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['name'], 'September Blast')
        self.assertEqual(response.data['audienceFilter'], 'vip')
        self.assertEqual(str(response.data['promoCodeId']), str(discount.id))
        self.assertIn('createdAt', response.data)

    def test_campaign_with_foreign_promo_code_is_400(self):
        foreign_discount = DiscountCode.objects.create(
            shop=Shop.objects.create(name='Foreign Marketing Shop'),
            code='FOREIGN', type='fixed', value=1000,
        )
        response = self.client.post('/api/v1/campaigns/', {
            'shopId': str(self.shop.id),
            'name': 'Cross Shop Blast',
            'reach': 10,
            'promoCodeId': str(foreign_discount.id),
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('promoCodeId', response.data)

    def test_create_campaign_without_shop_is_400(self):
        response = self.client.post('/api/v1/campaigns/', {'name': 'Orphan'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('shopId', response.data)

    def test_list_discounts_and_campaigns_by_camel_shop_id(self):
        DiscountCode.objects.create(shop=self.shop, code='CAM1', type='fixed', value=1000)
        Campaign.objects.create(shop=self.shop, name='Camp Camel', source='Dashboard')

        discounts = self.client.get(f'/api/v1/discounts/?shopId={self.shop.id}')
        self.assertEqual(discounts.status_code, 200)
        self.assertEqual(discounts.data['count'], 1)

        campaigns = self.client.get(f'/api/v1/campaigns/?shopId={self.shop.id}')
        self.assertEqual(campaigns.status_code, 200)
        self.assertEqual(campaigns.data['count'], 1)
