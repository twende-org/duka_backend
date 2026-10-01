from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
# pyrefly: ignore [missing-import]
from apps.sales.models import DailySalesSummary
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop, UserRole

User = get_user_model()

class ShopTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Test Shop', slug='test-shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')

    def test_shop_settings(self):
        self.shop.online_store_settings = {'themeColor': '#ffffff'}
        self.shop.store_policies = {'returnsPolicy': 'No returns'}
        self.shop.save()

        self.shop.refresh_from_db()
        self.assertEqual(self.shop.online_store_settings['themeColor'], '#ffffff')
        self.assertEqual(self.shop.store_policies['returnsPolicy'], 'No returns')

    def test_analytics_endpoint(self):
        response = self.client.get(f'/api/v1/shops/{self.shop.id}/analytics/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['storeViews'], 0)
        self.assertEqual(response.data['productClicks'], 0)

    def test_create_shop_frontend_schema(self):
        payload = {
            "name": "Frontend Shop",
            "location": "Dar es Salaam",
            "phone": "+255700000000",
            "whatsappNumber": "+255700000000",
            "description": "A test shop from frontend schema",
            "imageUrl": "https://example.com/image.jpg",
            "coverImage": "https://example.com/cover.jpg",
            "productCondition": "new",
            "categories": ["electronics", "gadgets"],
            "isWholesaleSupplier": True,
            "lat": -6.79,
            "lon": 39.20
        }
        response = self.client.post('/api/v1/shops/', data=payload, format='json')
        self.assertEqual(response.status_code, 201)
        
        # Verify the shop was created correctly
        shop = Shop.objects.get(name="Frontend Shop")
        self.assertEqual(shop.location, "Dar es Salaam")
        self.assertEqual(shop.whatsapp, "+255700000000")
        self.assertEqual(shop.imageUrl, "https://example.com/image.jpg")
        self.assertEqual(shop.productCategories, ["electronics", "gadgets"])
        self.assertTrue(shop.isWholesaleSupplier)
        
        # Verify owner role was created
        owner_role = shop.user_roles.filter(role='owner').first()
        self.assertIsNotNone(owner_role)
        self.assertEqual(owner_role.user, self.user)
        
        # Verify branch was created
        branch = shop.branches.first()
        self.assertIsNotNone(branch)
        self.assertEqual(branch.name, "Main Branch")
        self.assertTrue(branch.is_main)
        
        # Verify response structure contains correctly aliased fields
        data = response.data
        self.assertEqual(data['whatsappNumber'], "+255700000000")
        self.assertEqual(data['categories'], ["electronics", "gadgets"])
        self.assertEqual(data['ownerId'], str(self.user.id))

    def test_merchant_cannot_self_publish_to_marketplace(self):
        # Admin approval (``isPublic``) is the last check before products reach
        # the marketplace, so merchant writes must not be able to set it.
        response = self.client.post('/api/v1/shops/', data={
            'name': 'Sneaky Shop', 'isPublic': True,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(Shop.objects.get(name='Sneaky Shop').isPublic)

        response = self.client.patch(f'/api/v1/shops/{self.shop.id}/', {
            'isPublic': True, 'description': 'updated',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.isPublic)
        self.assertEqual(self.shop.description, 'updated')

    def test_platform_admin_can_approve_shop(self):
        admin = User.objects.create_user(
            username='approver', password='password', email='approver@example.com',
            is_staff=True, is_superuser=True)
        client = APIClient()
        client.force_authenticate(user=admin)
        response = client.patch(f'/api/v1/shops/{self.shop.id}/', {'isPublic': True}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.isPublic)


class UserTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='owner', password='password', email='owner@example.com', first_name='John', last_name='Doe')
        self.attendant = User.objects.create_user(username='attendant', password='password', email='attendant@example.com')
        
        self.shop = Shop.objects.create(name='Test Shop')
        # pyrefly: ignore [missing-import]
        from apps.shops.models import UserRole
        UserRole.objects.create(user=self.owner, shop=self.shop, role='owner')
        
    def test_get_user_roles(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(f'/api/v1/user-roles/?shopId={self.shop.id}')
        self.assertEqual(response.status_code, 200)
        
        data = response.data[0] if isinstance(response.data, list) else response.data['results'][0]
        self.assertEqual(data['userId'], str(self.owner.id))
        self.assertEqual(data['email'], 'owner@example.com')
        self.assertEqual(data['displayName'], 'John Doe')
        self.assertEqual(data['role'], 'owner')
        self.assertEqual(data['shopId'], str(self.shop.id))

    def test_get_my_roles_without_shop_id(self):
        """?mine=true restores the caller's roles after a reload, before any shop is known."""
        from apps.shops.models import UserRole
        UserRole.objects.create(user=self.attendant, shop=self.shop, role='attendant')
        self.client.force_authenticate(user=self.owner)

        response = self.client.get('/api/v1/user-roles/?mine=true')
        self.assertEqual(response.status_code, 200)
        data = response.data if isinstance(response.data, list) else response.data['results']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['userId'], str(self.owner.id))
        self.assertEqual(data[0]['role'], 'owner')

        # The shop-scoped browse still shows every member's role.
        response = self.client.get(f'/api/v1/user-roles/?shopId={self.shop.id}')
        data = response.data if isinstance(response.data, list) else response.data['results']
        self.assertEqual(len(data), 2)

    def test_create_and_delete_invitation(self):
        self.client.force_authenticate(user=self.owner)
        
        payload = {
            "email": "newuser@example.com",
            "shopId": self.shop.id,
            "role": "attendant",
        }
        response = self.client.post('/api/v1/invitations/', data=payload, format='json')
        self.assertEqual(response.status_code, 201)
        
        invite_id = response.data['id']
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['invitedBy'], str(self.owner.id))
        self.assertEqual(response.data['shopName'], 'Test Shop')
        
        # Test cancel invitation
        response = self.client.delete(f'/api/v1/invitations/{invite_id}/')
        self.assertEqual(response.status_code, 204)
        
        # Verify it's deleted
        # pyrefly: ignore [missing-import]
        from apps.shops.models import Invitation
        self.assertFalse(Invitation.objects.filter(id=invite_id).exists())


class ShopSettingsApiTests(TestCase):
    """PATCH/GET /api/v1/shops/{id}/settings/ — the merchant settings page contract."""

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username='settings-owner', password='password', email='settings-owner@example.com'
        )
        self.attendant = User.objects.create_user(
            username='settings-attendant', password='password', email='settings-attendant@example.com'
        )
        # legacy_id proves writes and reads address the shop by the app-visible id.
        self.shop = Shop.objects.create(name='Settings Shop', legacy_id='legacy-settings-1')
        UserRole.objects.create(user=self.owner, shop=self.shop, role='owner')
        UserRole.objects.create(user=self.attendant, shop=self.shop, role='attendant')

    def url(self, shop_id=None):
        return f'/api/v1/shops/{shop_id or self.shop.legacy_id}/settings/'

    def test_patch_round_trips_settings_groups(self):
        self.client.force_authenticate(user=self.owner)
        payload = {
            'onlineStore': {'enabled': True, 'themeColor': '#0f172a', 'layout': 'list'},
            'storePolicies': {'returnsPolicy': '7 days', 'shippingPolicy': 'Dar only'},
            'socialLinks': {'facebook': 'https://facebook.com/shop', 'whatsapp': '+255700000000'},
            'aiMarketing': {'enabled': True, 'tone': 'fun', 'musicVibe': 'upbeat'},
            'payoutDetails': {'provider': 'mpesa', 'accountName': 'Asha', 'accountNumber': '255700000'},
        }
        response = self.client.patch(self.url(), data=payload, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['shopId'], 'legacy-settings-1')
        self.assertEqual(response.data['payoutDetails']['provider'], 'mpesa')

        self.shop.refresh_from_db()
        self.assertEqual(self.shop.online_store_settings['themeColor'], '#0f172a')
        self.assertEqual(self.shop.store_policies['shippingPolicy'], 'Dar only')
        self.assertEqual(self.shop.social_links['facebook'], 'https://facebook.com/shop')
        self.assertEqual(self.shop.ai_marketing_settings['tone'], 'fun')
        self.assertEqual(self.shop.payout_details['accountNumber'], '255700000')

        # Reading back through the same action returns the app's camelCase keys.
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['onlineStore']['layout'], 'list')
        self.assertEqual(response.data['socialLinks']['whatsapp'], '+255700000000')
        self.assertEqual(response.data['aiMarketing']['musicVibe'], 'upbeat')

    def test_business_info_maps_to_legal_columns(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.patch(
            self.url(),
            data={'businessInfo': {'tin': '123-456', 'licenseNumber': 'LIC-9'}},
            format='json',
        )
        self.assertEqual(response.status_code, 200)

        self.shop.refresh_from_db()
        self.assertEqual(self.shop.tin_number, '123-456')
        self.assertEqual(self.shop.license_number, 'LIC-9')

        data = self.client.get(self.url()).data
        self.assertEqual(data['businessInfo']['tin'], '123-456')
        self.assertEqual(data['businessInfo']['licenseNumber'], 'LIC-9')
        self.assertEqual(data['businessInfo']['vat'], '')

    def test_attendant_cannot_write_settings(self):
        self.client.force_authenticate(user=self.attendant)
        response = self.client.patch(
            self.url(), data={'payoutDetails': {'provider': 'bank'}}, format='json'
        )
        self.assertEqual(response.status_code, 403)
        self.shop.refresh_from_db()
        self.assertNotEqual(self.shop.payout_details, {'provider': 'bank'})

    def test_member_can_read_settings(self):
        self.shop.payout_details = {'provider': 'none'}
        self.shop.save()
        self.client.force_authenticate(user=self.attendant)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['payoutDetails']['provider'], 'none')

    def test_outsider_cannot_reach_settings(self):
        outsider = User.objects.create_user(
            username='settings-outsider', password='password', email='settings-outsider@example.com'
        )
        self.client.force_authenticate(user=outsider)
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(
            self.client.patch(self.url(), data={'onlineStore': {'enabled': False}}, format='json').status_code,
            404,
        )


class ShopSalesTotalTests(TestCase):
    """The admin list's ``salesTotal`` column, derived from the summary cache."""

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='totals-owner', password='password')
        self.client.force_authenticate(user=self.owner)
        self.shop = Shop.objects.create(name='Totals Shop')
        UserRole.objects.create(user=self.owner, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main Branch')

    def _row(self):
        response = self.client.get('/api/v1/shops/')
        self.assertEqual(response.status_code, 200)
        return next(row for row in response.data['results'] if row['id'] == str(self.shop.id))

    def test_sales_total_sums_the_daily_summaries(self):
        today = timezone.localdate()
        DailySalesSummary.objects.create(
            shop=self.shop, branch=self.branch, date=today, total_sales=Decimal('1500.00')
        )
        DailySalesSummary.objects.create(
            shop=self.shop, branch=self.branch, date=today - timedelta(days=1),
            total_sales=Decimal('500.50'),
        )
        self.assertEqual(self._row()['salesTotal'], 2000.5)

    def test_sales_total_defaults_to_zero_without_sales(self):
        self.assertEqual(self._row()['salesTotal'], 0.0)
