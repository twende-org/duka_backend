"""pg_trgm GIN indexes on the catalogue columns the tap-to-add picker searches.

Raw DDL, PostgreSQL-only, on purpose — the same rule ``0014_search_indexes``
documents for ``products_product``: ``GinIndex`` cannot live in model ``Meta``
because the SQLite schema editor recreates every Meta index when it remakes a
table and would emit ``USING gin`` there, and keeping it out of ``Meta`` is what
lets ``makemigrations --check`` pass. The lookup these indexes serve is
``icontains`` inside ``apps.core.search``, reached through
``apps.products.selectors.search_master_products``.

``category_name``/``niche`` are intentionally absent: the picker filters those
with exact (``iexact``/``equal``) matches, which a trigram index cannot serve —
their plain btree indexes from ``0015`` already do.
"""
from django.db import migrations

_INDEXES = (
    ('masterproduct_name_trgm', 'name'),
    ('masterproduct_brand_trgm', 'brand'),
    ('masterproduct_barcode_trgm', 'barcode'),
)


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for name, column in _INDEXES:
        schema_editor.execute(
            f'CREATE INDEX IF NOT EXISTS {name} '
            f'ON products_masterproduct USING gin ({column} gin_trgm_ops)'
        )


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for name, _ in _INDEXES:
        schema_editor.execute(f'DROP INDEX IF EXISTS {name}')


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0015_masterproduct_product_requires_imei_and_more'),
        ('core', '0002_search_trigram'),
    ]

    operations = [
        migrations.RunPython(create_indexes, drop_indexes),
    ]
