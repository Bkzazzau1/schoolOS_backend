"""Where a bank credential rests: sealed before it reaches the database, opened only inside the
server, never handed to a client.

A sealed blob carries the school and connection it belongs to and is checked when opened, so a
ciphertext copied from one school's row to another's will not open.
"""

import json
from abc import ABC, abstractmethod

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


class VaultError(Exception):
    """Sealing or opening failed. The message is safe to show: it never contains a secret."""


class VaultNotConfigured(VaultError):
    """No key is configured, so a credential must not be stored at all."""


class SecretVault(ABC):
    @abstractmethod
    def seal(self, context: dict, secret: dict) -> bytes: ...

    @abstractmethod
    def open(self, context: dict, blob: bytes) -> dict: ...

    @abstractmethod
    def reseal(self, blob: bytes) -> bytes:
        """The same secret under the newest key (used after adding a key to the front of the list)."""


class FernetVault(SecretVault):
    """MultiFernet: the first key seals, every listed key can open - so a key is rotated by adding
    a new one in front, running `reseal` over the records, and only then dropping the old one."""

    def __init__(self, keys: list[str]):
        try:
            self._fernet = MultiFernet([Fernet(k.strip().encode()) for k in keys if k.strip()])
        except (ValueError, TypeError) as error:
            raise VaultNotConfigured("The secure storage keys are not valid.") from error

    def seal(self, context: dict, secret: dict) -> bytes:
        return self._fernet.encrypt(json.dumps({"context": context, "secret": secret}, sort_keys=True).encode())

    def open(self, context: dict, blob: bytes) -> dict:
        try:
            body = json.loads(self._fernet.decrypt(bytes(blob)))
        except (InvalidToken, ValueError) as error:
            raise VaultError("The stored credential could not be opened.") from error
        if body.get("context") != context:
            raise VaultError("The stored credential does not belong to this connection.")
        return body["secret"]

    def reseal(self, blob: bytes) -> bytes:
        try:
            return self._fernet.rotate(bytes(blob))
        except InvalidToken as error:
            raise VaultError("The stored credential could not be opened.") from error


def get_vault() -> SecretVault:
    keys = [k for k in getattr(settings, "BANKCONNECT_SECRET_KEYS", []) if k and k.strip()]
    if not keys:
        raise VaultNotConfigured("Secure storage for bank credentials is not configured on this server.")
    return FernetVault(keys)


def context_for(connection) -> dict:
    return {"school": str(connection.school_id), "connection": str(connection.id)}
