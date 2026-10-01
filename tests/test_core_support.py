from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

# pyrefly: ignore [missing-import]
from apps.core.models import Announcement, SupportTicket
# pyrefly: ignore [missing-import]
from apps.sales.models import Order
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.users.models import User


class AnnouncementApiTests(TestCase):
    """Banner reads are open to any signed-in user; management is admin-only."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='merchant', email='merchant@test.com', password='password', display_name='Merchant'
        )
        self.admin = User.objects.create_user(
            username='admin', email='admin@test.com', password='password', is_staff=True
        )
        self.live = Announcement.objects.create(title='Live', message='Shown', type='info', active=True)
        self.hidden = Announcement.objects.create(title='Hidden', message='Archived', type='warning', active=False)

        self.client = APIClient()

    def test_anonymous_read_is_401(self):
        response = self.client.get('/api/v1/announcements/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_merchant_sees_only_active_rows(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/v1/announcements/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['title'], 'Live')

    def test_active_param_narrows_the_banner_query(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get('/api/v1/announcements/?active=true&page_size=1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], str(self.live.id))

    def test_admin_sees_every_row(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get('/api/v1/announcements/')
        self.assertEqual(response.data['count'], 2)

    def test_merchant_cannot_create(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post('/api/v1/announcements/', {'title': 'Nope', 'message': 'x'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_create_stamps_author_camel_and_snake_keys(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            '/api/v1/announcements/',
            {'title': 'Maintenance', 'message': 'Late night', 'type': 'warning'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['createdByEmail'], 'admin@test.com')
        self.assertEqual(response.data['createdBy'], str(self.admin.id))
        self.assertTrue(response.data['active'])
        self.assertEqual(response.data['message'], 'Late night')

    def test_admin_toggle_and_delete(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            f'/api/v1/announcements/{self.hidden.id}/', {'active': True}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.hidden.refresh_from_db()
        self.assertTrue(self.hidden.active)

        response = self.client.delete(f'/api/v1/announcements/{self.live.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Announcement.objects.filter(id=self.live.id).exists())


class SupportTicketApiTests(TestCase):
    """Anyone signed in can file a ticket; only platform admins work the queue."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='merchant', email='merchant@test.com', password='password', display_name='Merchant'
        )
        self.admin = User.objects.create_user(
            username='admin', email='admin@test.com', password='password', is_staff=True
        )
        self.client = APIClient()

    def test_submit_uses_request_identity(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            '/api/v1/support-tickets/',
            {'message': 'Kitu hakifanyi kazi', 'category': 'Bug', 'shopId': 'shop-fs-1', 'route': '/app/products'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # A caller cannot spoof the identity fields; they come off the JWT user.
        self.assertEqual(response.data['userEmail'], 'merchant@test.com')
        self.assertEqual(response.data['userName'], 'Merchant')
        self.assertEqual(response.data['userId'], str(self.user.id))
        self.assertEqual(response.data['shopId'], 'shop-fs-1')
        self.assertFalse(response.data['resolved'])

    def test_empty_message_is_rejected(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post('/api/v1/support-tickets/', {'message': '   '}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anonymous_submit_is_401(self):
        response = self.client.post('/api/v1/support-tickets/', {'message': 'hi'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_merchant_cannot_list(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/v1/support-tickets/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_lists_newest_first_and_filters_unresolved(self):
        SupportTicket.objects.create(user=self.user, user_email='merchant@test.com', message='old', resolved=True)
        SupportTicket.objects.create(user=self.user, user_email='merchant@test.com', message='new')

        self.client.force_authenticate(user=self.admin)
        response = self.client.get('/api/v1/support-tickets/')
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(response.data['results'][0]['message'], 'new')

        response = self.client.get('/api/v1/support-tickets/?resolved=false&page_size=1')
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['message'], 'new')

    def test_admin_resolves_ticket(self):
        ticket = SupportTicket.objects.create(user=self.user, message='fix me')
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            f'/api/v1/support-tickets/{ticket.id}/', {'resolved': True}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ticket.refresh_from_db()
        self.assertTrue(ticket.resolved)


class OrderStatusFilterTests(TestCase):
    """The shell's pending-orders badge counts via ``?status=pending&page_size=1``."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', email='owner@test.com', password='password')
        self.shop = Shop.objects.create(name='Shop A')
        self.branch = Branch.objects.create(shop=self.shop, name='Main', is_main=True)
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        Order.objects.create(shop=self.shop, branch=self.branch, status='pending', total_amount=1000)
        Order.objects.create(shop=self.shop, branch=self.branch, status='pending', total_amount=2000)
        Order.objects.create(shop=self.shop, branch=self.branch, status='paid', total_amount=3000)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_status_filter_returns_matching_count(self):
        response = self.client.get(f'/api/v1/orders/?shop_id={self.shop.id}&status=pending&page_size=1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 1)

    def test_unknown_status_returns_zero(self):
        response = self.client.get(f'/api/v1/orders/?shop_id={self.shop.id}&status=returned')
        self.assertEqual(response.data['count'], 0)
