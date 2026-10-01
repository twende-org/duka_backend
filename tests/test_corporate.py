"""Corporate procurement API tests.

Legacy stored departments and purchase orders under ``companies/{companyId}``
in Firestore and scoped reads in the browser. Django derives the company from
the caller's ``corporateProfile`` and answers 403 for cross-company requests,
so these tests pin the membership/role gates that the old client-side checks
used to provide.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.corporate.models import CorporateDepartment, CorporatePurchaseOrder
# pyrefly: ignore [missing-import]
from apps.users.models import User


def rows(response):
    """List payload rows, tolerating both the paginated envelope and a bare list."""
    data = response.json()
    return data['results'] if isinstance(data, dict) else data


class CorporateAPITestCase(TestCase):
    company_id = 'company-acme'

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='corp-admin', email='admin@acme.example.com', password='password',
            display_name='Ada Admin',
            corporate_profile={
                'companyId': self.company_id, 'companyName': 'Acme Ltd',
                'role': 'admin', 'creditLimit': 5000000,
            },
        )
        self.buyer = User.objects.create_user(
            username='corp-buyer', email='buyer@acme.example.com', password='password',
            display_name='Ben Buyer',
            corporate_profile={
                'companyId': self.company_id, 'companyName': 'Acme Ltd',
                'role': 'buyer', 'creditLimit': 5000000,
            },
        )
        self.approver = User.objects.create_user(
            username='corp-approver', email='appr@acme.example.com', password='password',
            corporate_profile={
                'companyId': self.company_id, 'companyName': 'Acme Ltd',
                'role': 'approver', 'creditLimit': 5000000,
            },
        )
        self.outsider = User.objects.create_user(
            username='corp-other', email='other@globex.example.com', password='password',
            corporate_profile={
                'companyId': 'company-globex', 'companyName': 'Globex',
                'role': 'admin',
            },
        )
        self.plain = User.objects.create_user(
            username='plain', email='plain@example.com', password='password'
        )
        self.staff = User.objects.create_user(
            username='staff', email='staff@example.com', password='password', is_staff=True
        )

    def auth(self, user):
        self.client.force_authenticate(user=user)

    def make_order(self, **overrides):
        fields = dict(
            company_id=self.company_id,
            shop_id='shop-1',
            shop_name='Duka Kuu',
            buyer_id='buyer@acme.example.com',
            buyer_name='Ben Buyer',
            buyer_phone='255700000002',
            department_id='dept-1',
            department_name='Manunuzi',
            items=[{'productId': 'p1', 'productName': 'Cement', 'quantity': 10,
                    'price': 15000, 'subtotal': 150000}],
            total_amount='150000.00',
        )
        fields.update(overrides)
        return CorporatePurchaseOrder.objects.create(**fields)


class DepartmentTests(CorporateAPITestCase):
    def test_members_read_their_company_departments(self):
        CorporateDepartment.objects.create(company_id=self.company_id, name='Manunuzi', budget=1000000)
        CorporateDepartment.objects.create(company_id='company-globex', name='Theirs', budget=1)

        self.auth(self.buyer)
        response = self.client.get(reverse('corporate-department-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        listed = rows(response)
        self.assertEqual([row['name'] for row in listed], ['Manunuzi'])
        self.assertEqual(listed[0]['companyId'], self.company_id)
        self.assertEqual(listed[0]['budget'], '1000000.00')

    def test_only_admin_can_create_a_department(self):
        self.auth(self.buyer)
        response = self.client.post(
            reverse('corporate-department-list'), {'name': 'Fedha', 'budget': 500000}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(CorporateDepartment.objects.exists())

        self.auth(self.admin)
        response = self.client.post(
            reverse('corporate-department-list'), {'name': 'Fedha', 'budget': 500000}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body['companyId'], self.company_id)
        self.assertEqual(body['spent'], '0.00')
        department = CorporateDepartment.objects.get()
        self.assertEqual(department.name, 'Fedha')
        self.assertEqual(str(department.budget), '500000.00')

    def test_outsider_cannot_read_or_create(self):
        self.auth(self.outsider)
        response = self.client.get(reverse('corporate-department-list'), {'companyId': self.company_id})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Writing into another company via the scope parameter is refused.
        response = self.client.post(
            reverse('corporate-department-list') + '?companyId=' + self.company_id,
            {'name': 'Sneaky'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(CorporateDepartment.objects.exists())

        # The body's companyId is read-only, so a stray id cannot retarget the write.
        response = self.client.post(
            reverse('corporate-department-list'),
            {'name': 'Sneaky', 'companyId': self.company_id},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()['companyId'], 'company-globex')
        self.assertEqual(CorporateDepartment.objects.get().company_id, 'company-globex')

    def test_non_member_is_denied(self):
        self.auth(self.plain)
        response = self.client.get(reverse('corporate-department-list'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_may_scope_any_company_and_must_name_one(self):
        CorporateDepartment.objects.create(company_id=self.company_id, name='Manunuzi')
        CorporateDepartment.objects.create(company_id='company-globex', name='Theirs')

        self.auth(self.staff)
        response = self.client.get(reverse('corporate-department-list'), {'companyId': 'company-globex'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row['name'] for row in rows(response)], ['Theirs'])

        response = self.client.get(reverse('corporate-department-list'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse('corporate-department-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class BuyerTests(CorporateAPITestCase):
    def test_lists_only_colleagues(self):
        self.auth(self.buyer)
        response = self.client.get(reverse('corporate-buyer-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        emails = {row['email'] for row in rows(response)}
        self.assertEqual(emails, {'admin@acme.example.com', 'buyer@acme.example.com', 'appr@acme.example.com'})
        first = rows(response)[0]
        self.assertIn('corporateProfile', first)
        self.assertNotIn('password', response.json())

    def test_outsider_cannot_peek_at_another_company(self):
        self.auth(self.outsider)
        response = self.client.get(reverse('corporate-buyer-list'), {'companyId': self.company_id})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PurchaseOrderTests(CorporateAPITestCase):
    def payload(self):
        return {
            'shopId': 'shop-1',
            'shopName': 'Duka Kuu',
            'buyerId': 'buyer@acme.example.com',
            'buyerName': 'Ben Buyer',
            'buyerPhone': '255700000002',
            'departmentId': 'dept-1',
            'departmentName': 'Manunuzi',
            'items': [{'productId': 'p1', 'productName': 'Cement', 'quantity': 10,
                       'price': 15000, 'subtotal': 150000}],
            'totalAmount': '150000.00',
        }

    def test_create_stamps_company_and_pending_status(self):
        self.auth(self.buyer)
        response = self.client.post(
            reverse('corporate-purchase-order-list'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body['companyId'], self.company_id)
        self.assertEqual(body['approvalStatus'], 'pending_approval')
        self.assertEqual(body['totalAmount'], '150000.00')
        self.assertEqual(body['departmentName'], 'Manunuzi')

        order = CorporatePurchaseOrder.objects.get()
        self.assertEqual(order.company_id, self.company_id)
        self.assertEqual(order.approval_status, 'pending_approval')
        self.assertEqual(order.items[0]['productName'], 'Cement')

    def test_create_ignores_a_foreign_company_in_the_body(self):
        self.auth(self.buyer)
        payload = dict(self.payload(), companyId='company-globex')
        response = self.client.post(
            reverse('corporate-purchase-order-list'), payload, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()['companyId'], self.company_id)
        self.assertEqual(CorporatePurchaseOrder.objects.get().company_id, self.company_id)

    def test_list_is_scoped_and_retrieve_404s_across_companies(self):
        mine = self.make_order()
        theirs = self.make_order(company_id='company-globex')

        self.auth(self.buyer)
        response = self.client.get(reverse('corporate-purchase-order-list'))
        self.assertEqual([row['id'] for row in rows(response)], [str(mine.pk)])

        response = self.client.get(reverse('corporate-purchase-order-detail', args=[str(theirs.pk)]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_buyer_cannot_approve(self):
        order = self.make_order()
        self.auth(self.buyer)
        response = self.client.post(
            reverse('corporate-purchase-order-approve', args=[str(order.pk)]),
            {},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        order.refresh_from_db()
        self.assertEqual(order.approval_status, 'pending_approval')

    def test_approver_approves_and_stamps_identity(self):
        order = self.make_order()
        self.auth(self.approver)
        response = self.client.post(
            reverse('corporate-purchase-order-approve', args=[str(order.pk)]),
            {'approverId': 'appr@acme.example.com'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body['approvalStatus'], 'APPROVED')
        self.assertEqual(body['approverId'], 'appr@acme.example.com')
        self.assertIn('approvedAt', body)

        order.refresh_from_db()
        self.assertEqual(order.approval_status, 'APPROVED')
        self.assertIsNotNone(order.approved_at)

    def test_admin_rejects(self):
        order = self.make_order()
        self.auth(self.admin)
        response = self.client.post(
            reverse('corporate-purchase-order-reject', args=[str(order.pk)]), {}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['approvalStatus'], 'REJECTED')

        order.refresh_from_db()
        self.assertEqual(order.approval_status, 'REJECTED')
        self.assertIsNotNone(order.rejected_at)

    def test_outsider_is_isolated_to_their_own_company(self):
        order = self.make_order()
        self.auth(self.outsider)

        response = self.client.post(
            reverse('corporate-purchase-order-approve', args=[str(order.pk)]), {}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Asking to write into another company is refused outright.
        response = self.client.post(
            reverse('corporate-purchase-order-list') + '?companyId=' + self.company_id,
            self.payload(),
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Without the foreign id the outsider files under their own company.
        response = self.client.post(
            reverse('corporate-purchase-order-list'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()['companyId'], 'company-globex')

    def test_staff_scopes_the_company_explicitly(self):
        order = self.make_order()
        self.auth(self.staff)
        # Staff with no profile and no ?companyId= cannot resolve a scope.
        response = self.client.post(
            reverse('corporate-purchase-order-approve', args=[str(order.pk)]), {}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(
            reverse('corporate-purchase-order-approve', args=[str(order.pk)])
            + '?companyId=' + self.company_id,
            {'approverId': 'staff@example.com'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['approverId'], 'staff@example.com')

        order.refresh_from_db()
        self.assertEqual(order.approval_status, 'APPROVED')

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse('corporate-purchase-order-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
