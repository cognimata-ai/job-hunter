"""Key derivation and authenticated encryption at rest (spec §16.2, §16.9).

`encrypt` seals a value with AES-256-GCM under the data key and returns a self-describing blob:

    b"\\x01" ‖ key_id (8 bytes) ‖ nonce (12 bytes) ‖ ciphertext ‖ tag (16 bytes)

The first byte is the format version. `key_id` is the first 8 bytes of `sha256(key)`: it names the key that
sealed the blob without revealing it, so `decrypt` can pick the right one of several keys during a rotation
(`data_key` and `data_key_previous`). The associated data binds a blob to its purpose (`b"totp"` for the TOTP
secret): a blob moved to another column does not open.

Failures never carry key material, plaintext or a key id: `DecryptError` has one of four fixed messages.

`data_keys(cfg)` lists the keys of a configuration: the data key, which seals, and during a rotation the
previous one, which still opens. It lives here rather than in the api so that the worker reads the keys
without importing FastAPI.
"""

import hashlib
import os
from collections.abc import Sequence
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from jobhunter.config import AppConfig

FORMAT_VERSION: Final = 1
KEY_BYTES: Final = 32
KEY_ID_BYTES: Final = 8
NONCE_BYTES: Final = 12
TAG_BYTES: Final = 16
_NONCE_START: Final = 1 + KEY_ID_BYTES
_CIPHERTEXT_START: Final = _NONCE_START + NONCE_BYTES
_MIN_BLOB_BYTES: Final = _CIPHERTEXT_START + TAG_BYTES


class DecryptError(Exception):
    """A blob could not be opened: unknown version, unknown key, truncated, or failed authentication."""


def hkdf_sha256(root: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 (RFC 5869, no salt) of `root` for the purpose `info`; other `info`, an independent key."""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(root)


def key_id(key: bytes) -> bytes:
    """The first 8 bytes of `sha256(key)`."""
    return hashlib.sha256(key).digest()[:KEY_ID_BYTES]


def _require_aes256_key(key: bytes) -> None:
    # AESGCM would also take 16- and 24-byte keys; the data key is AES-256 only (§16.8, assertion 5).
    if len(key) != KEY_BYTES:
        raise ValueError(f"a key must be {KEY_BYTES} bytes")


def encrypt(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes:
    """Seal `plaintext` under `key` with a fresh random nonce; `aad` must be given again to open it."""
    _require_aes256_key(key)
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext, aad)
    return bytes([FORMAT_VERSION]) + key_id(key) + nonce + sealed


def decrypt(keys: Sequence[bytes], blob: bytes, *, aad: bytes) -> bytes:
    """Open `blob` with the key of `keys` whose id it names; raise `DecryptError` when that is impossible.

    A key of the wrong length is a configuration bug, not a bad blob: it raises `ValueError`.
    """
    for key in keys:
        _require_aes256_key(key)
    if len(blob) < _MIN_BLOB_BYTES:
        raise DecryptError("truncated blob")
    if blob[0] != FORMAT_VERSION:
        raise DecryptError("unsupported blob version")
    wanted = blob[1:_NONCE_START]
    candidates = [key for key in keys if key_id(key) == wanted]
    if not candidates:
        raise DecryptError("no key for this blob")
    nonce, sealed = blob[_NONCE_START:_CIPHERTEXT_START], blob[_CIPHERTEXT_START:]
    for key in candidates:
        try:
            return AESGCM(key).decrypt(nonce, sealed, aad)
        except InvalidTag:
            continue
    raise DecryptError("authentication failed")


def data_keys(cfg: AppConfig) -> list[bytes]:
    """The data key, then `data_key_previous` when it is configured (a rotation): `encrypt` takes the first,
    `decrypt` tries them all."""
    keys = [cfg.secret_b64("data_key")]
    if "data_key_previous" in cfg.secrets:
        keys.append(cfg.secret_b64("data_key_previous"))
    return keys
