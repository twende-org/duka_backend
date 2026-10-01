"""Port of the legacy ``runDeliveryAppMigration`` HTTP function.

Pushes every product into the Delivery App project, translating the schema on
the way. Unlike the Cloud Function this is a management command (the legacy
endpoint was unauthenticated HTTP and the deployment has no anonymous route for
it) and it defaults to a dry run.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# pyrefly: ignore [missing-import]
from apps.core import delivery_sync
# pyrefly: ignore [missing-import]
from apps.products.models import Product


class Command(BaseCommand):
    help = (
        'Mirror products into the Delivery App Firebase project '
        '(legacy runDeliveryAppMigration). Dry run unless --apply is passed.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Actually write to the Delivery App project.',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help=(
                'Include products whose publish_to_delivery_app toggle is off. '
                'The legacy function pushed every product; the default here only '
                'pushes products the merchant opted in.'
            ),
        )
        parser.add_argument('--limit', type=int, default=None, help='Stop after N products.')

    def handle(self, *args, **options):
        queryset = Product.objects.select_related('shop', 'category').order_by('created_at')
        if not options['all']:
            queryset = queryset.filter(publish_to_delivery_app=True)
        if options['limit']:
            queryset = queryset[: options['limit']]
        products = list(queryset)

        if not options['apply']:
            self.stdout.write(
                f'Dry run: {len(products)} product(s) would be pushed to '
                f'{settings.DELIVERY_APP_PROJECT_ID}. Re-run with --apply to write.'
            )
            return

        if not settings.DELIVERY_APP_SYNC_ENABLED:
            raise CommandError(
                'DELIVERY_APP_SYNC_ENABLED is off; refusing to write to the Delivery App.'
            )

        pushed = 0
        failed = 0
        for product in products:
            try:
                delivery_sync.push_product(product)
                pushed += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f'Error migrating product {product.pk}: {exc}')

        self.stdout.write(
            f'Successfully migrated {pushed} products to the Delivery App '
            f'without duplicates.'
        )
        if failed:
            raise CommandError(f'{failed} product(s) failed to migrate.')
