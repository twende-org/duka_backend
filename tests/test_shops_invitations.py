import uuid

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, UserRole, Invitation
from apps.users.models import User


class InvitationLifecycleTests(TestCase):
    """
    Invitation flow parity with Firebase: the invited email's user lists their
    pending invitations, then accepts (role granted) or declines (no role).
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', email='owner@test.com', password='password'
        )
        self.invitee = User.objects.create_user(
            username='invitee', email='invitee@test.com', password='password'
        )
        self.stranger = User.objects.create_user(
            username='stranger', email='stranger@test.com', password='password'
        )
        self.shop = Shop.objects.create(name='Invite Shop')
        UserRole.objects.create(user=self.owner, shop=self.shop, role='owner')

        self.client = APIClient()

    def _invite(self, email='invitee@test.com', role='attendant', shop=None):
        self.client.force_authenticate(user=self.owner)
        return self.client.post('/api/v1/invitations/', {
            'email': email,
            'shopId': str((shop or self.shop).id),
            'role': role,
        }, format='json')

    def test_owner_can_invite_and_invitee_sees_it(self):
        response = self._invite()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['shopName'], 'Invite Shop')
        self.assertIsNotNone(response.data['createdAt'])
        self.assertIsNotNone(response.data['updatedAt'])
        invite_id = response.data['id']

        self.client.force_authenticate(user=self.invitee)
        response = self.client.get('/api/v1/invitations/?mine=true&status=pending')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(str(response.data['results'][0]['id']), str(invite_id))
        self.assertEqual(response.data['results'][0]['shopName'], 'Invite Shop')

    def test_email_is_normalized_and_matched_case_insensitively(self):
        response = self._invite(email='  New.Hire@Example.COM  ')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['email'], 'new.hire@example.com')

        invitee = User.objects.create_user(
            username='newhire', email='new.hire@example.com', password='password'
        )
        self.client.force_authenticate(user=invitee)
        response = self.client.post(f"/api/v1/invitations/{response.data['id']}/accept/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(UserRole.objects.filter(user=invitee, shop=self.shop).exists())

    def test_mine_filter_excludes_other_invitations(self):
        self._invite()

        self.client.force_authenticate(user=self.stranger)
        response = self.client.get('/api/v1/invitations/?mine=true')
        self.assertEqual(response.data['count'], 0)

        # The management list stays shop-scoped: no role means nothing visible.
        self.client.force_authenticate(user=self.invitee)
        response = self.client.get('/api/v1/invitations/')
        self.assertEqual(response.data['count'], 0)

    def test_accept_grants_role_marks_accepted_and_reveals_shop(self):
        invite_id = self._invite(role='manager').data['id']

        self.client.force_authenticate(user=self.invitee)
        response = self.client.post(f'/api/v1/invitations/{invite_id}/accept/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'accepted')

        role = UserRole.objects.get(user=self.invitee, shop=self.shop)
        self.assertEqual(role.role, 'manager')

        # Finally the invitee can see the shop...
        response = self.client.get('/api/v1/shops/')
        self.assertEqual([s['name'] for s in response.data['results']], ['Invite Shop'])

        # ...and the invitation leaves the pending list.
        response = self.client.get('/api/v1/invitations/?mine=true&status=pending')
        self.assertEqual(response.data['count'], 0)

    def test_accept_twice_is_400(self):
        invite_id = self._invite().data['id']
        self.client.force_authenticate(user=self.invitee)
        self.client.post(f'/api/v1/invitations/{invite_id}/accept/')
        response = self.client.post(f'/api/v1/invitations/{invite_id}/accept/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('no longer pending', str(response.data['detail']))

    def test_accept_foreign_invitation_is_404(self):
        invite_id = self._invite().data['id']
        self.client.force_authenticate(user=self.stranger)
        response = self.client.post(f'/api/v1/invitations/{invite_id}/accept/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(UserRole.objects.filter(user=self.stranger).exists())

    def test_accept_unknown_invitation_is_404(self):
        self.client.force_authenticate(user=self.invitee)
        response = self.client.post(f'/api/v1/invitations/{uuid.uuid4()}/accept/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_decline_marks_declined_without_granting_role(self):
        invite_id = self._invite().data['id']
        self.client.force_authenticate(user=self.invitee)
        response = self.client.post(f'/api/v1/invitations/{invite_id}/decline/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'declined')
        self.assertFalse(UserRole.objects.filter(user=self.invitee).exists())

    def test_decline_foreign_invitation_is_404(self):
        invite_id = self._invite().data['id']
        self.client.force_authenticate(user=self.stranger)
        response = self.client.post(f'/api/v1/invitations/{invite_id}/decline/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Invitation.objects.get(id=invite_id).status, 'pending')

    def test_decline_after_accept_is_400(self):
        invite_id = self._invite().data['id']
        self.client.force_authenticate(user=self.invitee)
        self.client.post(f'/api/v1/invitations/{invite_id}/accept/')
        response = self.client.post(f'/api/v1/invitations/{invite_id}/decline/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reinvite_after_accept_resets_to_pending(self):
        invite_id = self._invite(role='attendant').data['id']
        self.client.force_authenticate(user=self.invitee)
        self.client.post(f'/api/v1/invitations/{invite_id}/accept/')

        response = self._invite(role='manager')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(response.data['id']), str(invite_id))
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['role'], 'manager')

        self.assertEqual(
            Invitation.objects.filter(shop=self.shop, email='invitee@test.com').count(), 1
        )

        # Accepting the re-invite updates the existing role instead of duplicating it.
        self.client.force_authenticate(user=self.invitee)
        self.client.post(f'/api/v1/invitations/{invite_id}/accept/')
        roles = UserRole.objects.filter(user=self.invitee, shop=self.shop)
        self.assertEqual(roles.count(), 1)
        self.assertEqual(roles.first().role, 'manager')

    def test_list_filters_by_shop_and_status(self):
        other_shop = Shop.objects.create(name='Other Shop')
        UserRole.objects.create(user=self.owner, shop=other_shop, role='owner')
        self._invite()
        self._invite(email='other@test.com', shop=other_shop)

        self.client.force_authenticate(user=self.owner)
        response = self.client.get(f'/api/v1/invitations/?shopId={self.shop.id}')
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['email'], 'invitee@test.com')

        response = self.client.get('/api/v1/invitations/?status=pending')
        self.assertEqual(response.data['count'], 2)

        response = self.client.get('/api/v1/invitations/?status=accepted')
        self.assertEqual(response.data['count'], 0)
