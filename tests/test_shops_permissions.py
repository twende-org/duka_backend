from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
# pyrefly: ignore [missing-import]
from apps.users.models import User
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.crm.models import Customer


class ShopAccessIsolationTests(TestCase):
    """
    Regression tests for shop-scoped authorization (IDOR closure):
    a user must never read or write another shop's data, even by passing
    that shop's id explicitly.
    """

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', email='alice@test.com', password='password')
        self.bob = User.objects.create_user(username='bob', email='bob@test.com', password='password')
        self.shop_a = Shop.objects.create(name='Shop A')
        self.shop_b = Shop.objects.create(name='Shop B')
        UserRole.objects.create(user=self.alice, shop=self.shop_a, role='owner')
        UserRole.objects.create(user=self.bob, shop=self.shop_b, role='owner')
        self.bob_customer = Customer.objects.create(shop=self.shop_b, name='Bob Customer')

        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    def test_foreign_shop_list_returns_nothing(self):
        response = self.client.get(f'/api/v1/customers/?shop_id={self.shop_b.id}')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

    def test_foreign_shop_detail_is_404(self):
        response = self.client.get(f'/api/v1/customers/{self.bob_customer.id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_foreign_shop_update_is_404(self):
        response = self.client.patch(f'/api/v1/customers/{self.bob_customer.id}/', {'name': 'Hacked'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.bob_customer.refresh_from_db()
        self.assertEqual(self.bob_customer.name, 'Bob Customer')

    def test_create_in_foreign_shop_is_403(self):
        response = self.client.post('/api/v1/customers/', {
            'shop_id': str(self.shop_b.id),
            'name': 'Infiltrator',
            'phone': '+255700000000',
            'address': 'Nowhere',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Customer.objects.filter(name='Infiltrator').exists())

    def test_foreign_shop_not_in_users_shop_list(self):
        response = self.client.get('/api/v1/shops/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [s['name'] for s in response.data['results']]
        self.assertEqual(names, ['Shop A'])

    def test_command_center_rejects_foreign_shop(self):
        response = self.client.get(f'/api/v1/analytics/command-center/?shop_id={self.shop_b.id}')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pos_sale_in_foreign_shop_is_denied(self):
        # Give shop B a real branch + stock so only authorization can fail.
        from apps.products.models import Product, Inventory
        from apps.shops.models import Branch
        branch = Branch.objects.create(shop=self.shop_b, name='B Branch')
        product = Product.objects.create(
            shop=self.shop_b, name='B Product', buying_price=1, selling_price=2
        )
        Inventory.objects.create(product=product, branch=branch, quantity=10)

        response = self.client.post('/api/v1/sales/', {
            'branch_id': str(branch.id),
            'payment_method': 'cash',
            'items': [{'product_id': str(product.id), 'quantity': 1, 'unit_price': 2}],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RoleManagementPermissionTests(TestCase):
    """Only owners/managers may assign roles or invite staff (spec §8)."""

    def setUp(self):
        self.owner = User.objects.create_user(username='owner', email='owner@test.com', password='password')
        self.attendant = User.objects.create_user(username='attendant', email='att@test.com', password='password')
        self.newcomer = User.objects.create_user(username='newcomer', email='new@test.com', password='password')
        self.shop = Shop.objects.create(name='Role Shop')
        UserRole.objects.create(user=self.owner, shop=self.shop, role='owner')
        UserRole.objects.create(user=self.attendant, shop=self.shop, role='attendant')

        self.client = APIClient()

    def test_attendant_cannot_assign_roles(self):
        self.client.force_authenticate(user=self.attendant)
        response = self.client.post('/api/v1/user-roles/', {
            'shopId': str(self.shop.id),
            'userId': str(self.newcomer.id),
            'role': 'manager',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_attendant_cannot_invite(self):
        self.client.force_authenticate(user=self.attendant)
        response = self.client.post('/api/v1/invitations/', {
            'email': 'hire@test.com',
            'role': 'attendant',
            'shopId': str(self.shop.id),
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_invite(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post('/api/v1/invitations/', {
            'email': 'hire@test.com',
            'role': 'attendant',
            'shopId': str(self.shop.id),
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
