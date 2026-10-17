import hashlib
import re
import unicodedata
from importlib.resources import files
from typing import Any, Final

import pytest
from argon2 import PasswordHasher
from jobhunter.auth import passwords
from jobhunter.auth.passwords import (
    WeakPassword,
    check_password_policy,
    dummy_verify,
    hash_password,
    verify_password,
)

# sha256 of the upstream 10k-most-common.txt at the commit named in the header of the list
UPSTREAM_SHA256 = "68782d6a4a19a4768d5f15dd66bd534e7a33055cc755411e33f16d18c50fdcce"


def test_argon2id_parameters() -> None:
    assert hash_password("correct horse battery").startswith("$argon2id$v=19$m=65536,t=3,p=4$")


def test_policy_length_and_common_list() -> None:
    with pytest.raises(WeakPassword) as e:
        check_password_policy("elevenchars")
    assert e.value.reason == "too_short"
    lines = files("jobhunter.auth").joinpath("common_passwords.txt").read_text().splitlines()
    words = [
        w for line in lines if not line.startswith("#") for w in line.split()
    ]  # whole '#' lines are comments
    common = next(w for w in words if len(w) >= 12)
    with pytest.raises(WeakPassword) as e2:
        check_password_policy(common.upper())
    assert e2.value.reason == "common"
    check_password_policy("Tannenbaum-Ölfass-7")


def test_common_password_file_is_pinned_and_complete() -> None:
    lines = files("jobhunter.auth").joinpath("common_passwords.txt").read_text(encoding="utf-8").splitlines()
    # The first line names the source path, the upstream commit and the licence (ruling T15-a).
    assert lines[0].startswith("#")
    assert "SecLists" in lines[0] and "Passwords/Common-Credentials/10k-most-common.txt" in lines[0]
    assert re.search(r"\b[0-9a-f]{40}\b", lines[0]) and "MIT" in lines[0]
    entries = [line for line in lines if not line.startswith("#")]
    assert len(entries) >= 9_000 and all(entry and entry == entry.strip() for entry in entries)
    assert {"password", "123456", "qwerty"} <= set(entries)


def test_the_list_body_is_the_pinned_upstream_file() -> None:
    """Every line after the header is the upstream SecLists file, byte for byte."""
    data = files("jobhunter.auth").joinpath("common_passwords.txt").read_bytes()
    header, newline, body = data.partition(b"\n")
    assert newline and header.startswith(b"#")
    assert hashlib.sha256(body).hexdigest() == UPSTREAM_SHA256


def test_policy_counts_characters_after_nfc_and_ignores_case_of_common_passwords() -> None:
    # Twelve characters as NFC (ü and ß are one each), but more code points as NFD.
    twelve_nfc = unicodedata.normalize("NFC", "Müßlerstrasse")[:12]
    assert len(twelve_nfc) == 12
    check_password_policy(unicodedata.normalize("NFD", twelve_nfc))
    eleven_nfc = twelve_nfc[:11]
    with pytest.raises(WeakPassword) as e:
        check_password_policy(unicodedata.normalize("NFD", eleven_nfc))
    assert e.value.reason == "too_short"
    # An exactly 12-character common password in mixed case is still common.
    with pytest.raises(WeakPassword) as e2:
        check_password_policy("MasterBating")
    assert e2.value.reason == "common"


def test_weak_password_message_never_contains_the_password() -> None:
    for weak in ("hunter2", "Unbelievable"):
        with pytest.raises(WeakPassword) as e:
            check_password_policy(weak)
        assert weak.lower() not in (str(e.value) + repr(e.value.args) + repr(e.value)).lower()


def test_nfc_and_nfd_passwords_verify_alike() -> None:  # Review Focus 3
    nfc = unicodedata.normalize("NFC", "Müllerstraße-2026!")
    assert verify_password(hash_password(nfc), unicodedata.normalize("NFD", nfc))[0]


def test_rehash_when_parameters_change() -> None:
    old = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=4).hash("Tannenbaum-Ölfass-7")
    ok, new = verify_password(old, "Tannenbaum-Ölfass-7")
    assert ok and new is not None and new.startswith("$argon2id$v=19$m=65536,t=3,p=4$")


def test_current_parameters_need_no_rehash_and_a_wrong_password_never_rehashes() -> None:
    stored = hash_password("Tannenbaum-Ölfass-7")
    assert verify_password(stored, "Tannenbaum-Ölfass-7") == (True, None)
    assert verify_password(stored, "Tannenbaum-Ölfass-8") == (False, None)
    outdated = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=4).hash("Tannenbaum-Ölfass-7")
    assert verify_password(outdated, "wrong-password-123") == (False, None)


def test_malformed_stored_hash_is_a_failed_verification() -> None:
    assert verify_password("not-a-hash", "Tannenbaum-Ölfass-7") == (False, None)
    assert verify_password("", "Tannenbaum-Ölfass-7") == (False, None)


_REAL: Final = passwords.ARGON2


class _Spy:
    """Stands in for `passwords.ARGON2`: counts `verify` calls and raises `error` when it is given."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.verified: list[str] = []

    def verify(self, stored: str, password: str) -> bool:
        self.verified.append(stored)
        if self.error is not None:
            raise self.error
        return _REAL.verify(stored, password)

    def check_needs_rehash(self, stored: str) -> bool:
        return _REAL.check_needs_rehash(stored)


def test_errors_other_than_a_failed_verification_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = hash_password("Tannenbaum-Ölfass-7")
    monkeypatch.setattr(passwords, "ARGON2", _Spy(error=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        verify_password(stored, "Tannenbaum-Ölfass-7")
    with pytest.raises(RuntimeError, match="boom"):
        dummy_verify("whatever")


def test_dummy_verify_does_one_real_verify_at_the_configured_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy: Any = _Spy()
    monkeypatch.setattr(passwords, "ARGON2", spy)
    dummy_verify("Tannenbaum-Ölfass-7")
    assert len(spy.verified) == 1
    # The hash it verifies against was made with the very parameters of ARGON2: same cost as a real login.
    assert spy.verified[0].startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert not _REAL.check_needs_rehash(spy.verified[0])
