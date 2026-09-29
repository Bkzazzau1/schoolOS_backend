"""S3-compatible object storage (Wasabi, or any provider speaking the same API) for production.

Nothing here is Wasabi-specific: `endpoint_url` is the one setting that points the same S3 client at a different
provider (or at real AWS S3, or at a local S3-compatible server for integration testing), never a hard-coded host.
Every credential lives in settings/environment; nothing here or in a response ever hands a raw access key to a
client - only short-lived presigned URLs, each good for one key and one operation.
"""

from datetime import timedelta

from django.utils import timezone

from .base import DownloadInstructions, MediaStorage, MediaStorageError, StoredObject, UploadInstructions

UPLOAD_URL_TTL_SECONDS = 15 * 60


class S3MediaStorage(MediaStorage):
    provider = "s3"

    def __init__(self, *, bucket: str, region: str, endpoint_url: str, access_key: str, secret_key: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            region_name=region or None,
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def initiate_upload(self, storage_key, *, mime_type, asset_id):
        url = self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": storage_key, "ContentType": mime_type},
            ExpiresIn=UPLOAD_URL_TTL_SECONDS,
        )
        return UploadInstructions(
            mode="presigned_put",
            upload_url=url,
            method="PUT",
            headers={"Content-Type": mime_type},
            expires_at=timezone.now() + timedelta(seconds=UPLOAD_URL_TTL_SECONDS),
        )

    def verify_existence(self, storage_key):
        from botocore.exceptions import ClientError

        try:
            head = self._client.head_object(Bucket=self.bucket, Key=storage_key)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise MediaStorageError("storage_unavailable", "The storage provider could not be reached.") from error
        return StoredObject(byte_size=int(head["ContentLength"]), etag=head.get("ETag", "").strip('"'))

    def read_bytes(self, storage_key, *, max_bytes):
        from botocore.exceptions import ClientError

        found = self.verify_existence(storage_key)
        if found is None:
            raise MediaStorageError("not_found", "That file is not in storage.")
        if found.byte_size > max_bytes:
            raise MediaStorageError("too_large_to_read", "That file is too large to process here.")
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=storage_key)
        except ClientError as error:
            raise MediaStorageError("storage_unavailable", "The storage provider could not be reached.") from error
        return obj["Body"].read()

    def write_bytes(self, storage_key, data, *, mime_type):
        from botocore.exceptions import ClientError

        try:
            self._client.put_object(Bucket=self.bucket, Key=storage_key, Body=data, ContentType=mime_type)
        except ClientError as error:
            raise MediaStorageError("storage_unavailable", "The storage provider could not be reached.") from error

    def open_download(self, storage_key, *, filename, mime_type, ttl_seconds):
        safe_name = filename.replace('"', "")
        url = self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": storage_key,
                "ResponseContentDisposition": f'inline; filename="{safe_name}"',
                "ResponseContentType": mime_type,
            },
            ExpiresIn=ttl_seconds,
        )
        return DownloadInstructions(mode="redirect", url=url, expires_at=timezone.now() + timedelta(seconds=ttl_seconds))

    def delete(self, storage_key):
        from botocore.exceptions import ClientError

        try:
            self._client.delete_object(Bucket=self.bucket, Key=storage_key)
        except ClientError as error:
            raise MediaStorageError("storage_unavailable", "The storage provider could not be reached.") from error
