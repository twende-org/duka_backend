"""Install the pg_trgm extension the marketplace search relies on.

PostgreSQL-only. The project has to keep working on SQLite (local dev and the
test suite), where Django migrations cannot express vendor-conditional index
DDL, so every statement here and in the app search migrations is guarded by the
connection vendor instead. `CREATE EXTENSION pg_trgm` is a no-op on SQLite.
"""
from django.db import migrations


def install_trigram(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')


def drop_trigram(apps, schema_editor):
    # Reversed only after the GIN indexes that depend on it are dropped
    # (products/shops search migrations depend on this one, so they unwind first).
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP EXTENSION IF EXISTS pg_trgm')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(install_trigram, drop_trigram),
    ]
