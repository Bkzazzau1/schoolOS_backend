import hashlib
import shutil
import tempfile
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import override_settings
from PIL import Image
from rest_framework.test import APITestCase

from apps.administration.specs import DOCUMENT_RECORDS
from apps.schoollife.community.common import POST as COMMUNITY_POST
from apps.schoollife.specs.campus import GALLERY
from apps.schools.models import Membership, Role, School
from apps.staff.constants import PROFILE as STAFF_PROFILE
from apps.structure.constants import APPEARANCE, APPEARANCE_ID
from apps.sync.models import SyncRecord

User = get_user_model()
PASSWORD = "a-long-test-password-1"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_png(width=6, height=4, color=(200, 30, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"


@override_settings(MEDIA_INLINE_JOBS=True, MEDIA_STORAGE_BACKEND="local")
class MediaTestCase(APITestCase):
    """A school with a Gallery album (school-wide and an internal one), a Community post, a staff profile, and an
    administrator document record - one real owner of each kind the media app is wired to today - plus a second,
    separate school with its own album, for the isolation tests. Local storage, writing to a fresh temp directory
    per test class."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="schoolos_media_test_")
        cls._media_root_override = override_settings(MEDIA_LOCAL_ROOT=cls._media_root)
        cls._media_root_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_root_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.school = School.objects.create(name="BrightGate", slug="brightgate")
        self.other_school = School.objects.create(name="Other", slug="other")
        self.members = {}
        for role in Role:
            user = User.objects.create_user(f"{role.value}@school.ng", PASSWORD)
            self.members[role.value] = Membership.objects.create(user=user, school=self.school, role=role)
        self.owner = self.members["proprietor"]
        self.principal = self.members["principal"]
        self.administrator = self.members["administrator"]
        self.teacher = self.members["teacher"]
        self.parent = self.members["parent"]
        self.student = self.members["student"]

        self.other_owner = Membership.objects.create(
            user=User.objects.create_user("owner@other.ng", PASSWORD), school=self.other_school, role=Role.PROPRIETOR
        )

        self.album_id = "ALBUM-1"
        SyncRecord.objects.create(
            school=self.school, entity_type=GALLERY.entity_type, entity_id=self.album_id,
            payload={"id": self.album_id, "title": "Sports Day", "termId": "T1", "visibility": "publicShowcase", "createdByMembershipId": str(self.teacher.id)},
        )
        self.internal_album_id = "ALBUM-2"
        SyncRecord.objects.create(
            school=self.school, entity_type=GALLERY.entity_type, entity_id=self.internal_album_id,
            payload={"id": self.internal_album_id, "title": "Staff Only", "termId": "T1", "visibility": "internal", "createdByMembershipId": str(self.teacher.id)},
        )
        self.other_school_album_id = "ALBUM-OTHER"
        SyncRecord.objects.create(
            school=self.other_school, entity_type=GALLERY.entity_type, entity_id=self.other_school_album_id,
            payload={"id": self.other_school_album_id, "title": "Elsewhere", "termId": "T1"},
        )

        self.post_id = "POST-1"
        SyncRecord.objects.create(
            school=self.school, entity_type=COMMUNITY_POST, entity_id=self.post_id,
            payload={"id": self.post_id, "createdByMembershipId": str(self.teacher.id)},
        )

        self.staff_id = "STAFF-1"
        SyncRecord.objects.create(
            school=self.school, entity_type=STAFF_PROFILE, entity_id=self.staff_id,
            payload={"staffId": self.staff_id, "linkedMembershipId": str(self.teacher.id)},
        )

        self.document_record_id = "DOC-1"
        SyncRecord.objects.create(
            school=self.school, entity_type=DOCUMENT_RECORDS.entity_type, entity_id=self.document_record_id,
            payload={"id": self.document_record_id, "document": "Birth certificate", "recordOwner": "student-1", "kind": "admission"},
        )

        SyncRecord.objects.create(
            school=self.school, entity_type=APPEARANCE, entity_id=APPEARANCE_ID,
            payload={"themeId": "forest", "updatedByMembershipId": str(self.owner.id)},
        )

    # -- API -------------------------------------------------------------------------------------

    def path(self, tail: str, school=None) -> str:
        return f"/api/v1/schools/{(school or self.school).id}/media/{tail}"

    def get(self, tail, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.get(self.path(tail, school))

    def post(self, tail, body=None, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.post(self.path(tail, school), body or {}, format="json")

    def put_body(self, tail, data: bytes, mime_type: str, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.generic("PUT", self.path(tail, school), data=data, content_type=mime_type)

    def initiate(self, *, owner_type, owner_id, category, data: bytes, file_name="photo.png", mime_type="image/png", who=None, **over):
        body = {
            "ownerType": owner_type, "ownerId": owner_id, "category": category, "fileName": file_name,
            "mimeType": mime_type, "byteSize": len(data), "sha256": sha256(data),
        }
        body.update(over)
        return self.post("assets/", body, who=who)

    def download_bytes(self, asset_id, *, thumbnail=False, who=None, school=None):
        """The two-hop flow every download is: ask where to get it, then get it from there - proven end to end,
        the way the app itself has to do it, rather than assuming local storage's shortcut."""
        info = self.get(f"assets/{asset_id}/download/{'?thumbnail=1' if thumbnail else ''}", who=who, school=school)
        self.assertEqual(info.status_code, 200, info.content)
        body = info.json()
        self.assertEqual(body["mode"], "stream")  # local storage, in every test that calls this
        raw = self.get(body["url"], who=who, school=school)
        self.assertEqual(raw.status_code, 200)
        return b"".join(raw.streaming_content)

    def upload_all(self, *, owner_type, owner_id, category, data, file_name="photo.png", mime_type="image/png", who=None, **over):
        """The whole flow local storage supports end to end: initiate, PUT the bytes, complete."""
        created = self.initiate(owner_type=owner_type, owner_id=owner_id, category=category, data=data, file_name=file_name, mime_type=mime_type, who=who, **over)
        self.assertEqual(created.status_code, 201, created.content)
        asset_id = created.json()["asset"]["id"]
        put = self.put_body(f"assets/{asset_id}/upload/", data, mime_type, who=who)
        self.assertEqual(put.status_code, 200, put.content)
        done = self.post(f"assets/{asset_id}/complete/", who=who)
        self.assertEqual(done.status_code, 200, done.content)
        return done.json()["asset"]
