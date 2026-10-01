"""Cost/accuracy layer of apps.intake.ai_extract: escalation + transport.

The OpenRouter socket is mocked at ``_post`` (escalation tests) and at
``requests.post`` (retry tests) so the model choice logic itself is what is
under test, not the network.
"""
import json
import unittest.mock as mock

import requests
from django.test import TestCase, override_settings

from apps.core.assistant import AssistantError
from apps.intake.ai_extract import extract_invoice_items

CHEAP = 'test/cheap'
STRONG = 'test/strong'


def _answer(items, usage=None):
    """A parsed OpenRouter completion body."""
    return {
        'choices': [{'message': {'content': json.dumps({'items': items})}}],
        'usage': usage or {'prompt_tokens': 10, 'completion_tokens': 5},
    }


def _row(name, confidence, **overrides):
    item = {
        'name': name, 'name_sw': name, 'quantity': 1, 'unit': 'pcs',
        'unit_cost': 100, 'unit_price': 150, 'category': 'general',
        'confidence': confidence,
    }
    item.update(overrides)
    return item


@override_settings(
    AI_INTAKE_MODEL=CHEAP,
    AI_INTAKE_MODEL_STRONG=STRONG,
    AI_INTAKE_MIN_CONFIDENCE=0.6,
)
class EscalationTest(TestCase):
    def _extract(self, post_side_effect, **kwargs):
        with mock.patch('apps.intake.ai_extract._post',
                        side_effect=post_side_effect) as post:
            result = extract_invoice_items('data:image/jpeg;base64,x', **kwargs)
        payloads = [call.args[0] for call in post.call_args_list]
        return result, payloads

    def test_confident_primary_answer_skips_strong_model(self):
        (items, usage), payloads = self._extract([
            _answer([_row('Sabuni', 0.9)]),
        ])
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['model'], CHEAP)
        self.assertEqual(items[0]['name_en'], 'Sabuni')
        self.assertEqual(usage, {
            'model': CHEAP, 'models_tried': [CHEAP],
            'prompt_tokens': 10, 'completion_tokens': 5,
        })

    def test_weak_primary_escalates_and_strong_wins(self):
        (items, usage), payloads = self._extract([
            _answer([_row('A', 0.3), _row('B', 0.2)]),
            _answer([_row('A', 0.8), _row('B', 0.7), _row('C', 0.9)]),
        ])
        self.assertEqual([payload['model'] for payload in payloads], [CHEAP, STRONG])
        # The strong model gets a larger answer budget.
        self.assertEqual(payloads[1]['max_tokens'], 4000)
        self.assertEqual([item['name_en'] for item in items], ['A', 'B', 'C'])
        self.assertEqual(usage['model'], STRONG)
        self.assertEqual(usage['models_tried'], [CHEAP, STRONG])
        self.assertEqual(usage['prompt_tokens'], 20)
        self.assertEqual(usage['completion_tokens'], 10)

    def test_escalation_with_fewer_rows_keeps_cheap_answer(self):
        (items, usage), payloads = self._extract([
            _answer([_row('A', 0.3), _row('B', 0.2)]),
            _answer([_row('A', 0.99)]),
        ])
        self.assertEqual(len(payloads), 2)
        self.assertEqual([item['name_en'] for item in items], ['A', 'B'])
        self.assertEqual(usage['model'], CHEAP)

    def test_escalation_tie_on_rows_prefers_higher_confidence(self):
        (items, usage), _ = self._extract([
            _answer([_row('A', 0.3), _row('B', 0.2)]),
            _answer([_row('A', 0.85), _row('B', 0.8)]),
        ])
        self.assertEqual(usage['model'], STRONG)
        self.assertEqual(items[0]['confidence'], 0.85)

    def test_escalation_tie_on_rows_and_confidence_keeps_cheap_answer(self):
        (_, usage), _ = self._extract([
            _answer([_row('A', 0.3), _row('B', 0.2)]),
            _answer([_row('A', 0.3), _row('B', 0.2)]),
        ])
        self.assertEqual(usage['model'], CHEAP)

    def test_primary_transport_error_escalates(self):
        (items, usage), payloads = self._extract([
            AssistantError('HTTP 502', 502),
            _answer([_row('A', 0.9)]),
        ])
        self.assertEqual(len(payloads), 2)
        self.assertEqual(items[0]['name_en'], 'A')
        self.assertEqual(usage['model'], STRONG)

    def test_both_attempts_failed_reraises_first_error(self):
        with self.assertRaisesRegex(AssistantError, 'first failure'):
            self._extract([
                AssistantError('first failure', 502),
                AssistantError('second failure', 502),
            ])

    def test_missing_key_raises_without_escalating(self):
        with mock.patch('apps.intake.ai_extract._post',
                        side_effect=[AssistantError('no key', 503)]) as post:
            with self.assertRaises(AssistantError):
                extract_invoice_items('data:image/jpeg;base64,x')
        self.assertEqual(post.call_count, 1)

    def test_primary_failed_but_escalation_also_failed_keeps_weak_answer(self):
        (items, usage), _ = self._extract([
            _answer([_row('A', 0.3)]),
            AssistantError('strong down', 502),
        ])
        self.assertEqual(items[0]['name_en'], 'A')
        self.assertEqual(usage['model'], CHEAP)

    def test_category_hints_reach_the_prompt(self):
        (_, _), payloads = self._extract(
            [_answer([_row('Soda', 0.9)])],
            category_hints=['mvinyo', 'vinywaji', ''],
        )
        text = payloads[0]['messages'][0]['content'][0]['text']
        self.assertIn('mvinyo', text)
        self.assertIn('vinywaji', text)

    def test_missing_usage_reports_zero_tokens(self):
        answer = _answer([_row('A', 0.9)])
        answer['usage'] = {}
        (_, usage), _ = self._extract([answer])
        self.assertEqual(usage['prompt_tokens'], 0)
        self.assertEqual(usage['completion_tokens'], 0)


# Hermetic: the model names and the key are pinned here because the key check
# runs before requests.post, so without a key the mock is never reached and the
# retry assertions silently pass on the "not configured" error path instead.
@override_settings(
    AI_INTAKE_MODEL=CHEAP,
    AI_INTAKE_MODEL_STRONG=STRONG,
    AI_INTAKE_MIN_CONFIDENCE=0.6,
    OPENROUTER_API_KEY='test-key',
)
class TransportRetryTest(TestCase):
    class _FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f'HTTP {self.status_code}')

        def json(self):
            return self._payload

    @override_settings(AI_INTAKE_MIN_CONFIDENCE=0.6)
    def test_retryable_status_is_retried_once(self):
        good = _answer([_row('A', 0.9)])
        with mock.patch('apps.intake.ai_extract.requests.post', side_effect=[
            self._FakeResponse(500),
            self._FakeResponse(200, good),
        ]) as post, mock.patch('apps.intake.ai_extract.time.sleep'):
            items, usage = extract_invoice_items('data:image/jpeg;base64,x')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(items[0]['name_en'], 'A')
        self.assertEqual(usage['models_tried'], [CHEAP])

    @override_settings(AI_INTAKE_MIN_CONFIDENCE=0.6)
    def test_client_error_is_not_retried(self):
        with mock.patch('apps.intake.ai_extract.requests.post', side_effect=[
            self._FakeResponse(401),
        ]) as post, mock.patch('apps.intake.ai_extract.time.sleep'):
            with self.assertRaises(AssistantError):
                extract_invoice_items('data:image/jpeg;base64,x')
        self.assertEqual(post.call_count, 1)
