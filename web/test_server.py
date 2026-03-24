"""Tests for PiVideo web server."""

import errno
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import server  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────

BOUNDARY = "testboundary"


def _multipart(fields):
    """Build a multipart/form-data body.

    fields: list of (name, value, filename_or_None)
      value is bytes when filename is set, str for plain fields.
    Returns (body_bytes, content_type_string).
    """
    b = BOUNDARY.encode()
    parts = []
    for name, value, filename in fields:
        if filename is not None:
            header = (
                f'Content-Disposition: form-data; name="{name}";'
                f' filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream"
            ).encode("utf-8")
            data = value if isinstance(value, bytes) else value.encode("utf-8")
        else:
            header = f'Content-Disposition: form-data; name="{name}"'.encode("utf-8")
            data = value.encode("utf-8") if isinstance(value, str) else value
        parts.append(header + b"\r\n\r\n" + data)

    body = b"--" + b + b"\r\n"
    body += (b"\r\n--" + b + b"\r\n").join(parts)
    body += b"\r\n--" + b + b"--\r\n"
    return body, f"multipart/form-data; boundary={BOUNDARY}"


class _ServerPatch:
    """Context manager: redirect VIDEO_DIR and CONFIG_PATH to a temp directory."""

    def __enter__(self):
        self.tmpdir = tempfile.mkdtemp()
        self._vd = server.VIDEO_DIR
        self._cp = server.CONFIG_PATH
        server.VIDEO_DIR = Path(self.tmpdir)
        server.CONFIG_PATH = Path(self.tmpdir) / "config.json"
        return self

    def __exit__(self, *_):
        server.VIDEO_DIR = self._vd
        server.CONFIG_PATH = self._cp
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ── Multipart parser ─────────────────────────────────────────────────────────

class TestMultipartParser(unittest.TestCase):

    def _parse(self, fields):
        body, ct = _multipart(fields)
        return server._parse_multipart(io.BytesIO(body), ct, len(body))

    def test_text_field(self):
        form = self._parse([("slot", "3", None)])
        self.assertEqual(form.getvalue("slot"), "3")

    def test_missing_key_returns_default(self):
        form = self._parse([("slot", "1", None)])
        self.assertIsNone(form.getvalue("missing"))
        self.assertEqual(form.getvalue("missing", "fallback"), "fallback")

    def test_file_field(self):
        form = self._parse([
            ("slot", "1", None),
            ("file", b"fake video bytes", "clip.mp4"),
        ])
        field = form.get("file")
        self.assertIsNotNone(field)
        self.assertEqual(field.filename, "clip.mp4")
        self.assertEqual(field.file.read(), b"fake video bytes")

    def test_utf8_filename_preserved(self):
        form = self._parse([("file", b"data", "vidéo_\u65e5\u672c\u8a9e.mp4")])
        field = form.get("file")
        self.assertIsNotNone(field)
        self.assertTrue(field.filename.endswith(".mp4"))

    def test_path_traversal_raw_value(self):
        """Parser stores the raw filename; basename stripping happens in the handler."""
        form = self._parse([("file", b"data", "../../etc/passwd.mp4")])
        field = form.get("file")
        self.assertIsNotNone(field)
        self.assertIn("passwd.mp4", field.filename)

    def test_no_boundary_returns_empty(self):
        body, _ = _multipart([("slot", "1", None)])
        form = server._parse_multipart(io.BytesIO(body), "multipart/form-data", len(body))
        self.assertIsNone(form.getvalue("slot"))

    def test_part_without_name_is_skipped(self):
        b = BOUNDARY.encode()
        body = (
            b"--" + b + b"\r\n"
            b"Content-Disposition: form-data\r\n\r\nvalue\r\n"
            b"--" + b + b"--\r\n"
        )
        ct = f"multipart/form-data; boundary={BOUNDARY}"
        form = server._parse_multipart(io.BytesIO(body), ct, len(body))
        self.assertIsNone(form.getvalue("anything"))

    def test_empty_body_returns_empty(self):
        form = server._parse_multipart(io.BytesIO(b""), "multipart/form-data; boundary=x", 0)
        self.assertIsNone(form.getvalue("x"))

    def test_multiple_text_fields(self):
        form = self._parse([("slot", "2", None), ("splash", "image", None)])
        self.assertEqual(form.getvalue("slot"), "2")
        self.assertEqual(form.getvalue("splash"), "image")


# ── Filename sanitizer ───────────────────────────────────────────────────────

class TestSanitizeFilename(unittest.TestCase):

    def test_normal_filename(self):
        name, err = server._sanitize_filename("clip.mp4")
        self.assertEqual(name, "clip.mp4")
        self.assertIsNone(err)

    def test_strips_path_components(self):
        name, err = server._sanitize_filename("../../etc/passwd.mp4")
        self.assertEqual(name, "passwd.mp4")
        self.assertIsNone(err)

    def test_null_byte_rejected(self):
        name, err = server._sanitize_filename("evil\x00.mp4")
        self.assertIsNone(name)
        self.assertIsNotNone(err)

    def test_long_filename_rejected(self):
        name, err = server._sanitize_filename("a" * 300 + ".mp4")
        self.assertIsNone(name)
        self.assertIsNotNone(err)

    def test_empty_after_basename_rejected(self):
        name, err = server._sanitize_filename("")
        self.assertIsNone(name)
        self.assertIsNotNone(err)

    def test_utf8_filename_accepted(self):
        name, err = server._sanitize_filename("vidéo.mp4")
        self.assertEqual(name, "vidéo.mp4")
        self.assertIsNone(err)

    def test_exactly_255_bytes_accepted(self):
        # 251 'a' chars + '.mp4' = 255 bytes (all ASCII)
        name, err = server._sanitize_filename("a" * 251 + ".mp4")
        self.assertIsNone(err)

    def test_256_bytes_rejected(self):
        name, err = server._sanitize_filename("a" * 252 + ".mp4")
        self.assertIsNone(name)
        self.assertIsNotNone(err)


# ── Config I/O ───────────────────────────────────────────────────────────────

class TestConfigFunctions(unittest.TestCase):

    def setUp(self):
        self._patch = _ServerPatch()
        self._patch.__enter__()

    def tearDown(self):
        self._patch.__exit__()

    def test_load_config_missing_file_gives_defaults(self):
        config = server.load_config()
        self.assertEqual(set(config.keys()), set(range(1, 8)))
        for slot in config.values():
            self.assertIsNone(slot["video"])

    def test_load_config_bad_json_gives_defaults(self):
        server.CONFIG_PATH.write_text("not json {{{")
        config = server.load_config()
        self.assertEqual(set(config.keys()), set(range(1, 8)))

    def test_save_and_load_roundtrip(self):
        config = server.load_config()
        config[1]["video"] = "button1.mp4"
        config[3]["video"] = "button3.mp4"
        server.save_config(config)
        loaded = server.load_config()
        self.assertEqual(loaded[1]["video"], "button1.mp4")
        self.assertEqual(loaded[3]["video"], "button3.mp4")
        self.assertIsNone(loaded[2]["video"])

    def test_load_config_migrates_old_string_format(self):
        server.CONFIG_PATH.write_text(json.dumps({"1": "old_video.mp4"}))
        config = server.load_config()
        self.assertEqual(config[1]["video"], "old_video.mp4")

    def test_load_splash_defaults_when_absent(self):
        splash = server.load_splash()
        self.assertIsNone(splash["image"])
        self.assertIsNone(splash["video"])

    def test_save_and_load_splash_roundtrip(self):
        server.save_splash({"image": "idle.jpg", "video": None})
        splash = server.load_splash()
        self.assertEqual(splash["image"], "idle.jpg")
        self.assertIsNone(splash["video"])

    def test_save_splash_preserves_slot_config(self):
        config = server.load_config()
        config[2]["video"] = "clip.mp4"
        server.save_config(config)
        server.save_splash({"image": "bg.jpg", "video": None})
        self.assertEqual(server.load_config()[2]["video"], "clip.mp4")


# ── HTTP integration ─────────────────────────────────────────────────────────

class TestHTTPServer(unittest.TestCase):
    """End-to-end tests against a live server instance."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        server.VIDEO_DIR = Path(cls.tmpdir)
        server.CONFIG_PATH = Path(cls.tmpdir) / "config.json"
        cls._orig_limit = server.MAX_UPLOAD_BYTES

        cls.httpd = server.HTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        server.MAX_UPLOAD_BYTES = cls._orig_limit
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        # Reset config before each test
        server.CONFIG_PATH.unlink(missing_ok=True)

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _upload(self, fields):
        body, ct = _multipart(fields)
        conn = self._conn()
        conn.request("POST", "/upload", body=body,
                     headers={"Content-Type": ct, "Content-Length": str(len(body))})
        return conn.getresponse()

    def _clear(self, params):
        body = "&".join(f"{k}={v}" for k, v in params.items()).encode()
        conn = self._conn()
        conn.request("POST", "/clear", body=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Content-Length": str(len(body))})
        return conn.getresponse()

    # ── GET ──────────────────────────────────────────────────────────────────

    def test_get_root_200(self):
        conn = self._conn()
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"PiVideo", resp.read())

    def test_get_unknown_404(self):
        conn = self._conn()
        conn.request("GET", "/nonexistent")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        resp.read()

    # ── Valid uploads ─────────────────────────────────────────────────────────

    def test_upload_video_to_slot(self):
        resp = self._upload([("slot", "1", None), ("file", b"fakevideo", "intro.mp4")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        self.assertEqual(server.load_config()[1]["video"], "intro.mp4")
        self.assertTrue((server.VIDEO_DIR / "intro.mp4").exists())

    def test_upload_splash_video(self):
        resp = self._upload([("splash", "video", None), ("file", b"data", "loop.mp4")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        self.assertEqual(server.load_splash()["video"], "loop.mp4")

    def test_upload_splash_image(self):
        resp = self._upload([("splash", "image", None), ("file", b"data", "bg.jpg")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        self.assertEqual(server.load_splash()["image"], "bg.jpg")

    # ── Extension checks ─────────────────────────────────────────────────────

    def test_upload_invalid_extension_400(self):
        resp = self._upload([("slot", "2", None), ("file", b"data", "virus.exe")])
        body = resp.read()
        self.assertEqual(resp.status, 400)
        self.assertIn(b"Unsupported", body)

    def test_upload_splash_video_as_image_400(self):
        resp = self._upload([("splash", "image", None), ("file", b"data", "bg.mp4")])
        body = resp.read()
        self.assertEqual(resp.status, 400)

    # ── Filename fuzzing ─────────────────────────────────────────────────────

    def test_upload_path_traversal_saved_safely(self):
        """../../evil.mp4 must be stored as evil.mp4, not outside VIDEO_DIR."""
        resp = self._upload([("slot", "1", None), ("file", b"data", "../../evil.mp4")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        saved = server.load_config()[1]["video"]
        self.assertEqual(saved, "evil.mp4")
        self.assertTrue((server.VIDEO_DIR / "evil.mp4").exists())
        self.assertFalse((server.VIDEO_DIR.parent / "evil.mp4").exists())

    def test_upload_utf8_filename(self):
        resp = self._upload([("slot", "3", None), ("file", b"data", "vidéo.mp4")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        saved = server.load_config()[3]["video"]
        self.assertIsNotNone(saved)
        self.assertTrue(saved.endswith(".mp4"))

    def test_upload_null_byte_filename_rejected(self):
        resp = self._upload([("slot", "5", None), ("file", b"data", "evil\x00.mp4")])
        body = resp.read()
        self.assertEqual(resp.status, 400)

    def test_upload_very_long_filename_rejected(self):
        long_name = "a" * 300 + ".mp4"
        resp = self._upload([("slot", "4", None), ("file", b"data", long_name)])
        body = resp.read()
        self.assertEqual(resp.status, 400)
        self.assertIn(b"long", body.lower())

    # ── Missing / invalid fields ─────────────────────────────────────────────

    def test_upload_no_file_field_400(self):
        resp = self._upload([("slot", "1", None)])
        resp.read()
        self.assertEqual(resp.status, 400)

    def test_upload_invalid_slot_400(self):
        resp = self._upload([("slot", "99", None), ("file", b"data", "clip.mp4")])
        resp.read()
        self.assertEqual(resp.status, 400)

    def test_upload_non_multipart_400(self):
        body = b"not multipart"
        conn = self._conn()
        conn.request("POST", "/upload", body=body,
                     headers={"Content-Type": "text/plain", "Content-Length": str(len(body))})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 400)

    def test_post_unknown_path_404(self):
        body = b""
        conn = self._conn()
        conn.request("POST", "/unknown", body=body, headers={"Content-Length": "0"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 404)

    # ── Size limit ───────────────────────────────────────────────────────────

    def test_upload_over_size_limit_413(self):
        server.MAX_UPLOAD_BYTES = 10  # 10 bytes — tiny limit for testing
        try:
            body, ct = _multipart([("slot", "1", None), ("file", b"x" * 100, "clip.mp4")])
            conn = self._conn()
            conn.request("POST", "/upload", body=body,
                         headers={"Content-Type": ct, "Content-Length": str(len(body))})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 413)
        finally:
            server.MAX_UPLOAD_BYTES = self._orig_limit

    # ── Disk full simulation ─────────────────────────────────────────────────

    def test_upload_disk_full_returns_507(self):
        def _raise_nospc(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("shutil.copyfileobj", side_effect=_raise_nospc):
            resp = self._upload([("slot", "1", None), ("file", b"data", "clip.mp4")])
            body = resp.read()
        self.assertEqual(resp.status, 507)
        self.assertIn(b"space", body.lower())

    def test_upload_disk_full_no_partial_file(self):
        """A failed write must not leave a partial file on disk."""
        def _raise_nospc(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("shutil.copyfileobj", side_effect=_raise_nospc):
            self._upload([("slot", "1", None), ("file", b"data", "clip.mp4")]).read()

        self.assertFalse((server.VIDEO_DIR / "clip.mp4").exists())

    def test_upload_disk_full_config_not_updated(self):
        """Config must not be updated when the file write fails."""
        def _raise_nospc(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("shutil.copyfileobj", side_effect=_raise_nospc):
            self._upload([("slot", "2", None), ("file", b"data", "clip.mp4")]).read()

        self.assertIsNone(server.load_config()[2]["video"])

    def test_upload_splash_disk_full_returns_507(self):
        def _raise_nospc(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("shutil.copyfileobj", side_effect=_raise_nospc):
            resp = self._upload([("splash", "video", None), ("file", b"data", "loop.mp4")])
            body = resp.read()
        self.assertEqual(resp.status, 507)

    # ── Clear ────────────────────────────────────────────────────────────────

    def test_clear_slot(self):
        self._upload([("slot", "1", None), ("file", b"data", "tmp.mp4")]).read()
        self.assertEqual(server.load_config()[1]["video"], "tmp.mp4")
        resp = self._clear({"slot": "1"})
        resp.read()
        self.assertIn(resp.status, (200, 303))
        self.assertIsNone(server.load_config()[1]["video"])

    def test_clear_splash_image(self):
        self._upload([("splash", "image", None), ("file", b"data", "bg.jpg")]).read()
        self.assertEqual(server.load_splash()["image"], "bg.jpg")
        resp = self._clear({"splash": "image"})
        resp.read()
        self.assertIn(resp.status, (200, 303))
        self.assertIsNone(server.load_splash()["image"])

    def test_clear_invalid_slot_400(self):
        resp = self._clear({"slot": "99"})
        resp.read()
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
