import os
from collections.abc import Callable

import pytest
from jobhunter.crypto import DecryptError, decrypt, encrypt, hkdf_sha256, key_id


def test_roundtrip_key_id_and_rotation() -> None:
    old, new = os.urandom(32), os.urandom(32)
    blob = encrypt(old, b"JBSWY3DPEHPK3PXP", aad=b"totp")
    assert (
        blob[0] == 1
        and blob[1:9] == key_id(old)
        and decrypt([new, old], blob, aad=b"totp") == b"JBSWY3DPEHPK3PXP"
    )
    bad_calls: list[Callable[[], bytes]] = [
        lambda: decrypt([old], blob, aad=b"other"),
        lambda: decrypt([old], blob[:-1] + bytes([blob[-1] ^ 1]), aad=b"totp"),
        lambda: decrypt([new], blob, aad=b"totp"),
    ]
    for bad in bad_calls:
        with pytest.raises(DecryptError):
            bad()


def test_hkdf_is_deterministic_and_separated() -> None:
    root = b"r" * 32
    assert (
        hkdf_sha256(root, b"session") == hkdf_sha256(root, b"session") != hkdf_sha256(root, b"login-username")
    )


def test_key_id_is_the_first_eight_bytes_of_the_sha256() -> None:
    import hashlib

    key = os.urandom(32)
    assert key_id(key) == hashlib.sha256(key).digest()[:8] and len(key_id(key)) == 8


def test_blob_layout_and_a_fresh_nonce_per_call() -> None:
    key = os.urandom(32)
    first, second = encrypt(key, b"same", aad=b"a"), encrypt(key, b"same", aad=b"a")
    # version(1) + key id(8) + nonce(12) + ciphertext (4 bytes) + tag(16)
    assert len(first) == 1 + 8 + 12 + 4 + 16
    assert first[9:21] != second[9:21] and first != second


def test_encrypt_rejects_keys_that_are_not_32_bytes() -> None:
    for short in (b"", b"k" * 16, b"k" * 24, b"k" * 31, b"k" * 33):
        with pytest.raises(ValueError, match="32 bytes"):
            encrypt(short, b"x", aad=b"a")


def test_decrypt_rejects_malformed_blobs_without_leaking_key_material() -> None:
    key = os.urandom(32)
    blob = encrypt(key, b"secret value", aad=b"a")
    unknown_version = b"\x02" + blob[1:]
    unknown_key_id = blob[:1] + bytes(8) + blob[9:]
    # Every cut that leaves less than version + key id + nonce + tag is truncated; so is the empty blob.
    truncated = [blob[:n] for n in (0, 1, 8, 9, 20, 21, 36)]
    for bad in (unknown_version, unknown_key_id, *truncated):
        with pytest.raises(DecryptError) as caught:
            decrypt([key], bad, aad=b"a")
        text = str(caught.value) + repr(caught.value.args)
        assert key.hex() not in text and key_id(key).hex() not in text and b"secret value".hex() not in text
    # A blob cut inside the ciphertext fails authentication, it is not accepted.
    with pytest.raises(DecryptError):
        decrypt([key], blob[:-1], aad=b"a")


def test_decrypt_with_no_keys_fails_and_a_malformed_key_is_a_bug() -> None:
    key = os.urandom(32)
    blob = encrypt(key, b"x", aad=b"a")
    with pytest.raises(DecryptError):
        decrypt([], blob, aad=b"a")
    with pytest.raises(ValueError, match="32 bytes"):
        decrypt([key[:16]], blob, aad=b"a")
