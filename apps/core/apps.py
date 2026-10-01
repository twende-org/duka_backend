from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'apps.core'

    def ready(self):
        # Registers the Delivery App sync receivers (products/inventory/shops).
        from apps.core import signals  # noqa: F401
