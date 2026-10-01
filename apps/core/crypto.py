"""Crypto-js compatible AES helpers for social tokens.

The Cloud Functions encrypt Facebook tokens with ``crypto.AES.encrypt(token,
key).toString()``. Both backends share Firestore rows during the migration, so
this port must read and write the exact same OpenSSL-style envelope:

- base64 of ``"Salted__"`` + 8-byte random salt + AES-256-CBC ciphertext,
- key/IV derived with EVP_BytesToKey (MD5, one iteration): key 32B, IV 16B,
- PKCS#7 padding.
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings

_SALTED_PREFIX = b'Salted__'
_SALT_LEN = 8
_KEY_LEN = 32
_IV_LEN = 16
_BLOCK_SIZE = 16
DEFAULT_KEY = 'biashara-connect-secret'


def get_passphrase() -> bytes:
    """The same value the JS functions used: ENCRYPTION_KEY or the default."""
    key = getattr(settings, 'SOCIAL_TOKEN_ENCRYPTION_KEY', '') or DEFAULT_KEY
    return key.encode('utf-8')


def _derive_key_iv(passphrase: bytes, salt: bytes) -> tuple:
    material = b''
    block = b''
    while len(material) < _KEY_LEN + _IV_LEN:
        block = hashlib.md5(block + passphrase + salt).digest()
        material += block
    return material[:_KEY_LEN], material[_KEY_LEN:_KEY_LEN + _IV_LEN]


def _pad(data: bytes) -> bytes:
    pad_len = _BLOCK_SIZE - len(data) % _BLOCK_SIZE
    return data + bytes([pad_len]) * pad_len


def _unpad(data: bytes) -> bytes:
    pad_len = data[-1] if data else 0
    if not 1 <= pad_len <= _BLOCK_SIZE or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError('Invalid PKCS#7 padding')
    return data[:-pad_len]


def _cipher(key: bytes, iv: bytes, encrypting: bool):
    primitive = Cipher(algorithms.AES(key), modes.CBC(iv))
    return primitive.encryptor() if encrypting else primitive.decryptor()


def encrypt_token(token):
    """Encrypt a token the way crypto-js did; falsy values pass through."""
    if not token:
        return token
    salt = os.urandom(_SALT_LEN)
    key, iv = _derive_key_iv(get_passphrase(), salt)
    encryptor = _cipher(key, iv, True)
    payload = encryptor.update(_pad(token.encode('utf-8'))) + encryptor.finalize()
    return base64.b64encode(_SALTED_PREFIX + salt + payload).decode('ascii')


def decrypt_token(cipher_text):
    """Mirror the JS ``decryptToken``: undecryptable input is returned as-is.

    Legacy rows may hold plaintext (the Cloud Function kept the user token
    unencrypted in its session doc), so callers rely on getting the original
    value back instead of an error.
    """
    if not cipher_text:
        return None
    try:
        raw = base64.b64decode(cipher_text, validate=True)
        if not raw.startswith(_SALTED_PREFIX):
            return cipher_text
        salt = raw[len(_SALTED_PREFIX):len(_SALTED_PREFIX) + _SALT_LEN]
        body = raw[len(_SALTED_PREFIX) + _SALT_LEN:]
        if len(salt) < _SALT_LEN or not body or len(body) % _BLOCK_SIZE:
            return cipher_text
        key, iv = _derive_key_iv(get_passphrase(), salt)
        decryptor = _cipher(key, iv, False)
        plain = _unpad(decryptor.update(body) + decryptor.finalize())
        return plain.decode('utf-8') or cipher_text
    except Exception:
        return cipher_text
