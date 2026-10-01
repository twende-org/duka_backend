from django.test import TestCase, override_settings

from apps.core.crypto import decrypt_token, encrypt_token

# Generated with node crypto-js, the library the Cloud Functions use:
#   crypto.AES.encrypt('EAAG-test-token-123', 'biashara-connect-secret').toString()
CRYPTO_JS_CIPHER = 'U2FsdGVkX1/y4rDrlOtTe3VQQgSBd6KiceyMlhscb8Jfdh/zM7tg9zfuCIJULJrl'


class CryptoJsInteropTests(TestCase):
    def test_decrypts_crypto_js_ciphertext(self):
        self.assertEqual(decrypt_token(CRYPTO_JS_CIPHER), 'EAAG-test-token-123')

    def test_encrypts_to_crypto_js_envelope(self):
        cipher = encrypt_token('EAAG-round-trip')
        # 11 chars is the longest fully stable prefix: char 12 already mixes in
        # the first random salt byte.
        self.assertTrue(cipher.startswith('U2FsdGVkX1'))
        self.assertEqual(decrypt_token(cipher), 'EAAG-round-trip')

    def test_undecryptable_input_passes_through(self):
        self.assertEqual(decrypt_token('EAAG-plain-token'), 'EAAG-plain-token')
        self.assertEqual(decrypt_token('not base64 at all!'), 'not base64 at all!')

    def test_falsy_values(self):
        self.assertIsNone(decrypt_token(None))
        self.assertEqual(encrypt_token(''), '')

    @override_settings(SOCIAL_TOKEN_ENCRYPTION_KEY='custom-secret')
    def test_passphrase_comes_from_settings(self):
        cipher = encrypt_token('EAAG-custom')
        self.assertEqual(decrypt_token(cipher), 'EAAG-custom')
        with override_settings(SOCIAL_TOKEN_ENCRYPTION_KEY='other-secret'):
            self.assertEqual(decrypt_token(cipher), cipher)
