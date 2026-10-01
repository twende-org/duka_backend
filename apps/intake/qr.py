"""Local, deterministic parser for the custom wholesale QR layout.

Wholesalers print QR codes whose payload is JSON like::

    {
      "type": "twendeduka.wholesale.v1",
      "invoice_no": "INV-2291",
      "supplier": "Kilima Wholesalers",
      "issued_at": "2026-09-30",
      "items": [
        {"name": "Sugar 1kg", "name_sw": "Sukari 1kg", "quantity": 24,
         "unit": "pcs", "unit_cost": 2500, "unit_price": 3000}
      ]
    }

A payload that carries the marker and parses cleanly lets the whole batch skip
the external vision model entirely (near-zero token cost, exact numbers).
"""
import json

MARKER = 'twendeduka.wholesale.v1'


def parse_wholesale_qr(payload):
    """Normalize one QR payload into item dicts, or return ``None``.

    Strict on purpose: anything that is not exactly our layout must fall
    through to the vision pipeline rather than produce half-guessed drafts.
    """
    if not payload or not isinstance(payload, str):
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get('type') != MARKER:
        return None
    raw_items = data.get('items')
    if not isinstance(raw_items, list) or not raw_items:
        return None

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get('name') or '').strip()
        if not name:
            continue
        quantity = _as_number(raw.get('quantity'), 0)
        items.append({
            'name_en': name,
            'name_sw': str(raw.get('name_sw') or '').strip(),
            'quantity': quantity,
            'unit': str(raw.get('unit') or 'pcs').strip() or 'pcs',
            'unit_cost': _as_number(raw.get('unit_cost'), 0),
            'unit_price': _as_number(raw.get('unit_price'), 0),
            'category': str(raw.get('category') or '').strip(),
            # Locally parsed payloads are exact — no review flag needed.
            'confidence': 1.0,
        })
    if not items:
        return None
    return {
        'invoice_no': str(data.get('invoice_no') or ''),
        'supplier': str(data.get('supplier') or ''),
        'items': items,
    }


def _as_number(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # filter NaN
