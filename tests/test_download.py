"""Downloader recovery tests using small in-memory HTTP responses, never the real corpus."""
import hashlib
import io
import urllib.error
from unittest.mock import patch

from tests.test_core import TemporaryCase
from privacy_pilot.common import PilotError, read_json
from privacy_pilot.source import download_webis


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(body))}


class DownloadTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.content = b"small-test-archive-content"
        self.destination = self.root / "corpus.zip"
        self.partial = self.root / "corpus.zip.part"
        self.md5_patch = patch("privacy_pilot.source.WEBIS_MD5", hashlib.md5(self.content).hexdigest())
        self.md5_patch.start()
        self.addCleanup(self.md5_patch.stop)

    def test_new_download_checksums_and_manifest(self):
        with patch("privacy_pilot.source.urllib.request.urlopen", return_value=Response(self.content)):
            info = download_webis(self.destination)
        self.assertEqual(self.destination.read_bytes(), self.content)
        self.assertFalse(self.partial.exists())
        self.assertEqual(info["sha256"], hashlib.sha256(self.content).hexdigest())
        self.assertEqual(read_json(self.root / "corpus.zip.source.json")["bytes"], len(self.content))

    def test_resume_exact_offset(self):
        self.partial.write_bytes(self.content[:8])
        def opened(request, timeout):
            self.assertEqual(request.get_header("Range"), "bytes=8-")
            return Response(self.content[8:], 206, {"Content-Range": f"bytes 8-{len(self.content)-1}/{len(self.content)}",
                                                   "Content-Length": str(len(self.content) - 8)})
        with patch("privacy_pilot.source.urllib.request.urlopen", side_effect=opened):
            download_webis(self.destination)
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_complete_zip_or_partial_requires_no_network(self):
        self.partial.write_bytes(self.content)
        with patch("privacy_pilot.source.urllib.request.urlopen") as network:
            self.assertEqual(download_webis(self.destination)["status"], "recovered_complete")
            self.assertEqual(download_webis(self.destination)["status"], "already_complete")
            network.assert_not_called()

    def test_ignored_range_preserves_backup_instead_of_appending(self):
        self.partial.write_bytes(b"original-fragment")
        with patch("privacy_pilot.source.urllib.request.urlopen", return_value=Response(self.content)):
            download_webis(self.destination)
        backups = list(self.root.glob("*.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"original-fragment")
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_wrong_range_preserves_original(self):
        self.partial.write_bytes(self.content[:8])
        invalid = Response(self.content[9:], 206, {"Content-Range": f"bytes 9-{len(self.content)-1}/{len(self.content)}"})
        with patch("privacy_pilot.source.urllib.request.urlopen", return_value=invalid), self.assertRaises(PilotError):
            download_webis(self.destination)
        self.assertEqual(self.partial.read_bytes(), self.content[:8])

    def test_truncated_response_preserves_downloaded_bytes(self):
        early = Response(self.content[:8], 200, {"Content-Length": str(len(self.content))})
        with patch("privacy_pilot.source.urllib.request.urlopen", return_value=early), self.assertRaisesRegex(PilotError, "ended early"):
            download_webis(self.destination)
        self.assertEqual(self.partial.read_bytes(), self.content[:8])
        self.assertFalse(self.destination.exists())

    def test_restart_backs_up_and_sends_no_range(self):
        self.partial.write_bytes(b"bad-prefix")
        def opened(request, timeout):
            self.assertIsNone(request.get_header("Range"))
            return Response(self.content)
        with patch("privacy_pilot.source.urllib.request.urlopen", side_effect=opened):
            download_webis(self.destination, restart=True)
        self.assertEqual(next(self.root.glob("*.backup-*")).read_bytes(), b"bad-prefix")
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_corrupt_complete_file_is_not_overwritten(self):
        self.destination.write_bytes(b"corrupt")
        with patch("privacy_pilot.source.urllib.request.urlopen") as network, self.assertRaises(PilotError):
            download_webis(self.destination, restart=True)
        network.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), b"corrupt")

    def test_bad_checksum_keeps_partial_and_suggests_restart(self):
        with patch("privacy_pilot.source.urllib.request.urlopen", return_value=Response(b"wrong-content")), \
             self.assertRaisesRegex(PilotError, "--restart"):
            download_webis(self.destination)
        self.assertEqual(self.partial.read_bytes(), b"wrong-content")

    def test_range_not_satisfiable_suggests_restart(self):
        self.partial.write_bytes(b"prefix")
        error = urllib.error.HTTPError("https://example.com", 416, "invalid range", {}, None)
        with patch("privacy_pilot.source.urllib.request.urlopen", side_effect=error), \
             self.assertRaisesRegex(PilotError, "--restart"):
            download_webis(self.destination)
        self.assertEqual(self.partial.read_bytes(), b"prefix")
