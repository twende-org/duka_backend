"""pg_trgm GIN indexes on the shop columns the storefront search reads.

Same contract as products 0014_search_indexes: raw vendor-guarded DDL because
GinIndex cannot sit in model Meta on a dual SQLite/Postgres project (SQLite
table remakes would replay it as ``USING gin``).
"""
from django.db import migrations

_INDEXES = (
    ('shop_name_trgm', 'name'),
    ('shop_description_trgm', 'description'),
    ('shop_location_trgm', 'location'),
)


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for name, column in _INDEXES:
        schema_editor.execute(
            f'CREATE INDEX IF NOT EXISTS {name} '
            f'ON shops_shop USING gin ({column} gin_trgm_ops)'
        )


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for name, _ in _INDEXES:
        schema_editor.execute(f'DROP INDEX IF EXISTS {name}')


class Migration(migrations.Migration):

    dependencies = [
        ('shops', '0019_shop_payout_details'),
        ('core', '0002_search_trigram'),
    ]

    operations = [
        migrations.RunPython(create_indexes, drop_indexes),
    ]
