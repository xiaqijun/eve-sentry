"""Store an encrypted copy of the active SeAT service token."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from app.server.auth_store import AuthRepository


SEAT_TOKEN_COPY_SETTING = "seat_service_token_copy"


class SeatTokenVault:
    """Keep ciphertext in the database and the encryption key in private state."""

    def __init__(self, repository: AuthRepository, key_path: Path) -> None:
        self.repository = repository
        self.key_path = key_path

    def load(self) -> str:
        ciphertext = self.repository.setting(SEAT_TOKEN_COPY_SETTING)
        if not ciphertext:
            return ""
        return Fernet(self._read_key()).decrypt(ciphertext.encode("ascii")).decode("ascii")

    def save(self, token: str, now: str) -> None:
        try:
            key = self._read_key()
        except FileNotFoundError:
            key = self._create_key()
        ciphertext = Fernet(key).encrypt(token.encode("ascii")).decode("ascii")
        self.repository.set_setting(SEAT_TOKEN_COPY_SETTING, ciphertext, now)

    def _read_key(self) -> bytes:
        if self.key_path.is_symlink():
            raise PermissionError("SeAT token key must be a regular file")
        with self.key_path.open("rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or (os.name != "nt" and mode & 0o077):
                raise PermissionError("SeAT token key must be private")
            return stream.read(128)

    def _create_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.key_path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self.key_path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return self._read_key()
