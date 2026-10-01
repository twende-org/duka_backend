"""Phone-number matching rules shared by the importer and the customer portal.

Mirrors the frontend ``getPhoneFormats`` (src/lib/firestore.ts) so a phone stored
in any of the common Tanzanian spellings — ``0712345678``, ``255712345678``,
``+255712345678`` — resolves to the same person on both backends.
"""
import re

_DIGITS = re.compile(r'\D')


def phone_formats(phone):
    """Every spelling of ``phone`` that must be treated as the same number.

    The frontend keys on the last nine digits; shorter inputs are returned
    unchanged (matching ``getPhoneFormats``' single-element fallback).
    """
    if not phone:
        return []
    clean = _DIGITS.sub('', str(phone))
    if len(clean) < 9:
        return [str(phone)]
    last9 = clean[-9:]
    return [last9, f'0{last9}', f'255{last9}', f'+255{last9}', f'+{last9}']
