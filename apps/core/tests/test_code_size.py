from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

MAX_LINES = 700
SKIP_DIRS = {".venv", "__pycache__", "migrations", "staticfiles"}


class CodeSizeTests(SimpleTestCase):
    """Keeps every file small enough to read in one sitting.

    When a file nears the limit, split it by feature or responsibility into a
    folder rather than raising the limit. Generated migrations are exempt.
    """

    def test_no_python_file_is_longer_than_the_limit(self):
        too_long = []
        for path in Path(settings.BASE_DIR).rglob("*.py"):
            if SKIP_DIRS & set(path.parts):
                continue
            lines = len(path.read_text(encoding="utf-8").splitlines())
            if lines > MAX_LINES:
                too_long.append(f"{path.relative_to(settings.BASE_DIR)}: {lines} lines")
        self.assertEqual(too_long, [], f"Split these files (limit {MAX_LINES} lines).")
