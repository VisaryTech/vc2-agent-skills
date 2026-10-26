"""Offline archive downloads through the CLI, client and HTTPS transport."""

import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

from test_sbis import client, common, cli, http_transport


class Response(io.BytesIO):
    status = 200


class ArchiveTests(unittest.TestCase):
    url = "https://disk.sbis.ru/archive?private=secret"

    def setUp(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("attachment.txt", b"attachment")
            archive.writestr("attachment.txt.sgn", b"signature")
        self.zip_bytes = buffer.getvalue()
        self.pending = json.dumps({"error": {"code":
            "00000000-0000-0000-0000-1AA0000F1002", "message": "private"}}).encode()

    def download(self, root, response, document=None, identifier="doc"):
        c = client(document if document is not None else {
            "Идентификатор": identifier, "СсылкаНаАрхив": self.url})
        transport = http_transport.HttpTransport()
        with patch.object(c.transport, "get", side_effect=transport.get) as getter:
            with patch.object(transport.opener, "open", side_effect=[response]) as opened:
                result = c.download(identifier, root, archive=True)
                getter.assert_called_once_with(self.url, archive=True)
                opened.assert_called_once_with(self.url, timeout=60)
                return result

    def test_cli_dispatch(self):
        args = cli.parser().parse_args(["download", "doc", "--archive", "--output", "/tmp/export"])
        c = Mock()
        cli.run(args, c)
        c.download.assert_called_once_with("doc", "/tmp/export", archive=True)
        self.assertFalse(cli.parser().parse_args([
            "download", "doc", "--output", "/tmp/export"]).archive)

    def test_archive_bytes_permissions_and_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "output"
            root.mkdir()
            outside = Path(directory) / "outside"
            (root / "doc.zip").symlink_to(outside)
            (root / "doc_1.zip").write_bytes(b"keep")
            result = self.download(root, Response(self.zip_bytes), identifier="../../doc")
            path = root / "doc_2.zip"
            self.assertEqual(result, {"document_id": "../../doc", "files": [
                {"kind": "archive", "path": str(path), "bytes": len(self.zip_bytes)}]})
            self.assertEqual(path.read_bytes(), self.zip_bytes)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(outside.exists())
            self.assertEqual((root / "doc_1.zip").read_bytes(), b"keep")
            self.assertEqual(len(list(root.iterdir())), 3)

    def test_missing_link_and_partial_data_do_not_download(self):
        documents = [({"Идентификатор": "doc"}, "archive_unavailable"),
                     ({"Идентификатор": "doc", "СсылкаНаАрхив": 42}, "archive_unavailable"),
                     ({"Идентификатор": "doc", "ЧастичныеДанные": "Да",
                       "СсылкаНаАрхив": self.url}, "partial_data")]
        for document, code in documents:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                c = client(document)
                with self.assertRaises(common.ClientError) as ctx:
                    c.download("doc", directory, archive=True)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(c.transport.downloads, [])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_pending_invalid_http_and_oversize_responses_leave_no_files(self):
        cases = [(200, self.pending, "archive_not_ready"),
                 (503, self.pending, "archive_not_ready"),
                 (200, b'<html>private</html>', "invalid_response"),
                 (200, b'PK\x03\x04truncated', "invalid_response"),
                 (200, b'{"error": {"code": "other", "message": "private"}}', "invalid_response"),
                 (403, b"private", "access_denied"),
                 (500, b"private", "http_error"),
                 (200, b"x" * 1025, "invalid_input"),
                 (503, b"x" * 1025, "invalid_input")]
        for status, body, code in cases:
            with self.subTest(status=status, code=code), tempfile.TemporaryDirectory() as directory:
                response = (Response(body) if status == 200 else urllib.error.HTTPError(
                    self.url, status, "private", {}, io.BytesIO(body)))
                with patch.object(http_transport, "MAX_FILE", 1024):
                    with self.assertRaises(common.ClientError) as ctx:
                        self.download(directory, response)
                self.assertEqual(ctx.exception.code, code)
                self.assertNotIn("private", str(ctx.exception))
                if code == "archive_not_ready":
                    self.assertIn("5–10", str(ctx.exception))
                self.assertTrue(response.closed)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_archive_redirect_validation(self):
        for destination in ("https://evil.test/archive", "http://disk.sbis.ru/archive"):
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as directory:
                error = urllib.error.HTTPError(self.url, 302, "redirect",
                                               {"Location": destination}, io.BytesIO())
                with self.assertRaises(common.ClientError) as ctx:
                    self.download(directory, error)
                self.assertEqual(ctx.exception.code, "unsafe_download")
                self.assertTrue(error.closed)

    def test_non_json_preparation_text_is_not_treated_as_pending(self):
        for body in (b"00000000-0000-0000-0000-1AA0000F1002",
                     b'"00000000-0000-0000-0000-1AA0000F1002"'):
            http_transport.check_archive_pending(body)


if __name__ == "__main__":
    unittest.main()
