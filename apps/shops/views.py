from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from .models import Shop, UserRole
from .serializers import ShopSettingsSerializer
from django.core.exceptions import PermissionDenied

class ShopSettingsUpdateView(RetrieveUpdateAPIView):
    serializer_class = ShopSettingsSerializer
    permission_classes = [IsAuthenticated]
    queryset = Shop.objects.all()

    def get_object(self):
        obj = super().get_object()
        # Verify user has access to update this shop settings
        # Usually only 'owner' or 'manager' roles should have access
        user = self.request.user
        try:
            role = UserRole.objects.get(user=user, shop=obj)
            if role.role not in ['owner', 'manager']:
                raise PermissionDenied("You do not have permission to modify settings for this shop.")
        except UserRole.DoesNotExist:
            raise PermissionDenied("You do not have access to this shop.")
        
        return obj
