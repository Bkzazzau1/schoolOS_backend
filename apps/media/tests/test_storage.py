"""The storage abstraction itself: local disk behaves as the interface promises, the S3-compatible adapter calls
boto3 the way a presigned-URL flow needs (mocked - no real network, no real Wasabi/AWS account), and the whole
upload API works unchanged against a fake object-store backend, proving the business logic never assumes which
storage is in effect.
"""

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.media.constants import Status
from apps.media.storage import DownloadInstructions, MediaStorage, StoredObject, UploadInstructions, get_storage, use_storage
from apps.media.storage.base import MediaStorageError
from apps.media.storage.local import LocalMediaStorage

from .base import MediaTestCase, make_png


class LocalStorageUnitTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = LocalMediaStorage(root=Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_key_that_has_not_been_written_does_not_exist(self):
        self.assertIsNone(self.storage.verify_existence("a/b/c.png"))

    def test_write_then_verify_then_read_round_trips_the_exact_bytes(self):
        self.storage.write_bytes("a/b/c.png", b"hello world", mime_type="image/png")
        found = self.storage.verify_existence("a/b/c.png")
        self.assertEqual(found.byte_size, 11)
        self.assertEqual(self.storage.read_bytes("a/b/c.png", max_bytes=100), b"hello world")

    def test_reading_more_than_the_ceiling_refuses_rather_than_returning_partial_bytes(self):
        self.storage.write_bytes("a.png", b"0123456789", mime_type="image/png")
        with self.assertRaises(MediaStorageError):
            self.storage.read_bytes("a.png", max_bytes=5)

    def test_a_storage_key_cannot_escape_the_media_root(self):
        with self.assertRaises(MediaStorageError):
            self.storage.absolute_path("../../etc/passwd")

    def test_deleting_a_key_that_was_never_written_is_not_an_error(self):
        self.storage.delete("never-existed.png")  # must not raise

    def test_local_upload_instructions_point_back_at_this_apps_own_endpoint(self):
        instructions = self.storage.initiate_upload("a.png", mime_type="image/png", asset_id="ASSET-1")
        self.assertEqual(instructions.mode, "direct")
        self.assertEqual(instructions.upload_url, "assets/ASSET-1/upload/")


class FakeS3Client:
    """A stand-in for boto3's S3 client: an in-memory bucket, so the presigned-URL-issuing methods can be checked
    without botocore ever opening a socket."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.presign_calls: list[tuple[str, dict]] = []

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.presign_calls.append((operation, Params))
        return f"https://fake-bucket.s3.example/{Params['Key']}?op={operation}&ttl={ExpiresIn}"

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key]), "ETag": '"abc123"'}

    def get_object(self, *, Bucket, Key):
        body = MagicMock()
        body.read.return_value = self.objects[Key]
        return {"Body": body}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)


class S3StorageUnitTests(SimpleTestCase):
    def _storage(self, client):
        from apps.media.storage.s3 import S3MediaStorage

        with patch("boto3.client", return_value=client):
            return S3MediaStorage(bucket="brightgate-files", region="us-east-1", endpoint_url="https://s3.wasabisys.com", access_key="AK", secret_key="SK")

    def test_initiating_an_upload_asks_for_a_presigned_put_and_hands_back_no_credential_of_any_kind(self):
        client = FakeS3Client()
        storage = self._storage(client)
        instructions = storage.initiate_upload("school/image/2026/01/key.png", mime_type="image/png", asset_id="ASSET-1")
        self.assertEqual(instructions.mode, "presigned_put")
        self.assertIn("key.png", instructions.upload_url)
        self.assertNotIn("SK", instructions.upload_url)
        self.assertNotIn("AK", instructions.upload_url)
        operation, params = client.presign_calls[0]
        self.assertEqual((operation, params["Bucket"], params["Key"], params["ContentType"]), ("put_object", "brightgate-files", "school/image/2026/01/key.png", "image/png"))

    def test_verify_existence_is_none_for_an_object_never_written(self):
        storage = self._storage(FakeS3Client())
        self.assertIsNone(storage.verify_existence("nope.png"))

    def test_write_then_verify_then_read_round_trips_through_the_fake_bucket(self):
        client = FakeS3Client()
        storage = self._storage(client)
        storage.write_bytes("key.png", b"pretend bytes", mime_type="image/png")
        found = storage.verify_existence("key.png")
        self.assertEqual(found.byte_size, len(b"pretend bytes"))
        self.assertEqual(storage.read_bytes("key.png", max_bytes=1000), b"pretend bytes")

    def test_open_download_asks_for_a_presigned_get_with_the_files_own_name_and_type(self):
        client = FakeS3Client()
        storage = self._storage(client)
        instructions = storage.open_download("key.png", filename="party.png", mime_type="image/png", ttl_seconds=120)
        self.assertEqual(instructions.mode, "redirect")
        operation, params = client.presign_calls[0]
        self.assertEqual(operation, "get_object")
        self.assertIn("party.png", params["ResponseContentDisposition"])
        self.assertGreater(instructions.expires_at, timezone.now())

    def test_delete_asks_the_bucket_to_forget_the_key(self):
        client = FakeS3Client()
        storage = self._storage(client)
        storage.write_bytes("key.png", b"x", mime_type="image/png")
        storage.delete("key.png")
        self.assertIsNone(storage.verify_existence("key.png"))


class FakeRemoteStorage(MediaStorage):
    """A minimal in-memory stand-in for an S3-compatible backend, used through `use_storage` so the whole upload
    API (not just the S3 adapter in isolation) is proven to work unchanged against a non-local backend: uploads
    go to a "presigned" URL of its own choosing and downloads come back as a redirect, exactly like the real
    S3 adapter, but with no network and no botocore involved."""

    provider = "s3"

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def initiate_upload(self, storage_key, *, mime_type, asset_id):
        return UploadInstructions(mode="presigned_put", upload_url=f"https://fake-object-store.example/{storage_key}", method="PUT")

    def verify_existence(self, storage_key):
        if storage_key not in self.objects:
            return None
        return StoredObject(byte_size=len(self.objects[storage_key]))

    def read_bytes(self, storage_key, *, max_bytes):
        data = self.objects.get(storage_key)
        if data is None:
            raise MediaStorageError("not_found", "not found")
        return data

    def write_bytes(self, storage_key, data, *, mime_type):
        self.objects[storage_key] = data

    def open_download(self, storage_key, *, filename, mime_type, ttl_seconds):
        return DownloadInstructions(mode="redirect", url=f"https://fake-object-store.example/{storage_key}", expires_at=timezone.now() + timedelta(seconds=ttl_seconds))

    def delete(self, storage_key):
        self.objects.pop(storage_key, None)


@override_settings(MEDIA_INLINE_JOBS=True)
class FakeRemoteBackendIntegrationTests(MediaTestCase):
    """The same API tests as local storage's upload_all, but against a fake remote backend - the device "uploads"
    straight to the fake object store's key (never through this app's own upload endpoint - there is no local PUT
    to make), then completion notices the bytes are there on its own."""

    def upload_via_remote(self, storage, *, owner_type, owner_id, category, data, mime_type="image/png"):
        """Everything happens while the same fake backend is the ambient one (`get_storage()` always returns
        whatever `use_storage` currently has installed) - exactly the real constraint a server also has: one
        storage backend is in effect for as long as it is configured, never chosen per call."""
        created = self.initiate(owner_type=owner_type, owner_id=owner_id, category=category, data=data, mime_type=mime_type, who=self.teacher)
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertEqual(body["upload"]["mode"], "presigned_put")
        self.assertEqual(body["asset"]["status"], Status.PENDING_UPLOAD)
        asset_id = body["asset"]["id"]

        # The device now PUTs straight to the object store - simulated here, since that call never reaches this
        # server at all, exactly as the real flow works.
        storage.write_bytes(_key_for(asset_id), data, mime_type=mime_type)

        done = self.post(f"assets/{asset_id}/complete/", who=self.teacher)
        self.assertEqual(done.status_code, 200, done.content)
        return done.json()["asset"]

    def test_a_photo_uploaded_through_a_fake_remote_backend_becomes_available_with_a_thumbnail(self):
        with use_storage(FakeRemoteStorage()) as storage:
            asset = self.upload_via_remote(storage, owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png())
        self.assertEqual(asset["status"], Status.AVAILABLE)
        self.assertTrue(asset["hasThumbnail"])

    def test_completing_before_the_device_has_really_put_the_bytes_is_refused(self):
        with use_storage(FakeRemoteStorage()):
            created = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png(), who=self.teacher)
            asset_id = created.json()["asset"]["id"]
            response = self.post(f"assets/{asset_id}/complete/", who=self.teacher)
            self.assertEqual((response.status_code, response.json()["code"]), (400, "not_uploaded"))

    def test_downloading_asks_the_fake_backend_and_gets_a_redirect_not_a_stream(self):
        with use_storage(FakeRemoteStorage()) as storage:
            asset = self.upload_via_remote(storage, owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=make_png())
            response = self.get(f"assets/{asset['id']}/download/", who=self.teacher)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["mode"], "redirect")
            self.assertIn(_key_for(asset["id"]), response.json()["url"])

    def test_use_storage_is_what_get_storage_returns_for_the_life_of_the_with_block_and_is_undone_after(self):
        fake = FakeRemoteStorage()
        with use_storage(fake):
            self.assertIs(get_storage(), fake)
        self.assertIsNot(get_storage(), fake)


def _key_for(asset_id: str) -> str:
    from apps.media.models import MediaAsset

    return MediaAsset.objects.get(id=asset_id).storage_key
