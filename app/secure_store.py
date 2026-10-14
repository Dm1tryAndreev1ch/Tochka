"""Authenticated encrypted objects in a private directory outside web/static."""

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.errors import AppError


class SecureStore:
    def __init__(self, root, company="local-company"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.objects = self.root / "encrypted-archive"
        self.objects.mkdir(exist_ok=True, mode=0o700)
        self.objects.chmod(0o700)
        key_path = self.root / "archive-master.key"
        marker = self.root / "archive-key.fingerprint"
        if marker.exists() and not key_path.exists():
            raise RuntimeError("Archive key is missing; restore the existing key")
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            if any(self.objects.glob("*.enc")):
                os.close(fd)
                key_path.unlink()
                raise RuntimeError("Archive key is missing; restore it before opening the existing archive")
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
        key_path.chmod(0o600)
        key = key_path.read_bytes()
        if len(key) != 32:
            raise RuntimeError("Archive key is invalid; restore the existing key, do not replace it")
        fingerprint = hashlib.sha256(key).hexdigest()
        if marker.exists() and marker.read_text().strip() != fingerprint:
            raise RuntimeError("Archive key differs from its registered fingerprint")
        if not marker.exists():
            marker.write_text(fingerprint + "\n")
            marker.chmod(0o600)
        self.cipher = AESGCM(key)
        self._key = key
        self.company = company

    def sign(self, data):
        return hmac.new(self._key, b"audit-v1:" + data, hashlib.sha256).hexdigest()

    def path(self, record, kind):
        if kind not in ("transcript", "audio", "backup", "capture"):
            raise ValueError("Unknown protected object type")
        return self.objects / f"{UUID(str(record))}.{kind}.enc"

    def seal(self, record, kind, data):
        context = f"voice-desk:v1:{self.company}:{UUID(str(record))}:{kind}".encode()
        nonce = secrets.token_bytes(12)
        return b"VD1" + nonce + self.cipher.encrypt(nonce, data, context)

    def open(self, record, kind, data):
        context = f"voice-desk:v1:{self.company}:{UUID(str(record))}:{kind}".encode()
        if not data.startswith(b"VD1") or len(data) < 31:
            raise AppError("archive_integrity", "Не удалось проверить целостность архивного объекта", 503)
        try:
            return self.cipher.decrypt(data[3:15], data[15:], context)
        except InvalidTag:
            raise AppError("archive_integrity", "Не удалось проверить целостность архивного объекта", 503) from None

    def write(self, record, kind, data):
        path = self.path(record, kind)
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            temporary.chmod(0o600)
            stream.write(self.seal(record, kind, data))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def read(self, record, kind):
        path = self.path(record, kind)
        if not path.is_file():
            raise AppError("archive_not_found", "Архивный объект не найден", 404)
        if path.stat().st_size > 512 * 1024 * 1024:
            raise AppError("archive_too_large", "Архивный объект превышает допустимый размер", 413)
        return self.open(record, kind, path.read_bytes())

    def delete(self, record):
        for kind in ("audio", "transcript", "capture"):
            self.path(record, kind).unlink(missing_ok=True)
