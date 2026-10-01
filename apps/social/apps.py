from django.apps import AppConfig

class SocialConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.social'

    def ready(self):
        # Registers the publish_to_facebook -> auto-post signal receiver.
        from apps.social import signals  # noqa: F401
