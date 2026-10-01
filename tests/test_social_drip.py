from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from celery.schedules import crontab
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.core.crypto import encrypt_token
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.social import posting, tasks
# pyrefly: ignore [missing-import]
from apps.social.models import SocialIntegration
# pyrefly: ignore [missing-import]
from apps.users.models import User

POST_PAYLOAD = {'success': True, 'facebookPostId': 'fb-1', 'instagramPostId': None}


class MarketingDripTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='drip-owner', email='drip-owner@example.com', password='password',
        )
        self.shop = Shop.objects.create(
            name='Drip Shop', legacy_id='drip-shop-1', slug='drip-shop',
            ai_marketing_settings={'enabled': True, 'tone': 'playful'},
        )
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.integration = SocialIntegration.objects.create(
            shop=self.shop, platform='facebook', is_connected=True,
            page_id='page-1', page_name='Page One', access_token=encrypt_token('EAAG-token'),
        )
        self.product = Product.objects.create(
            shop=self.shop, name='Sukari', legacy_id='prod-1', publish_to_facebook=True,
            selling_price=Decimal('1200.00'),
            image_urls=['https://cdn.example/one.jpg', 'https://cdn.example/two.jpg'],
        )

    def make_shop(self, name, legacy_id, slug, ai_settings, connected=True):
        shop = Shop.objects.create(
            name=name, legacy_id=legacy_id, slug=slug, ai_marketing_settings=ai_settings,
        )
        if connected:
            SocialIntegration.objects.create(
                shop=shop, platform='facebook', is_connected=True,
                page_id=f'page-{legacy_id}', page_name=name,
                access_token=encrypt_token('EAAG-token'),
            )
        return shop


class NextMarketingProductTests(MarketingDripTestCase):
    def test_requires_the_publish_flag(self):
        Product.objects.filter(pk=self.product.pk).update(publish_to_facebook=False)
        self.assertIsNone(tasks.next_marketing_product(self.shop))

    def test_picks_the_oldest_last_posted_at(self):
        Product.objects.filter(pk=self.product.pk).update(
            last_posted_at=datetime(2021, 6, 1, tzinfo=dt_timezone.utc),
        )
        stale = Product.objects.create(
            shop=self.shop, name='Stale', legacy_id='prod-stale', publish_to_facebook=True,
            selling_price=Decimal('500'), image_urls=['https://cdn.example/s.jpg'],
            last_posted_at=datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
        )

        chosen = tasks.next_marketing_product(self.shop)

        self.assertEqual(chosen.pk, stale.pk)

    def test_never_posted_products_go_first(self):
        Product.objects.filter(pk=self.product.pk).update(
            last_posted_at=datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
        )
        fresh = Product.objects.create(
            shop=self.shop, name='Fresh', legacy_id='prod-fresh', publish_to_facebook=True,
            selling_price=Decimal('500'), image_urls=['https://cdn.example/f.jpg'],
        )

        self.assertEqual(tasks.next_marketing_product(self.shop).pk, fresh.pk)

    def test_ignores_products_without_images(self):
        Product.objects.filter(pk=self.product.pk).update(image_urls=[], image_url='')
        self.assertIsNone(tasks.next_marketing_product(self.shop))

    def test_ignores_another_shops_products(self):
        self.make_shop(
            'Other', 'drip-shop-2', 'drip-shop-two', {'enabled': True},
        ).products.create(
            name='Elsewhere', legacy_id='prod-elsewhere', publish_to_facebook=True,
            selling_price=Decimal('1'), image_urls=['https://cdn.example/o.jpg'],
        )
        Product.objects.filter(pk=self.product.pk).update(publish_to_facebook=False)
        self.assertIsNone(tasks.next_marketing_product(self.shop))


class MarketingDripForShopTests(MarketingDripTestCase):
    @patch('apps.social.tasks.perform_facebook_post')
    def test_posts_reel_with_the_ai_config_and_stamps_last_posted(self, mock_perform):
        mock_perform.return_value = dict(POST_PAYLOAD)

        result = tasks.marketing_drip_for_shop(self.shop)

        mock_perform.assert_called_once_with(
            self.shop.pk, ['prod-1'], None, True, 'reel',
            {'enabled': True, 'tone': 'playful'},
        )
        self.assertEqual(result['success'], True)
        self.assertEqual(result['shopId'], str(self.shop.pk))
        self.product.refresh_from_db()
        self.assertIsNotNone(self.product.last_posted_at)

    @patch('apps.social.tasks.perform_facebook_post')
    def test_shop_without_auto_pilot_is_skipped(self, mock_perform):
        Shop.objects.filter(pk=self.shop.pk).update(ai_marketing_settings={'enabled': False})
        self.shop.refresh_from_db()

        self.assertIsNone(tasks.marketing_drip_for_shop(self.shop))
        mock_perform.assert_not_called()

    @patch('apps.social.tasks.perform_facebook_post')
    def test_shop_without_a_live_connection_is_skipped(self, mock_perform):
        self.integration.is_connected = False
        self.integration.save(update_fields=['is_connected'])

        self.assertIsNone(tasks.marketing_drip_for_shop(self.shop))
        mock_perform.assert_not_called()

    @patch('apps.social.tasks.perform_facebook_post')
    def test_shop_without_eligible_products_is_skipped(self, mock_perform):
        Product.objects.filter(pk=self.product.pk).update(publish_to_facebook=False)

        self.assertIsNone(tasks.marketing_drip_for_shop(self.shop))
        mock_perform.assert_not_called()


class RunMarketingDripTests(MarketingDripTestCase):
    @patch('apps.social.tasks.perform_facebook_post')
    def test_one_failing_shop_does_not_stop_the_others(self, mock_perform):
        second_shop = self.make_shop(
            'Second', 'drip-shop-2', 'drip-shop-two', {'enabled': True},
        )
        second_shop.products.create(
            name='Mchele', legacy_id='prod-2', publish_to_facebook=True,
            selling_price=Decimal('900'), image_urls=['https://cdn.example/m.jpg'],
        )

        def fake_post(shop_ref, *args, **kwargs):
            if str(shop_ref) == str(self.shop.pk):
                raise posting.SocialPostFailed('boom')
            return dict(POST_PAYLOAD)

        mock_perform.side_effect = fake_post

        results = {row['shopId']: row for row in tasks.run_marketing_drip()}

        self.assertEqual(len(results), 2)
        self.assertEqual(results[str(self.shop.pk)]['success'], False)
        self.assertEqual(results[str(self.shop.pk)]['error'], 'boom')
        self.assertEqual(results[str(second_shop.pk)]['success'], True)
        self.assertEqual(mock_perform.call_count, 2)

    @patch('apps.social.tasks.perform_facebook_post')
    def test_shop_ids_limit_the_run(self, mock_perform):
        self.make_shop('Second', 'drip-shop-2', 'drip-shop-two', {'enabled': True})
        mock_perform.return_value = dict(POST_PAYLOAD)

        results = tasks.run_marketing_drip(shop_ids=[self.shop.pk])

        self.assertEqual([row['shopId'] for row in results], [str(self.shop.pk)])

    @patch('apps.social.tasks.perform_facebook_post')
    def test_ineligible_shops_are_absent_from_the_results(self, mock_perform):
        self.make_shop('Second', 'drip-shop-2', 'drip-shop-two', {'enabled': False})

        results = tasks.run_marketing_drip()

        self.assertEqual([row['shopId'] for row in results], [str(self.shop.pk)])

    @patch('apps.social.tasks.run_marketing_drip')
    def test_peak_hours_task_wraps_the_platform_run(self, mock_run):
        mock_run.return_value = []
        self.assertEqual(tasks.peak_hours_marketing_drip(), [])
        mock_run.assert_called_once_with()


class MarketingDripTestViewTests(MarketingDripTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('marketing-test-drip')

    def test_requires_authentication(self):
        self.assertEqual(self.client.post(self.url, {}, format='json').status_code, 401)

    def test_requires_a_management_role_for_the_shop(self):
        staff = User.objects.create_user(
            username='drip-staff', email='drip-staff@example.com', password='password',
        )
        UserRole.objects.create(user=staff, shop=self.shop, role='attendant')
        self.client.force_authenticate(user=staff)

        response = self.client.post(self.url, {'shopId': self.shop.legacy_id}, format='json')

        self.assertEqual(response.status_code, 403)

    def test_foreign_manager_cannot_run_another_shop(self):
        other_shop = self.make_shop('Other', 'drip-shop-2', 'drip-shop-two', {'enabled': True})
        stranger = User.objects.create_user(
            username='drip-other', email='drip-other@example.com', password='password',
        )
        UserRole.objects.create(user=stranger, shop=other_shop, role='manager')
        self.client.force_authenticate(user=stranger)

        response = self.client.post(self.url, {'shopId': self.shop.legacy_id}, format='json')

        self.assertEqual(response.status_code, 403)

    def test_unknown_shop_is_404(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'shopId': 'nope'}, format='json')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['detail'], 'Shop not found.')

    def test_platform_run_requires_a_superuser(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['detail'], 'shopId is missing.')

    @patch('api.v1.views.facebook.run_marketing_drip')
    def test_manager_runs_only_their_shop(self, mock_run):
        mock_run.return_value = [{'shopId': str(self.shop.pk), 'success': True}]
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, {'shopId': self.shop.legacy_id}, format='json')

        self.assertEqual(response.status_code, 200)
        mock_run.assert_called_once_with(shop_ids=[self.shop.pk])
        self.assertEqual(
            response.data['message'],
            'Marketing Drip Triggered Successfully. Check logs to verify.',
        )
        self.assertEqual(response.data['results'], mock_run.return_value)

    @patch('api.v1.views.facebook.run_marketing_drip')
    def test_superuser_without_shop_runs_the_platform(self, mock_run):
        mock_run.return_value = []
        admin = User.objects.create_superuser(
            username='drip-admin', email='drip-admin@example.com', password='password',
        )
        self.client.force_authenticate(user=admin)

        response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.status_code, 200)
        mock_run.assert_called_once_with()


class BeatScheduleTests(TestCase):
    def test_peak_hours_drip_is_scheduled(self):
        entry = settings.CELERY_BEAT_SCHEDULE['peak-hours-marketing-drip']
        self.assertEqual(entry['task'], 'apps.social.tasks.peak_hours_marketing_drip')
        self.assertEqual(str(entry['schedule']), str(crontab(hour='7,12,19', minute=30)))
        self.assertEqual(entry['options']['timezone'], 'Africa/Dar_es_Salaam')
