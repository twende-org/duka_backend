from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.core import assistant
# pyrefly: ignore [missing-import]
from apps.users.models import User


class BuildPromptTests(SimpleTestCase):
    def test_prompt_carries_shop_name_and_context(self):
        prompt = assistant.build_assistant_prompt(
            'Duka Jema', {'todaySales': 1500, 'userRole': 'owner'},
        )
        self.assertIn('for a shop named "Duka Jema"', prompt)
        self.assertIn('[NAVIGATE:/exact-path]', prompt)
        self.assertIn('"todaySales": 1500', prompt)
        self.assertIn('"userRole": "owner"', prompt)

    def test_context_renders_like_json_stringify(self):
        prompt = assistant.build_assistant_prompt('S', {'name': 'Mkate', 'qty': 2})
        self.assertIn('{\n  "name": "Mkate",\n  "qty": 2\n}', prompt)


class AskAssistantTests(SimpleTestCase):
    def _response(self, payload):
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = payload
        return response

    @override_settings(OPENROUTER_API_KEY='')
    def test_missing_key_is_503(self):
        with self.assertRaises(assistant.AssistantError) as ctx:
            assistant.ask_assistant('S', 'Habari', {})
        self.assertEqual(ctx.exception.status_code, 503)

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_successful_reply_is_returned(self):
        payload = {'choices': [{'message': {'content': 'Mauzo yako ni mazuri leo.'}}]}
        with mock.patch.object(assistant.requests, 'post', return_value=self._response(payload)) as post:
            reply = assistant.ask_assistant('Duka', 'Habari', {'todaySales': 10})

        self.assertEqual(reply, 'Mauzo yako ni mazuri leo.')
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-key')
        self.assertEqual(kwargs['json']['model'], assistant.CHAT_MODEL)
        self.assertEqual(kwargs['json']['max_tokens'], assistant.MAX_TOKENS)
        self.assertEqual(kwargs['json']['temperature'], 0.7)
        self.assertEqual(kwargs['json']['messages'][1], {'role': 'user', 'content': 'Habari'})
        self.assertIn('Duka', kwargs['json']['messages'][0]['content'])

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_empty_answer_falls_back_to_the_legacy_line(self):
        payload = {'choices': [{'message': {'content': ''}}]}
        with mock.patch.object(assistant.requests, 'post', return_value=self._response(payload)):
            reply = assistant.ask_assistant('S', 'Habari', {})
        self.assertEqual(reply, 'Samahani, sijaelewa. Tafadhali rudia.')

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_http_error_is_502(self):
        response = mock.Mock(ok=False, status_code=500)
        response.raise_for_status.side_effect = assistant.requests.HTTPError('boom')
        with mock.patch.object(assistant.requests, 'post', return_value=response):
            with self.assertRaises(assistant.AssistantError) as ctx:
                assistant.ask_assistant('S', 'Habari', {})
        self.assertEqual(ctx.exception.status_code, 502)

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_malformed_choices_fall_back_like_the_browser(self):
        # The legacy helper read ``data.choices?.[0]?.message?.content || ...``,
        # so a non-list ``choices`` also produced the generic line.
        with mock.patch.object(assistant.requests, 'post', return_value=self._response({'choices': 'nope'})):
            reply = assistant.ask_assistant('S', 'Habari', {})
        self.assertEqual(reply, 'Samahani, sijaelewa. Tafadhali rudia.')

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_non_object_body_is_502(self):
        with mock.patch.object(assistant.requests, 'post', return_value=self._response(['nope'])):
            with self.assertRaises(assistant.AssistantError) as ctx:
                assistant.ask_assistant('S', 'Habari', {})
        self.assertEqual(ctx.exception.status_code, 502)


class ExtractProductDetailsTests(SimpleTestCase):
    def _response(self, content):
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': content}}]}
        return response

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_plain_json_answer_is_parsed(self):
        content = '{"name": "Sabuni", "unit": "pcs"}'
        with mock.patch.object(assistant.requests, 'post', return_value=self._response(content)) as post:
            details = assistant.extract_product_details('data:image/jpeg;base64,AAA')

        self.assertEqual(details, {'name': 'Sabuni', 'unit': 'pcs'})
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs['json']['model'], assistant.VISION_MODEL)
        self.assertEqual(kwargs['json']['temperature'], 0)
        blocks = kwargs['json']['messages'][0]['content']
        self.assertEqual(blocks[1], {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,AAA'}})

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_markdown_fences_are_stripped(self):
        content = '```json\n{"brand": "Azam", "barcode": "123"}\n```'
        with mock.patch.object(assistant.requests, 'post', return_value=self._response(content)):
            details = assistant.extract_product_details('data:image/jpeg;base64,AAA')
        self.assertEqual(details, {'brand': 'Azam', 'barcode': '123'})

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_unknown_fields_are_kept_as_extras(self):
        content = '{"name": "Chai", "price": 500, "brand": "  ", "unit": null}'
        with mock.patch.object(assistant.requests, 'post', return_value=self._response(content)):
            details = assistant.extract_product_details('data:image/jpeg;base64,AAA')
        self.assertEqual(details, {'name': 'Chai', 'extra': {'price': '500'}})

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_extended_and_numeric_fields_survive(self):
        content = (
            '{"name": "Azam Soda", "category": "Beverages", "size": "500ml",'
            ' "weight": "500g", "color": "Blue", "expiryDate": "2027-01-31",'
            ' "buyingPrice": "3,500", "sellingPrice": 4000, "quantity": "12",'
            ' "profitable": true, "junk": {"nested": 1}}'
        )
        with mock.patch.object(assistant.requests, 'post', return_value=self._response(content)):
            details = assistant.extract_product_details('data:image/jpeg;base64,AAA')
        self.assertEqual(details, {
            'name': 'Azam Soda',
            'category': 'Beverages',
            'size': '500ml',
            'weight': '500g',
            'color': 'Blue',
            'expiryDate': '2027-01-31',
            'buyingPrice': 3500.0,
            'sellingPrice': 4000.0,
            'quantity': 12.0,
        })

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_unparsable_answer_is_502(self):
        with mock.patch.object(assistant.requests, 'post', return_value=self._response('not json at all')):
            with self.assertRaises(assistant.AssistantError) as ctx:
                assistant.extract_product_details('data:image/jpeg;base64,AAA')
        self.assertEqual(ctx.exception.status_code, 502)

    @override_settings(OPENROUTER_API_KEY='')
    def test_missing_key_is_503(self):
        with self.assertRaises(assistant.AssistantError) as ctx:
            assistant.extract_product_details('data:image/jpeg;base64,AAA')
        self.assertEqual(ctx.exception.status_code, 503)


class ExtractProductListTests(SimpleTestCase):
    def _response(self, content):
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': content}}]}
        return response

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_products_array_is_parsed_and_cleaned(self):
        content = (
            '{"products": ['
            '{"name": "Sabuni", "price": 5, "brand": "  "},'
            '{"brand": "NoName"},'
            '{"name": " Sukari ", "unit": "kg", "buyingPrice": "1,200", "sellingPrice": 1500}'
            ']}'
        )
        with mock.patch.object(assistant.requests, 'post', return_value=self._response(content)) as post:
            products = assistant.extract_product_list('data:image/jpeg;base64,AAA')

        self.assertEqual(products, [
            {'name': 'Sabuni', 'extra': {'price': '5'}},
            {
                'name': 'Sukari',
                'unit': 'kg',
                'buyingPrice': 1200.0,
                'sellingPrice': 1500.0,
            },
        ])
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs['json']['model'], assistant.VISION_MODEL)
        self.assertEqual(kwargs['json']['temperature'], 0)
        blocks = kwargs['json']['messages'][0]['content']
        self.assertEqual(blocks[1], {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,AAA'}})

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_list_answers_keep_extras_and_extended_fields(self):
        content = (
            '{"products": [{"name": "Chai", "flavour": "Vanilla", "size": "250g",'
            ' "quantity": "6", "memo": null}]}'
        )
        with mock.patch.object(assistant.requests, 'post', return_value=self._response(content)):
            products = assistant.extract_product_list('data:image/jpeg;base64,AAA')
        self.assertEqual(products, [{
            'name': 'Chai',
            'size': '250g',
            'quantity': 6.0,
            'extra': {'flavour': 'Vanilla'},
        }])

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_bare_array_answer_is_tolerated(self):
        with mock.patch.object(
            assistant.requests, 'post',
            return_value=self._response('[{"name": "Unga"}]'),
        ):
            products = assistant.extract_product_list('data:image/jpeg;base64,AAA')
        self.assertEqual(products, [{'name': 'Unga'}])

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_no_products_yields_an_empty_list(self):
        with mock.patch.object(
            assistant.requests, 'post',
            return_value=self._response('{"products": []}'),
        ):
            products = assistant.extract_product_list('data:image/jpeg;base64,AAA')
        self.assertEqual(products, [])

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_list_is_capped(self):
        entries = ','.join(f'{{"name": "P{i}"}}' for i in range(assistant.MAX_PRODUCTS_PER_PHOTO + 5))
        with mock.patch.object(
            assistant.requests, 'post',
            return_value=self._response('{"products": [' + entries + ']}'),
        ):
            products = assistant.extract_product_list('data:image/jpeg;base64,AAA')
        self.assertEqual(len(products), assistant.MAX_PRODUCTS_PER_PHOTO)
        self.assertEqual(products[0], {'name': 'P0'})

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_unparsable_answer_is_502(self):
        with mock.patch.object(assistant.requests, 'post', return_value=self._response('not json at all')):
            with self.assertRaises(assistant.AssistantError) as ctx:
                assistant.extract_product_list('data:image/jpeg;base64,AAA')
        self.assertEqual(ctx.exception.status_code, 502)

    @override_settings(OPENROUTER_API_KEY='')
    def test_missing_key_is_503(self):
        with self.assertRaises(assistant.AssistantError) as ctx:
            assistant.extract_product_list('data:image/jpeg;base64,AAA')
        self.assertEqual(ctx.exception.status_code, 503)


class AIViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', email='o@test.com', password='pw')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.assistant_url = reverse('ai-assistant')
        self.extract_url = reverse('ai-extract-product')
        self.extract_many_url = reverse('ai-extract-products')

    def test_anonymous_is_rejected(self):
        self.assertEqual(
            APIClient().post(self.assistant_url, {'message': 'hi'}, format='json').status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_message_is_required(self):
        response = self.client.post(self.assistant_url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_context_must_be_an_object(self):
        response = self.client.post(
            self.assistant_url, {'message': 'hi', 'context': 'nope'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_assistant_answers_with_camel_case_body(self):
        payload = {'choices': [{'message': {'content': 'Pongezi! Mauzo yamepanda.'}}]}
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = payload
        with mock.patch.object(assistant.requests, 'post', return_value=response) as post:
            answer = self.client.post(
                self.assistant_url,
                {'message': 'Habari', 'shopName': 'Duka Jema', 'context': {'todaySales': 5}},
                format='json',
            )

        self.assertEqual(answer.status_code, status.HTTP_200_OK)
        self.assertEqual(answer.json(), {'reply': 'Pongezi! Mauzo yamepanda.'})
        self.assertIn('Duka Jema', post.call_args.kwargs['json']['messages'][0]['content'])

    @override_settings(OPENROUTER_API_KEY='')
    def test_missing_key_answers_503(self):
        response = self.client.post(self.assistant_url, {'message': 'Habari'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_image_is_required(self):
        response = self.client.post(self.extract_url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_extraction_answers_with_details(self):
        payload = {'choices': [{'message': {'content': '{"name": "Sabuni", "barcode": "12345"}'}}]}
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = payload
        with mock.patch.object(assistant.requests, 'post', return_value=response):
            answer = self.client.post(
                self.extract_url, {'image': 'data:image/jpeg;base64,AAA'}, format='json',
            )

        self.assertEqual(answer.status_code, status.HTTP_200_OK)
        self.assertEqual(answer.json(), {'details': {'name': 'Sabuni', 'barcode': '12345'}})

    def test_anonymous_list_extraction_is_rejected(self):
        self.assertEqual(
            APIClient().post(self.extract_many_url, {'image': 'x'}, format='json').status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_image_is_required_for_list_extraction(self):
        response = self.client.post(self.extract_many_url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(OPENROUTER_API_KEY='test-key')
    def test_list_extraction_answers_with_details(self):
        payload = {'choices': [{'message': {'content': '{"products": [{"name": "Sabuni"}, {"name": "Sukari"}]}'}}]}
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = payload
        with mock.patch.object(assistant.requests, 'post', return_value=response):
            answer = self.client.post(
                self.extract_many_url, {'image': 'data:image/jpeg;base64,AAA'}, format='json',
            )

        self.assertEqual(answer.status_code, status.HTTP_200_OK)
        self.assertEqual(answer.json(), {'details': [{'name': 'Sabuni'}, {'name': 'Sukari'}]})
