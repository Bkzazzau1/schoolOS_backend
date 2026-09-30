"""The malware scanner abstraction itself: the real, ClamAV-backed implementation behaves correctly whether or
not a real `clamscan` binary is actually on this machine's PATH (mocked - this environment genuinely has none
installed, the same reason apps/media/tests/test_transcoding.py mocks ffmpeg rather than requiring a real one),
and `use_scanner`/`get_scanner` swap the same way `apps/media/transcoding.py`'s `use_transcoder`/`get_transcoder`
already do.
"""

import subprocess
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.media.scanning import ClamAvScanner, Infected, ScannerError, ScannerUnavailable, get_scanner, use_scanner


class ClamAvScannerTests(SimpleTestCase):
    def test_with_no_clamscan_on_this_machine_it_says_so_plainly_rather_than_crashing(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaises(ScannerUnavailable) as caught:
                ClamAvScanner().scan(b"anything at all")
        self.assertEqual(caught.exception.code, "scanner_unavailable")

    def test_a_clean_result_returns_normally(self):
        with patch("shutil.which", return_value="/usr/bin/clamscan"), patch("subprocess.run", return_value=MagicMock(returncode=0)):
            ClamAvScanner().scan(b"a perfectly ordinary file")  # must not raise

    def test_a_detected_virus_is_reported_as_infected_not_a_scanner_failure(self):
        with patch("shutil.which", return_value="/usr/bin/clamscan"), patch("subprocess.run", return_value=MagicMock(returncode=1)):
            with self.assertRaises(Infected) as caught:
                ClamAvScanner().scan(b"eicar-test-string-or-similar")
        self.assertEqual(caught.exception.code, "malware_detected")

    def test_the_scanner_itself_erroring_is_a_genuine_failure_not_unavailable_or_infected(self):
        with patch("shutil.which", return_value="/usr/bin/clamscan"), patch("subprocess.run", return_value=MagicMock(returncode=2)):
            with self.assertRaises(ScannerError) as caught:
                ClamAvScanner().scan(b"something clamscan itself chokes on")
        self.assertNotIsInstance(caught.exception, Infected)
        self.assertEqual(caught.exception.code, "scan_failed")

    def test_clamscan_taking_too_long_is_a_genuine_failure_not_unavailable(self):
        with patch("shutil.which", return_value="/usr/bin/clamscan"), patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="clamscan", timeout=60)
        ):
            with self.assertRaises(ScannerError) as caught:
                ClamAvScanner().scan(b"slow")
        self.assertEqual(caught.exception.code, "scan_timed_out")


class ScannerRegistryTests(SimpleTestCase):
    def test_the_default_scanner_is_the_real_clamav_backed_one(self):
        self.assertIsInstance(get_scanner(), ClamAvScanner)

    def test_use_scanner_overrides_get_scanner_only_for_the_duration_of_the_block(self):
        class Fake:
            def scan(self, data):
                return None

        fake = Fake()
        with use_scanner(fake):
            self.assertIs(get_scanner(), fake)
        self.assertIsInstance(get_scanner(), ClamAvScanner)
