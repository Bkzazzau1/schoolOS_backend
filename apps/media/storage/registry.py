"""Which storage backend is in effect: `MEDIA_STORAGE_BACKEND` in settings, normally - or, in a test, whatever
`use_storage` installs for the duration of a `with` block, the same override-for-tests shape
`apps/bankconnect/providers/transport.py: use_transport` already uses for a provider connector.
"""

from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

from .base import MediaStorage, MediaStorageError

_override: MediaStorage | None = None


@contextmanager
def use_storage(storage: MediaStorage):
    global _override
    previous, _override = _override, storage
    try:
        yield storage
    finally:
        _override = previous


def get_storage() -> MediaStorage:
    if _override is not None:
        return _override

    backend = getattr(settings, "MEDIA_STORAGE_BACKEND", "local")
    if backend == "s3":
        bucket = getattr(settings, "MEDIA_S3_BUCKET", "")
        if not bucket:
            raise MediaStorageError("storage_not_configured", "Object storage is not configured on this server.")
        from .s3 import S3MediaStorage

        return S3MediaStorage(
            bucket=bucket,
            region=getattr(settings, "MEDIA_S3_REGION", ""),
            endpoint_url=getattr(settings, "MEDIA_S3_ENDPOINT_URL", ""),
            access_key=getattr(settings, "MEDIA_S3_ACCESS_KEY", ""),
            secret_key=getattr(settings, "MEDIA_S3_SECRET_KEY", ""),
        )

    from .local import LocalMediaStorage

    root = getattr(settings, "MEDIA_LOCAL_ROOT", "") or (Path(settings.BASE_DIR) / "media_storage")
    return LocalMediaStorage(root=Path(root))
