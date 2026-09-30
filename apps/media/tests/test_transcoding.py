"""The video transcoder abstraction itself: the real, ffmpeg-backed implementation behaves correctly whether or
not a real `ffmpeg` binary is actually on this machine's PATH (mocked - this environment genuinely has none
installed, the same reason apps/media/tests/test_storage.py mocks boto3 rather than hitting a real object
store), and `use_transcoder`/`get_transcoder` swap the same way `apps/media/storage`'s `use_storage`/`get_storage`
already do.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.media.transcoding import FfmpegTranscoder, TranscoderError, TranscoderUnavailable, get_transcoder, use_transcoder


class FfmpegTranscoderTests(SimpleTestCase):
    def test_with_no_ffmpeg_on_this_machine_it_says_so_plainly_rather_than_crashing(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaises(TranscoderUnavailable) as caught:
                FfmpegTranscoder().build_thumbnail(b"not a real video")
        self.assertEqual(caught.exception.code, "transcoder_unavailable")

    def test_when_ffmpeg_is_present_it_shells_out_and_reads_back_the_frame_it_wrote(self):
        written_frame = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

        def fake_run(cmd, **kwargs):
            # The real FfmpegTranscoder asks ffmpeg to write its one extracted frame to a path it names on the
            # command line - write that same frame here, the way a real ffmpeg process would, so the rest of
            # build_thumbnail's own read-back-and-measure logic runs unmodified against real bytes.
            out_path = Path(cmd[-1])
            out_path.write_bytes(written_frame)
            return MagicMock(returncode=0)

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch("subprocess.run", side_effect=fake_run), patch(
            "apps.media.transcoding.probe_dimensions", return_value=(64, 36)
        ):
            data, width, height, mime_type = FfmpegTranscoder().build_thumbnail(b"pretend this is a real video")
        self.assertEqual((data, width, height, mime_type), (written_frame, 64, 36, "image/jpeg"))

    def test_a_nonzero_exit_from_ffmpeg_is_a_genuine_decode_failure_not_unavailable(self):
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch("subprocess.run", return_value=MagicMock(returncode=1)):
            with self.assertRaises(TranscoderError) as caught:
                FfmpegTranscoder().build_thumbnail(b"corrupt")
        self.assertEqual(caught.exception.code, "video_frame_extraction_failed")

    def test_ffmpeg_taking_too_long_is_a_genuine_failure_not_unavailable(self):
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30)
        ):
            with self.assertRaises(TranscoderError) as caught:
                FfmpegTranscoder().build_thumbnail(b"slow")
        self.assertEqual(caught.exception.code, "video_frame_extraction_timed_out")


class TranscoderRegistryTests(SimpleTestCase):
    def test_the_default_transcoder_is_the_real_ffmpeg_backed_one(self):
        self.assertIsInstance(get_transcoder(), FfmpegTranscoder)

    def test_use_transcoder_overrides_get_transcoder_only_for_the_duration_of_the_block(self):
        class Fake:
            def build_thumbnail(self, data):
                return b"", 0, 0, "image/jpeg"

        fake = Fake()
        with use_transcoder(fake):
            self.assertIs(get_transcoder(), fake)
        self.assertIsInstance(get_transcoder(), FfmpegTranscoder)
