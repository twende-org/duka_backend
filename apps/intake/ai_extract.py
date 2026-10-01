"""OpenRouter vision calls that turn an invoice photo into draft line items.

Mirrors ``apps.core.assistant`` (same transport, same ``AssistantError``
semantics) but demands a strict flat JSON schema so the model cannot spend
tokens on prose and the answer parses without cleanup.

Cost discipline: the cheap ``AI_INTAKE_MODEL`` runs first and its answer is
accepted outright whenever it is usable and confident. Only a weak first pass
(no rows, an unparsable answer, or best-row confidence below
``AI_INTAKE_MIN_CONFIDENCE``) pays for exactly one escalation to the strong
model with a larger token budget. Transient upstream failures (429/5xx,
timeouts) are retried once before the attempt counts as failed.
"""
import json
import logging
import time

import requests
from django.conf import settings

# pyrefly: ignore [missing-import]
from apps.core.assistant import AssistantError

logger = logging.getLogger(__name__)

OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
INVOICE_TIMEOUT = 60
#: One retry for transient upstream failures; the worker thread can afford
#: the pause and a retry is far cheaper than failing a whole batch.
INVOICE_RETRIES = 1
RETRY_BACKOFF_SECONDS = 1.5
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
#: An escalation usually means the first pass choked on a long or dense
#: invoice, so the strong model gets double the answer room.
MAX_TOKENS = 2000
MAX_TOKENS_STRONG = 4000
#: Shop category names injected into the prompt (accuracy: reusing the
#: merchant's own categories keeps the apply-time match rate high).
MAX_CATEGORY_HINTS = 30

INVOICE_PROMPT = (
    "Extract line items from this wholesale invoice, delivery note, or receipt "
    "photo from a Tanzanian shop. Return ONLY JSON per the schema. Rules: name = "
    "English product name, keep brand and pack size (e.g. 'Azam Soda 500ml'); "
    "name_sw = Swahili name (copy the English name when unknown); quantity = "
    "numeric count of units; unit = kg/g/litre/ml/pcs/carton/box/pack; unit_cost "
    "= wholesale price per unit in Tanzanian shillings (0 if not printed); "
    "unit_price = retail price per unit in Tanzanian shillings (0 if not "
    "printed); category = short lowercase category; confidence = your 0.0-1.0 "
    "certainty for that row, high only when the name AND a price are clearly "
    "legible. Omit rows you cannot read. Ignore totals, tax lines and payment "
    "terms. No prose."
)

CATEGORY_HINT_PROMPT = (
    "Known categories for this shop: {}. Reuse one of them verbatim when a row "
    "fits; otherwise invent a short lowercase category."
)

ITEM_JSON_SCHEMA = {
    'type': 'json_schema',
    'json_schema': {
        'name': 'invoice_items',
        'strict': True,
        'schema': {
            'type': 'object',
            'properties': {
                'items': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'name_sw': {'type': 'string'},
                            'quantity': {'type': 'number'},
                            'unit': {'type': 'string'},
                            'unit_cost': {'type': 'number'},
                            'unit_price': {'type': 'number'},
                            'category': {'type': 'string'},
                            'confidence': {'type': 'number'},
                        },
                        'required': [
                            'name', 'name_sw', 'quantity', 'unit', 'unit_cost',
                            'unit_price', 'category', 'confidence',
                        ],
                        'additionalProperties': False,
                    },
                },
            },
            'required': ['items'],
            'additionalProperties': False,
        },
    },
}


def _post(payload, timeout):
    key = getattr(settings, 'OPENROUTER_API_KEY', '')
    if not key:
        raise AssistantError('OPENROUTER_API_KEY is not configured on the server.', status_code=503)
    last_failure = ''
    for attempt in range(INVOICE_RETRIES + 1):
        try:
            response = requests.post(
                OPENROUTER_URL,
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                json=payload,
                timeout=timeout,
            )
            if response.status_code in RETRYABLE_STATUSES and attempt < INVOICE_RETRIES:
                last_failure = f'HTTP {response.status_code}'
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            response.raise_for_status()
            data = response.json()
        except requests.HTTPError:
            # A non-retryable status (429/5xx were handled above): a bad key
            # or refused request will not improve on a second attempt.
            raise AssistantError(
                f'OpenRouter request failed: HTTP {response.status_code}',
                status_code=response.status_code,
            ) from None
        except (requests.RequestException, ValueError) as exc:
            if attempt < INVOICE_RETRIES:
                last_failure = str(exc)
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise AssistantError(f'OpenRouter request failed: {exc}') from exc
        if not isinstance(data, dict):
            raise AssistantError('OpenRouter answered an unexpected payload.')
        return data
    raise AssistantError(f'OpenRouter request failed: {last_failure}')


def _first_content(data):
    choices = data.get('choices')
    if choices and isinstance(choices[0], dict):
        message = choices[0].get('message')
        if isinstance(message, dict) and message.get('content'):
            return message['content']
    return ''


def _strip_json_fences(content):
    if content.startswith('```'):
        content = content.replace('```json', '').replace('```', '')
    return content.strip()


def _usage(data, model):
    raw = data.get('usage')
    raw = raw if isinstance(raw, dict) else {}
    return {
        'model': model,
        'prompt_tokens': int(raw.get('prompt_tokens') or 0),
        'completion_tokens': int(raw.get('completion_tokens') or 0),
    }


def _call_invoice_model(model, image_data_url, category_hints, max_tokens):
    """One extraction attempt; returns ``(items, usage)`` or raises."""
    prompt = INVOICE_PROMPT
    hints = [str(hint or '').strip() for hint in (category_hints or ())]
    hints = [hint for hint in hints if hint][:MAX_CATEGORY_HINTS]
    if hints:
        prompt = f'{prompt} {CATEGORY_HINT_PROMPT.format(", ".join(hints))}'

    data = _post({
        'model': model,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': image_data_url}},
            ],
        }],
        'response_format': ITEM_JSON_SCHEMA,
        'temperature': 0,
        'max_tokens': max_tokens,
    }, timeout=INVOICE_TIMEOUT)

    content = _strip_json_fences(_first_content(data))
    if not content:
        raise AssistantError('Vision model returned an empty answer.')
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise AssistantError(f'Vision model returned unparsable JSON: {exc}') from exc
    raw_items = parsed.get('items') if isinstance(parsed, dict) else None
    if not isinstance(raw_items, list):
        raise AssistantError('Vision model answer had no items array.')

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get('name') or '').strip()
        if not name:
            continue
        items.append({
            'name_en': name,
            'name_sw': str(raw.get('name_sw') or '').strip(),
            'quantity': _as_number(raw.get('quantity'), 0),
            'unit': str(raw.get('unit') or 'pcs').strip() or 'pcs',
            'unit_cost': _as_number(raw.get('unit_cost'), 0),
            'unit_price': _as_number(raw.get('unit_price'), 0),
            'category': str(raw.get('category') or '').strip(),
            'confidence': min(1.0, max(0.0, _as_number(raw.get('confidence'), 0.0))),
        })
    return items, _usage(data, model)


def _row_confidence(items):
    return max((item['confidence'] for item in items), default=0.0)


def _is_usable(items):
    """A first pass good enough to skip the strong model entirely."""
    return bool(items) and _row_confidence(items) >= float(
        getattr(settings, 'AI_INTAKE_MIN_CONFIDENCE', 0.6))


#: Errors that are about the key/request, not the model: escalating would
#: just pay for a second identical failure. 503 = no key configured.
FATAL_STATUSES = frozenset({401, 402, 403, 503})


def extract_invoice_items(image_data_url, category_hints=()):
    """One data-URL image in, ``(items, usage)`` out.

    The cheap model answers first; a weak answer (failed, empty, unparsable,
    or best confidence under ``AI_INTAKE_MIN_CONFIDENCE``) triggers exactly
    one escalation to the strong model with double the token budget. Between
    the two, more rows wins and a tie goes to higher best-row confidence —
    with the cheap answer kept on a full tie. ``usage`` reports the winning
    model plus the tokens of *every* attempt, so the batch shows true cost.
    Raises :class:`AssistantError` when the failure is about the key or request
    itself (:data:`FATAL_STATUSES` — both tiers would fail identically, so no
    escalation is paid for) or every attempt failed.
    """
    primary_model = getattr(settings, 'AI_INTAKE_MODEL', 'google/gemini-2.5-flash')
    strong_model = getattr(settings, 'AI_INTAKE_MODEL_STRONG', 'google/gemini-2.5-pro')
    usage = {'model': '', 'models_tried': [], 'prompt_tokens': 0, 'completion_tokens': 0}
    fallback = None  # (items, model) from a usable-but-weak primary answer
    first_error = None

    def _record(attempt_usage):
        usage['models_tried'].append(attempt_usage['model'])
        usage['prompt_tokens'] += attempt_usage['prompt_tokens']
        usage['completion_tokens'] += attempt_usage['completion_tokens']

    try:
        items, attempt_usage = _call_invoice_model(
            primary_model, image_data_url, category_hints, MAX_TOKENS)
    except AssistantError as exc:
        if exc.status_code in FATAL_STATUSES:
            raise
        first_error = exc
        logger.info('Intake model %s failed: %s', primary_model, exc)
    else:
        _record(attempt_usage)
        if _is_usable(items):
            usage['model'] = primary_model
            return items, usage
        fallback = (items, primary_model)

    try:
        items, attempt_usage = _call_invoice_model(
            strong_model, image_data_url, category_hints, MAX_TOKENS_STRONG)
    except AssistantError as exc:
        if fallback is not None:
            logger.info('Intake escalation to %s failed (%s); keeping %s answer.',
                        strong_model, exc, fallback[1])
            usage['model'] = fallback[1]
            return fallback[0], usage
        raise (first_error or exc) from exc
    _record(attempt_usage)

    if fallback is not None:
        primary_items = fallback[0]
        if len(primary_items) > len(items) or (
            len(primary_items) == len(items)
            and _row_confidence(primary_items) >= _row_confidence(items)
        ):
            usage['model'] = primary_model
            return primary_items, usage
    usage['model'] = strong_model
    return items, usage


def _as_number(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # filter NaN
