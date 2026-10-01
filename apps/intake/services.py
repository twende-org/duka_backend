"""Intake pipeline: staging batch -> ProductDraft rows -> live product tables.

Deterministic first: custom wholesale QR payloads are parsed locally and skip
every external call. Only when no QR is recovered do image sources go to the
vision model. Parsing runs on a daemon thread so the endpoint answers 202
immediately; the client polls the batch until it leaves processing.

Uploaded images are copied into ``intake/<batch id>/`` at creation so the
worker never touches the (already-finished) request, and are deleted once the
batch reaches a terminal state — the drafts carry the data, storage does not
accumulate. Remote URLs are streamed in chunks with the same byte cap.
"""
import base64
import logging
import threading
import uuid

import requests
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import transaction

from apps.core.assistant import AssistantError
from apps.products.models import Branch, Category, Product
from apps.products.services import adjust_inventory

from .ai_extract import extract_invoice_items
from .barcode import decode_qr_from_image
from .models import IntakeBatch, ProductDraft
from .qr import parse_wholesale_qr
from .tra import classify_tra

logger = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 8 * 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
URL_FETCH_TIMEOUT = 30


# --------------------------------------------------------------------------
# Batch creation
# --------------------------------------------------------------------------

def create_intake_batch(shop, created_by, images=(), image_urls=(), qr_payloads=(), note=''):
    """Persist a staging ``IntakeBatch`` with its raw sources; status pending.

    Returns the batch. Raises ``ValidationError`` when no usable source was
    supplied or a URL is not http(s) — the caller surfaces that as a 400.
    """
    sources = []
    for payload in qr_payloads:
        text = str(payload or '').strip()
        if text:
            sources.append({'kind': 'qr', 'ref': text})
    for upload in images:
        stored_name = default_storage.save(
            f'intake/{shop.id}/{uuid_path(upload.name)}', upload,
        )
        sources.append({
            'kind': 'image',
            'ref': stored_name,
            'name': upload.name,
            'content_type': upload.content_type or '',
        })
    for raw_url in image_urls:
        url = str(raw_url or '').strip()
        if not url:
            continue
        if not url.lower().startswith(('http://', 'https://')):
            raise ValidationError({'imageUrls': ['Only http(s) media URLs are accepted.']})
        sources.append({'kind': 'url', 'ref': url, 'name': url.rsplit('/', 1)[-1] or url})

    if not sources:
        raise ValidationError(
            {'detail': ['Provide at least one image, media URL or QR payload.']})

    kinds = {source['kind'] for source in sources}
    if kinds == {'qr'}:
        source_type = 'qr'
    elif kinds == {'url'}:
        source_type = 'url'
    else:
        source_type = 'image'

    return IntakeBatch.objects.create(
        shop=shop,
        created_by=created_by,
        status='pending',
        source_type=source_type,
        sources=sources,
        source_note=note or '',
    )


def uuid_path(filename):
    """Storage key for an uploaded source: ``<hex>.<safe extension>``."""
    from api.v1.views.uploads import ALLOWED_IMAGE_TYPES
    suffix = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    extension = ALLOWED_IMAGE_TYPES.get(suffix)
    if extension is None:
        # Clients compress to JPEG before upload; treat unknown as .jpg.
        extension = '.jpg'
    return f'{uuid.uuid4().hex}{extension}'


# --------------------------------------------------------------------------
# Background processing
# --------------------------------------------------------------------------

def dispatch_intake_processing(batch_id):
    """Run :func:`process_intake_batch` on a daemon thread (dev has no broker)."""
    thread = threading.Thread(
        target=process_intake_batch, args=(batch_id,),
        name=f'intake-{batch_id}', daemon=True,
    )
    thread.start()
    return thread


def process_intake_batch(batch_id):
    """Extract items for one batch and write its ProductDraft rows.

    QR payloads (submitted or decoded locally out of an image) win outright;
    AI vision only sees images when no QR was recovered. Any failure flips the
    batch to ``failed`` with the reason — the endpoint already answered.
    """
    batch = IntakeBatch.objects.filter(id=batch_id).first()
    if batch is None or batch.status not in ('pending', 'failed'):
        return
    batch.status = 'processing'
    batch.error_message = ''
    batch.save(update_fields=['status', 'error_message', 'updated_at'])

    stored_paths = [s['ref'] for s in batch.sources if s.get('kind') == 'image']
    usage = {}
    try:
        items, engine = _extract_items(batch, usage)
        grouped = _group_duplicates(items)
        ProductDraft.objects.filter(batch=batch).delete()
        ProductDraft.objects.bulk_create(
            ProductDraft(
                batch=batch,
                name_en=item['name_en'][:255],
                name_sw=(item.get('name_sw') or '')[:255],
                unit=(item.get('unit') or 'pcs')[:100],
                quantity=int(round(float(item.get('quantity') or 0))),
                buying_price=item.get('unit_cost') or 0,
                selling_price=item.get('unit_price') or 0,
                category_name=(item.get('category') or '')[:255],
                ai_confidence_score=item.get('confidence', 0.0),
                **dict(zip(('tra_item_code', 'tax_rate_percent'), classify_tra(item['name_en']))),
            )
            for item in grouped
        )
        batch.engine_used = engine
        batch.item_count = len(grouped)
        batch.status = 'completed'
    except AssistantError as exc:
        logger.warning('Intake batch %s failed: %s', batch_id, exc)
        batch.status = 'failed'
        batch.error_message = str(exc)
    except Exception as exc:  # worker threads must never die silently
        logger.exception('Intake batch %s crashed', batch_id)
        batch.status = 'failed'
        batch.error_message = str(exc)
    finally:
        batch.ai_usage = usage or None
        batch.save(update_fields=[
            'engine_used', 'item_count', 'status', 'error_message', 'ai_usage', 'updated_at',
        ])
        for path in stored_paths:
            try:
                default_storage.delete(path)
            except Exception:  # storage hygiene is best-effort
                logger.debug('Could not delete intake source %s', path, exc_info=True)


def _extract_items(batch, usage):
    """Return ``(items, engine)`` for every source in the batch.

    ``usage`` is mutated with per-image model/token accounting so a batch
    that later fails still reports what the AI passes cost.
    """
    qr_items = []
    remote_seen = False
    for source in batch.sources:
        kind = source.get('kind')
        if kind == 'qr':
            parsed = parse_wholesale_qr(source.get('ref') or '')
            if parsed:
                qr_items.extend(parsed['items'])
        elif kind in ('image', 'url'):
            remote_seen = remote_seen or kind == 'url'
            payload = (
                _fetch_url(source['ref']) if kind == 'url'
                else _read_stored_image(source['ref'])
            )
            qr_text = decode_qr_from_image(payload)
            if qr_text:
                parsed = parse_wholesale_qr(qr_text)
                if parsed:
                    qr_items.extend(parsed['items'])

    if qr_items:
        return qr_items, 'qr'
    if not remote_seen and not any(s.get('kind') == 'image' for s in batch.sources):
        raise ValueError(
            'QR payload did not match the Twende Duka wholesale format and no '
            'invoice image was provided to fall back on.')

    hints = _shop_category_hints(batch.shop)
    items = []
    for source in batch.sources:
        kind = source.get('kind')
        if kind == 'image':
            payload = _read_stored_image(source['ref'])
            found, attempt_usage = extract_invoice_items(
                _data_url(payload, source.get('content_type')), category_hints=hints)
        elif kind == 'url':
            payload = _fetch_url(source['ref'])
            found, attempt_usage = extract_invoice_items(
                _data_url(payload, sniff_content_type(payload)), category_hints=hints)
        else:
            continue
        items.extend(_with_conservative_confidence(found))
        _accumulate_usage(usage, attempt_usage)
    return items, 'ai'


def _read_stored_image(path):
    """Read an uploaded source in chunks, enforcing the byte cap."""
    with default_storage.open(path, 'rb') as handle:
        return _read_stream(handle)


def _fetch_url(url):
    """Stream a remote media file in chunks; refuse anything over the cap."""
    try:
        with requests.get(url, stream=True, timeout=URL_FETCH_TIMEOUT) as response:
            response.raise_for_status()
            return _read_stream(response.iter_content(chunk_size=STREAM_CHUNK_BYTES))
    except (requests.RequestException, ValueError) as exc:
        raise AssistantError(f'Could not fetch {url}: {exc}') from exc


def _read_stream(stream):
    chunks = []
    total = 0
    for chunk in stream:
        total += len(chunk)
        if total > MAX_SOURCE_BYTES:
            raise AssistantError('Source media is larger than 8MB.')
        chunks.append(chunk)
    return b''.join(chunks)


_MAGIC = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
)


def sniff_content_type(payload):
    """Content type from magic bytes; fetched URLs carry no upload metadata."""
    for magic, content_type in _MAGIC:
        if payload.startswith(magic):
            return content_type
    if payload[:4] == b'RIFF' and payload[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


def _data_url(payload, content_type):
    return f'data:{content_type or sniff_content_type(payload)};base64,' + base64.b64encode(payload).decode('ascii')


# --------------------------------------------------------------------------
# Accuracy steering + cost accounting
# --------------------------------------------------------------------------

MAX_CATEGORY_HINTS = 30


def _shop_category_hints(shop):
    """Existing category names for the extraction prompt.

    Steering the model toward the merchant's own categories keeps the
    apply-time ``get_or_create`` match rate (and product grouping) clean
    instead of growing a new near-duplicate category per batch.
    """
    return list(
        Category.objects.filter(shop=shop).exclude(name='').order_by('name')
        .values_list('name', flat=True)[:MAX_CATEGORY_HINTS]
    )


def _with_conservative_confidence(items):
    """Cap self-reported confidence with deterministic sanity checks.

    The model never sees these rules, so a shaky row cannot talk itself into
    a confident one: nothing legible in either price column, an inverted
    margin, or a zero quantity caps the row no matter what it claimed.
    Confidence is only ever lowered here, never raised.
    """
    capped = []
    for item in items:
        confidence = item.get('confidence', 0.0)
        unit_cost, unit_price = item.get('unit_cost') or 0, item.get('unit_price') or 0
        if not unit_cost and not unit_price:
            confidence = min(confidence, 0.35)
        elif unit_cost and unit_price and unit_price < unit_cost:
            confidence = min(confidence, 0.6)
        if not item.get('quantity'):
            confidence = min(confidence, 0.7)
        capped.append({**item, 'confidence': confidence})
    return capped


def _accumulate_usage(total, attempt):
    """Merge one image's AI usage into the batch total."""
    for model in attempt.get('models_tried', ()):
        if model not in total.setdefault('models_tried', []):
            total['models_tried'].append(model)
    total['images'] = total.get('images', 0) + 1
    total['prompt_tokens'] = total.get('prompt_tokens', 0) + attempt.get('prompt_tokens', 0)
    total['completion_tokens'] = total.get('completion_tokens', 0) + attempt.get('completion_tokens', 0)


# --------------------------------------------------------------------------
# Duplicate grouping
# --------------------------------------------------------------------------

def _group_duplicates(items):
    """Merge rows with the same normalized name + unit (spec: duplicate grouping).

    Quantities add up; confidence keeps the *worst* value so a shaky read is
    never laundered into a confident row, and the first spelling of the name
    survives for the merchant to edit.
    """
    grouped = {}
    for item in items:
        name = ' '.join(str(item.get('name_en') or '').split())
        if not name:
            continue
        unit = (item.get('unit') or 'pcs').strip().lower() or 'pcs'
        key = (name.lower(), unit)
        try:
            quantity = float(item.get('quantity') or 0)
            confidence = float(item.get('confidence', 0.0))
        except (TypeError, ValueError):
            quantity, confidence = 0.0, 0.0
        if key in grouped:
            grouped[key]['quantity'] += quantity
            grouped[key]['confidence'] = min(grouped[key]['confidence'], confidence)
        else:
            grouped[key] = {
                'name_en': name,
                'name_sw': str(item.get('name_sw') or '').strip(),
                'unit': unit,
                'quantity': quantity,
                'unit_cost': item.get('unit_cost'),
                'unit_price': item.get('unit_price'),
                'category': str(item.get('category') or '').strip(),
                'confidence': max(0.0, min(1.0, confidence)),
            }
    return list(grouped.values())


# --------------------------------------------------------------------------
# Applying to the live tables
# --------------------------------------------------------------------------

def apply_intake_batch(batch, user):
    """Push COMPLETED drafts into Product + Inventory (spec Part 1, final step).

    Products match per shop by case-insensitive name: existing rows get their
    prices/tax refreshed, new rows are created with publishing flags off (the
    shop-level marketplace gate stays in charge). Stock enters through
    :func:`adjust_inventory`, so every row lands in the movement ledger.

    Returns ``{'created': n, 'updated': n, 'skipped': n}``. Raises
    ``ValidationError`` for batches not in the ``completed`` state or already
    applied — the view maps that to a 400/409.
    """
    with transaction.atomic():
        # Re-fetch under lock: callers may hold a stale instance (the worker
        # flips the status in the database, not on their copy).
        batch = IntakeBatch.objects.select_for_update().get(id=batch.id)
        if batch.status == 'applied':
            raise ValidationError('This batch has already been applied.')
        if batch.status != 'completed':
            raise ValidationError(f'Batch is {batch.status}; only completed batches can be applied.')

        branch = (
            Branch.objects.filter(shop=batch.shop, is_main=True).first()
            or Branch.objects.filter(shop=batch.shop).order_by('created_at').first()
        )
        if branch is None:
            raise ValidationError('This shop has no branch to stock; create one first.')

        created = updated = skipped = 0
        drafts = list(batch.drafts.select_related('applied_product'))
        for draft in drafts:
            name = draft.name_en.strip()
            if not name:
                skipped += 1
                continue
            category = None
            if draft.category_name.strip():
                category, _ = Category.objects.get_or_create(
                    shop=batch.shop, name__iexact=draft.category_name.strip()[:255],
                    defaults={'name': draft.category_name.strip()[:255]})
            product = Product.objects.filter(shop=batch.shop, name__iexact=name).first()
            if product is None:
                product = Product.objects.create(
                    shop=batch.shop,
                    branch=branch,
                    category=category,
                    name=name[:255],
                    unit=draft.unit or 'pcs',
                    buying_price=draft.buying_price,
                    selling_price=draft.selling_price,
                    tax_rate=draft.tax_rate_percent,
                )
                created += 1
            else:
                product.buying_price = draft.buying_price
                product.selling_price = draft.selling_price
                product.tax_rate = draft.tax_rate_percent
                if category and product.category_id is None:
                    product.category = category
                product.save(update_fields=[
                    'buying_price', 'selling_price', 'tax_rate', 'category', 'updated_at',
                ])
                updated += 1
            if draft.quantity > 0:
                adjust_inventory(
                    product, branch, 'in', draft.quantity, user=user,
                    reason=f'Intake batch {batch.id}',
                )
            draft.applied_product = product
        ProductDraft.objects.bulk_update(drafts, ['applied_product'])
        batch.status = 'applied'
        batch.save(update_fields=['status', 'updated_at'])
        return {'created': created, 'updated': updated, 'skipped': skipped}
