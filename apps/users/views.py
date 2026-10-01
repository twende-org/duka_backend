from django.conf import settings
from rest_framework import status
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from google.auth import exceptions as google_exceptions
from google.auth.transport import requests
from google.oauth2 import id_token
from google.oauth2.id_token import verify_firebase_token

from .models import User
from .serializers import (
    EmailTokenObtainPairSerializer,
    PasswordUpdateSerializer,
    RegisterSerializer,
    UserProfileSerializer,
    find_user_by_email,
    user_auth_payload,
)


def token_response(user, created):
    refresh = RefreshToken.for_user(user)
    return {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'is_new_user': created,
        'user': user_auth_payload(user),
    }


class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(token_response(user, created=True), status=status.HTTP_201_CREATED)


class EmailLoginView(TokenObtainPairView):
    serializer_class = EmailTokenObtainPairSerializer


class GoogleLoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        if not settings.GOOGLE_CLIENT_ID:
            return Response(
                {"error": "Google sign-in is not configured"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        token = request.data.get("idToken")
        if not token:
            return Response({"error": "No ID token provided"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Verify the token with Google
            idinfo = id_token.verify_oauth2_token(
                token, requests.Request(), settings.GOOGLE_CLIENT_ID
            )

            # Get user info from the token
            email = idinfo.get("email")
            if not email:
                return Response({"error": "Token did not contain an email"}, status=status.HTTP_400_BAD_REQUEST)
                
            # You can also extract given_name, family_name, etc.
            first_name = idinfo.get("given_name", "")
            last_name = idinfo.get("family_name", "")
            display_name = idinfo.get("name") or f"{first_name} {last_name}".strip() or email.split("@")[0]

            # Get or create the Django user
            user = find_user_by_email(email)
            created = user is None
            if created:
                user = User.objects.create_user(
                    username=email,
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    display_name=display_name,
                    # Frontend sends new Google users through workspace onboarding.
                    account_type="unassigned",
                )

            # The Firebase uid is the key the portal data was written under
            # (``customerUserId`` on sales/orders). For Google-provider users it
            # equals the Google ``sub``, so fill it in when the row has none —
            # without clobbering a uid that came from another provider.
            firebase_uid = (idinfo.get("sub") or "").strip()
            if firebase_uid and not user.firebase_uid:
                user.firebase_uid = firebase_uid
                user.save(update_fields=["firebase_uid"])

            return Response(token_response(user, created), status=status.HTTP_200_OK)

        except ValueError as e:
            # Invalid token
            return Response({"error": f"Invalid token: {str(e)}"}, status=status.HTTP_401_UNAUTHORIZED)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class FirebaseLoginView(APIView):
    """Exchanges a Firebase ID token from the duka-smart-16c8e project for DRF JWTs.

    Lets the Google-popup frontend keep its Firebase session while calling the
    Django API during the migration.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        token = request.data.get("idToken")
        if not token:
            return Response({"error": "No ID token provided"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            idinfo = verify_firebase_token(
                token, requests.Request(), audience=settings.FIREBASE_PROJECT_ID
            )
        except (ValueError, google_exceptions.GoogleAuthError) as e:
            return Response({"error": f"Invalid Firebase token: {str(e)}"}, status=status.HTTP_401_UNAUTHORIZED)

        email = (idinfo.get("email") or "").strip().lower()
        if not email:
            return Response({"error": "Token did not contain an email"}, status=status.HTTP_400_BAD_REQUEST)

        user = find_user_by_email(email)
        created = user is None
        if created:
            display_name = idinfo.get("name") or email.split("@")[0]
            user = User.objects.create_user(
                username=email,
                email=email,
                display_name=display_name,
                account_type="unassigned",
            )

        # The Firebase uid is the key the portal data was written under
        # (``customerUserId`` on sales/orders), so keep it on the user row.
        firebase_uid = (idinfo.get("user_id") or idinfo.get("sub") or "").strip()
        if firebase_uid and user.firebase_uid != firebase_uid:
            user.firebase_uid = firebase_uid
            user.save(update_fields=["firebase_uid"])

        return Response(token_response(user, created), status=status.HTTP_200_OK)


class UserProfileUpdateView(RetrieveUpdateAPIView):
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user

    def perform_update(self, serializer):
        previous_phone = self.get_object().phone
        user = serializer.save()
        # Mirrors the linkIdentity ``users/{uid}`` trigger: a new phone means this
        # account may now own CRM rows (and their sales/orders) created earlier.
        if user.phone and user.phone != previous_phone:
            from apps.crm.services import link_identity

            link_identity(user, phone=user.phone)


class PasswordUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = PasswordUpdateSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            user = request.user
            user.set_password(serializer.validated_data['new_password'])
            user.save()
            return Response({"message": "Password updated successfully."}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
