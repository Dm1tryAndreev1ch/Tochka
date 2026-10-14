from uuid import uuid4

import pytest

from app.errors import AppError
from app.secure_store import SecureStore


def test_archive_is_encrypted_bound_to_object_and_detects_tampering(tmp_path):
    store = SecureStore(tmp_path)
    record = uuid4()
    payload = b"Private test conversation and telephone +000000000"
    store.write(record, "transcript", payload)
    cipher = store.path(record, "transcript").read_bytes()
    assert payload not in cipher
    assert store.read(record, "transcript") == payload
    other = uuid4()
    store.path(other, "transcript").write_bytes(cipher)
    with pytest.raises(AppError) as bound:
        store.read(other, "transcript")
    assert bound.value.code == "archive_integrity"
    store.path(record, "transcript").write_bytes(cipher[:-1] + bytes([cipher[-1] ^ 1]))
    with pytest.raises(AppError):
        store.read(record, "transcript")
