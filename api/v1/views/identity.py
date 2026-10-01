"""Identity resolution for the signed-in account (legacy ``resolveIdentity``).

The frontend calls this once per login with the profile's phone/email so the
account gets attached to any CRM customer rows (and their historical
sales/orders) that were created before the buyer had an account.
"""
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

# pyrefly: ignore [missing-import]
from apps.crm.services import link_identity


class ResolveIdentityView(APIView):
    """``POST /api/v1/identity/resolve/`` -> ``{success, linkedCount}``."""

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        phone = (request.data.get('phone') or '').strip()
        email = (request.data.get('email') or '').strip()
        if not phone and not email:
            return Response(
                {'detail': ['Must provide either phone or email to resolve identity.']},
                status=status.HTTP_400_BAD_REQUEST,
            )

        linked_count = link_identity(request.user, phone=phone or None, email=email or None)
        return Response({'success': True, 'linkedCount': linked_count})
