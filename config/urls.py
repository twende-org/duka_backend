from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, include, re_path
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET
from django.views.static import serve as serve_files
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.users.views import (
    EmailLoginView,
    FirebaseLoginView,
    GoogleLoginView,
    PasswordUpdateView,
    RegisterView,
    UserProfileUpdateView,
)

urlpatterns = [
    path('admin/', admin.site.urls),

    # Liveness/readiness probe for load balancers, Traefik and the container
    # healthcheck. Unauthenticated and DB-touching-free on purpose.
    path('healthz/', lambda request: JsonResponse({'status': 'ok'}), name='healthz'),

    # JWT Authentication Endpoints
    path('api/auth/register/', RegisterView.as_view(), name='register'),
    path('api/auth/login/', EmailLoginView.as_view(), name='email_login'),
    path('api/auth/google/', GoogleLoginView.as_view(), name='google_login'),
    path('api/auth/firebase/', FirebaseLoginView.as_view(), name='firebase_login'),
    path('api/users/me/', UserProfileUpdateView.as_view(), name='user_profile'),
    path('api/users/me/password/', PasswordUpdateView.as_view(), name='user_password_update'),
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/token/verify/', TokenVerifyView.as_view(), name='token_verify'),

    # OpenAPI Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),

    # API v1
    path('api/v1/', include('api.v1.urls')),
]

# Media uploads (product photos, reels). django.views.static.serve is documented
# as dev-only, but gunicorn behind Traefik has no other media server and the
# upstream comment targets exactly this gap; the cache header keeps repeat
# fetches off the container. Production reels are still served from
# REEL_PUBLIC_BASE_URL when that is set.
urlpatterns += [
    re_path(
        r'^media/(?P<path>.*)$',
        require_GET(cache_control(max_age=86400)(serve_files)),
        {'document_root': settings.MEDIA_ROOT},
    ),
]
