"""pg_trgm GIN indexes on the product columns the marketplace search reads.

Raw DDL, PostgreSQL-only, on purpose: ``django.contrib.postgres.indexes.GinIndex``
cannot live in model ``Meta`` here because the SQLite schema editor recreates
every Meta index when it remakes a table, and would emit ``USING gin`` there.
Kept out of ``Meta`` so ``makemigrations --check`` stays clean; the lookups that
consume these indexes are ``icontains`` and ``trigram_word_similar`` in
``apps.core.search``.
"""
from django.db import migrations

_INDEXES = (
    ('product_name_trgm', 'name'),
    ('product_sku_trgm', 'sku'),
    ('product_barcode_trgm', 'barcode'),
    ('product_brand_trgm', 'brand'),
)


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for name, column in _INDEXES:
        schema_editor.execute(
            f'CREATE INDEX IF NOT EXISTS {name} '
            f'ON products_product USING gin ({column} gin_trgm_ops)'
        )


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for name, _ in _INDEXES:
        schema_editor.execute(f'DROP INDEX IF EXISTS {name}')


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0013_category_uniqueness'),
        ('core', '0002_search_trigram'),
    ]

    operations = [
        migrations.RunPython(create_indexes, drop_indexes),
    ]
