"""Checking an upload's real bytes for malware before they are ever trusted as verified - the same
override-for-tests shape apps/media/storage/registry.py already uses for storage and apps/media/transcoding.py
already uses for video thumbnails. Where no real scanner is installed, ScannerUnavailable is a known, expected
condition - see uploads.py: run_verify - never a job failure: the upload is still checksum- and
signature-verified; it simply was not checked for malware, the same honest gap the QUARANTINED status has always
documented (see constants.py).
"""

import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager


class ScannerError(Exception):
    """Something the scanner itself refused or could not do. `code` is a stable string."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ScannerUnavailable(ScannerError):
    """No real malware scanner exists on this server."""

    def __init__(self, message: str = "No malware scanner is available on this server."):
        super().__init__("scanner_unavailable", message)


class Infected(ScannerError):
    """The scanner ran successfully and found malware - not a scanner failure, the opposite: it did exactly its
    job. `code` is the signature/name the scanner reported, when it has one."""

    def __init__(self, code: str = "malware_detected", message: str = "This file was flagged by a malware scan."):
        super().__init__(code, message)


class MalwareScanner(ABC):
    @abstractmethod
    def scan(self, data: bytes) -> None:
        """Raises Infected if these bytes are malicious; raises ScannerUnavailable if this server has no real way
        to scan at all; raises ScannerError for a genuine scan failure (the scanner itself could not finish).
        Returns normally - none of the above - when the file is clean."""


class ClamAvScanner(MalwareScanner):
    """Shells out to a real `clamscan` binary - ClamAV's own standalone command-line scanner, no daemon required,
    the same "just a binary on PATH" shape FfmpegTranscoder already uses for ffmpeg. Raises ScannerUnavailable
    when clamscan is not actually installed on this server - the day it is, this starts scanning with no other
    code change."""

    def scan(self, data: bytes) -> None:
        clamscan = shutil.which("clamscan")
        if not clamscan:
            raise ScannerUnavailable()
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(data)
            tmp.flush()
            try:
                result = subprocess.run([clamscan, "--no-summary", "--infected", tmp.name], capture_output=True, timeout=60)
            except subprocess.TimeoutExpired as error:
                raise ScannerError("scan_timed_out", "This file took too long to scan.") from error
        # clamscan's own exit codes: 0 clean, 1 a virus was found, 2 the scanner itself could not complete.
        if result.returncode == 1:
            raise Infected()
        if result.returncode not in (0, 1):
            raise ScannerError("scan_failed", "This file could not be scanned.")


_override: MalwareScanner | None = None


@contextmanager
def use_scanner(scanner: MalwareScanner):
    """Swaps in a fake scanner for the duration of a `with` block, so a test can prove the
    verify/quarantine/audit machinery end to end without a real ClamAV install - the same shape
    `apps/media/storage/registry.py: use_storage` and `apps/media/transcoding.py: use_transcoder` already use."""
    global _override
    previous, _override = _override, scanner
    try:
        yield scanner
    finally:
        _override = previous


def get_scanner() -> MalwareScanner:
    if _override is not None:
        return _override
    return ClamAvScanner()
