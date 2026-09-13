import os

import pytest

from runtime.personal_chat.image_crypto import encrypt_image


@pytest.mark.skipif(os.name != 'nt', reason='Windows CNG runtime')
def test_aes_known_vector_and_full_block_padding():
    # FIPS 197 AES-128 example plus an independently computed PKCS7 block.
    key = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
    raw = bytes.fromhex('00112233445566778899aabbccddeeff')
    encrypted = encrypt_image(raw, key)
    assert encrypted[:16].hex() == '69c4e0d86a7b0430d8cdb78070b4c55a'
    assert len(encrypted) == 32
    assert encrypted[16:] == encrypt_image(b'', key)


@pytest.mark.skipif(os.name != 'nt', reason='Windows CNG runtime')
@pytest.mark.parametrize('length', [0, 1, 15, 16, 17, 1024])
def test_matches_independent_crypto_library(length):
    crypto = pytest.importorskip('cryptography.hazmat.primitives.ciphers')
    raw, key = bytes(i % 256 for i in range(length)), bytes(range(16))
    padding = 16 - length % 16
    reference = crypto.Cipher(crypto.algorithms.AES(key), crypto.modes.ECB()).encryptor()
    expected = reference.update(raw + bytes([padding]) * padding) + reference.finalize()
    assert encrypt_image(raw, key) == expected


def test_rejects_invalid_key():
    with pytest.raises(ValueError, match='WECHAT_IMAGE_KEY_INVALID'):
        encrypt_image(b'png', b'short')
