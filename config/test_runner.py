import shutil
import tempfile

from django.conf import settings
from django.test.runner import DiscoverRunner


class IsolatedMediaRunner(DiscoverRunner):
    """Schreibt Upload-Dateien der Tests in ein Wegwerf-Verzeichnis statt nach ``media/``."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._original_media_root = settings.MEDIA_ROOT
        self._media_directory = tempfile.mkdtemp(prefix="ausgaben-test-media-")
        settings.MEDIA_ROOT = self._media_directory

    def teardown_test_environment(self, **kwargs):
        settings.MEDIA_ROOT = self._original_media_root
        shutil.rmtree(self._media_directory, ignore_errors=True)
        super().teardown_test_environment(**kwargs)
