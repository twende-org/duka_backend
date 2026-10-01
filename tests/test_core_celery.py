from django.conf import settings
from django.test import SimpleTestCase

# pyrefly: ignore [missing-import]
from config import celery_app
# pyrefly: ignore [missing-import]
from apps.core.tasks import ping


class CeleryConfigTests(SimpleTestCase):
    """The Celery app must load its configuration from Django settings."""

    def test_app_is_wired_into_django(self):
        self.assertEqual(celery_app.main, 'config')

    def test_settings_namespace_is_loaded(self):
        self.assertEqual(celery_app.conf.broker_url, settings.CELERY_BROKER_URL)
        self.assertEqual(celery_app.conf.accept_content, ['json'])
        self.assertEqual(celery_app.conf.task_serializer, 'json')
        self.assertEqual(celery_app.conf.result_serializer, 'json')
        self.assertEqual(str(celery_app.conf.timezone), settings.TIME_ZONE)

    def test_task_autodiscovery_registers_app_tasks(self):
        self.assertIn('apps.core.tasks.ping', celery_app.tasks)

    def test_tests_run_eagerly_without_a_broker(self):
        self.assertTrue(settings.CELERY_TASK_ALWAYS_EAGER)

    def test_ping_runs_eagerly(self):
        self.assertEqual(ping(), 'pong')
        self.assertEqual(ping.apply().get(), 'pong')
        self.assertEqual(ping.delay().get(), 'pong')
