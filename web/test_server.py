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


# ── Multipart parser ───────────────────────────────────────────────────────

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
        form = self._parse([("index", "2", None), ("button", "3", None)])
        self.assertEqual(form.getvalue("index"), "2")
        self.assertEqual(form.getvalue("button"), "3")


# ── Filename sanitizer ─────────────────────────────────────────────────────

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
        name, err = server._sanitize_filename("a" * 251 + ".mp4")
        self.assertIsNone(err)

    def test_256_bytes_rejected(self):
        name, err = server._sanitize_filename("a" * 252 + ".mp4")
        self.assertIsNone(name)
        self.assertIsNotNone(err)


# ── Config I/O ──────────────────────────────────────────────────────────────

class TestMediaConfig(unittest.TestCase):

    def setUp(self):
        self._patch = _ServerPatch()
        self._patch.__enter__()

    def tearDown(self):
        self._patch.__exit__()

    def test_load_media_empty_when_no_file(self):
        media = server.load_media()
        self.assertEqual(media, [])

    def test_load_media_bad_json_returns_empty(self):
        server.CONFIG_PATH.write_text("not json {{{")
        media = server.load_media()
        self.assertEqual(media, [])

    def test_save_and_load_roundtrip(self):
        media = [
            {"file": "intro.mp4", "button": 1},
            {"file": "landscape.jpg", "button": None},
        ]
        server.save_media(media)
        loaded = server.load_media()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["file"], "intro.mp4")
        self.assertEqual(loaded[0]["button"], 1)
        self.assertIsNone(loaded[1]["button"])

    def test_save_media_caps_at_max(self):
        media = [{"file": f"f{i}.mp4", "button": None} for i in range(15)]
        server.save_media(media)
        loaded = server.load_media()
        self.assertEqual(len(loaded), server.MAX_MEDIA)

    def test_load_media_validates_button_range(self):
        server.save_media([{"file": "clip.mp4", "button": 99}])
        loaded = server.load_media()
        self.assertIsNone(loaded[0]["button"])

    def test_assigned_buttons(self):
        media = [
            {"file": "a.mp4", "button": 1},
            {"file": "b.mp4", "button": 3},
            {"file": "c.jpg", "button": None},
        ]
        assigned = server._assigned_buttons(media)
        self.assertEqual(assigned, {1, 3})


# ── Migration ──────────────────────────────────────────────────────────────

class TestMigration(unittest.TestCase):

    def setUp(self):
        self._patch = _ServerPatch()
        self._patch.__enter__()

    def tearDown(self):
        self._patch.__exit__()

    def test_migrate_v1_slot_videos(self):
        v1 = {
            "1": {"gpio": 4, "pin": 7, "video": "intro.mp4"},
            "2": {"gpio": 17, "pin": 11, "video": None},
            "3": {"gpio": 22, "pin": 15, "video": "demo.mp4"},
        }
        server.CONFIG_PATH.write_text(json.dumps(v1))
        media = server.load_media()
        files = {m["file"] for m in media}
        self.assertIn("intro.mp4", files)
        self.assertIn("demo.mp4", files)
        # Null videos should not be migrated
        self.assertEqual(len(media), 2)

    def test_migrate_v1_splash_to_kiosk(self):
        v1 = {
            "1": {"gpio": 4, "pin": 7, "video": None},
            "splash": {"image": "bg.jpg", "video": "loop.mp4"},
        }
        server.CONFIG_PATH.write_text(json.dumps(v1))
        media = server.load_media()
        kiosk = [m for m in media if m["button"] is None]
        self.assertEqual(len(kiosk), 2)
        files = {m["file"] for m in kiosk}
        self.assertIn("bg.jpg", files)
        self.assertIn("loop.mp4", files)

    def test_migrate_v1_old_string_format(self):
        v1 = {"1": "old_video.mp4"}
        server.CONFIG_PATH.write_text(json.dumps(v1))
        media = server.load_media()
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["file"], "old_video.mp4")
        self.assertEqual(media[0]["button"], 1)

    def test_migrate_writes_v2_to_disk(self):
        v1 = {"1": {"gpio": 4, "pin": 7, "video": "intro.mp4"}}
        server.CONFIG_PATH.write_text(json.dumps(v1))
        server.load_media()
        raw = json.loads(server.CONFIG_PATH.read_text())
        self.assertEqual(raw["version"], 2)
        self.assertIn("media", raw)

    def test_v2_config_not_re_migrated(self):
        v2 = {
            "version": 2,
            "media": [{"file": "a.mp4", "button": 1}],
            "buttons": {"1": {"gpio": 4, "pin": 7}},
        }
        server.CONFIG_PATH.write_text(json.dumps(v2))
        media = server.load_media()
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["file"], "a.mp4")

    def test_migrate_mixed_slots_and_splash(self):
        v1 = {
            "1": {"gpio": 4, "pin": 7, "video": "intro.mp4"},
            "2": {"gpio": 17, "pin": 11, "video": "safety.mp4"},
            "splash": {"image": "bg.jpg", "video": None},
        }
        server.CONFIG_PATH.write_text(json.dumps(v1))
        media = server.load_media()
        button_media = [m for m in media if m["button"] is not None]
        kiosk_media = [m for m in media if m["button"] is None]
        self.assertEqual(len(button_media), 2)
        self.assertEqual(len(kiosk_media), 1)
        self.assertEqual(kiosk_media[0]["file"], "bg.jpg")


# ── HTML escaping / XSS ────────────────────────────────────────────────────

class TestHTMLEscaping(unittest.TestCase):

    def setUp(self):
        self._patch = _ServerPatch()
        self._patch.__enter__()

    def tearDown(self):
        self._patch.__exit__()

    def test_render_page_escapes_message(self):
        page = server.render_page(message='<script>alert("xss")</script>')
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_render_page_escapes_error(self):
        page = server.render_page(error='<img onerror="alert(1)">')
        self.assertNotIn('onerror="alert(1)"', page)
        self.assertIn("&lt;img", page)

    def test_render_page_escapes_media_filename(self):
        server.save_media([{"file": '<script>alert("xss")</script>.mp4', "button": None}])
        page = server.render_page()
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)

    def test_render_page_escapes_button_assigned_filename(self):
        server.save_media([{"file": '<img src=x>.mp4', "button": 1}])
        page = server.render_page()
        self.assertNotIn('<img src=x>', page)
        self.assertIn("&lt;img", page)


# ── _file_info_html ────────────────────────────────────────────────────────

class TestFileInfoHtml(unittest.TestCase):

    def setUp(self):
        self._patch = _ServerPatch()
        self._patch.__enter__()

    def tearDown(self):
        self._patch.__exit__()

    def test_existing_file_shows_size(self):
        (server.VIDEO_DIR / "clip.mp4").write_bytes(b"x" * 2048)
        result = server._file_info_html("clip.mp4")
        self.assertIn("MB", result)

    def test_missing_file_shows_warning(self):
        result = server._file_info_html("nonexistent.mp4")
        self.assertIn("missing", result)

    def test_no_filename_returns_empty(self):
        result = server._file_info_html(None)
        self.assertEqual(result, "")

    def test_empty_filename_returns_empty(self):
        result = server._file_info_html("")
        self.assertEqual(result, "")


# ── HTTP integration ───────────────────────────────────────────────────────

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
        server.CONFIG_PATH.unlink(missing_ok=True)

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _upload(self, fields):
        body, ct = _multipart(fields)
        conn = self._conn()
        conn.request("POST", "/upload", body=body,
                     headers={"Content-Type": ct, "Content-Length": str(len(body))})
        return conn.getresponse()

    def _assign(self, params):
        body = "&".join(f"{k}={v}" for k, v in params.items()).encode()
        conn = self._conn()
        conn.request("POST", "/assign", body=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Content-Length": str(len(body))})
        return conn.getresponse()

    def _delete(self, params):
        body = "&".join(f"{k}={v}" for k, v in params.items()).encode()
        conn = self._conn()
        conn.request("POST", "/delete", body=body,
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

    # ── Upload ───────────────────────────────────────────────────────────────

    def test_upload_video(self):
        resp = self._upload([("file", b"fakevideo", "intro.mp4")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        media = server.load_media()
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["file"], "intro.mp4")
        self.assertIsNone(media[0]["button"])
        self.assertTrue((server.VIDEO_DIR / "intro.mp4").exists())

    def test_upload_with_button_assignment(self):
        resp = self._upload([("button", "3", None), ("file", b"data", "clip.mp4")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        media = server.load_media()
        self.assertEqual(media[0]["button"], 3)

    def test_upload_image(self):
        resp = self._upload([("file", b"imgdata", "photo.jpg")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        media = server.load_media()
        self.assertEqual(media[0]["file"], "photo.jpg")

    def test_upload_invalid_extension_400(self):
        resp = self._upload([("file", b"data", "virus.exe")])
        body = resp.read()
        self.assertEqual(resp.status, 400)
        self.assertIn(b"Unsupported", body)

    def test_upload_path_traversal_saved_safely(self):
        """../../evil.mp4 must be stored as evil.mp4, not outside VIDEO_DIR."""
        resp = self._upload([("file", b"data", "../../evil.mp4")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        media = server.load_media()
        self.assertEqual(media[0]["file"], "evil.mp4")
        self.assertTrue((server.VIDEO_DIR / "evil.mp4").exists())
        self.assertFalse((server.VIDEO_DIR.parent / "evil.mp4").exists())

    def test_upload_utf8_filename(self):
        resp = self._upload([("file", b"data", "vidéo.mp4")])
        resp.read()
        self.assertIn(resp.status, (200, 303))
        media = server.load_media()
        self.assertTrue(media[0]["file"].endswith(".mp4"))

    def test_upload_null_byte_filename_rejected(self):
        resp = self._upload([("file", b"data", "evil\x00.mp4")])
        resp.read()
        self.assertEqual(resp.status, 400)

    def test_upload_very_long_filename_rejected(self):
        long_name = "a" * 300 + ".mp4"
        resp = self._upload([("file", b"data", long_name)])
        body = resp.read()
        self.assertEqual(resp.status, 400)
        self.assertIn(b"long", body.lower())

    def test_upload_no_file_field_400(self):
        resp = self._upload([("button", "1", None)])
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

    def test_upload_duplicate_button_400(self):
        self._upload([("button", "1", None), ("file", b"data", "a.mp4")]).read()
        resp = self._upload([("button", "1", None), ("file", b"data", "b.mp4")])
        body = resp.read()
        self.assertEqual(resp.status, 400)
        self.assertIn(b"already assigned", body)

    def test_upload_library_full_400(self):
        for i in range(server.MAX_MEDIA):
            self._upload([("file", b"data", f"f{i}.mp4")]).read()
        resp = self._upload([("file", b"data", "extra.mp4")])
        body = resp.read()
        self.assertEqual(resp.status, 400)
        self.assertIn(b"full", body.lower())

    def test_post_unknown_path_404(self):
        body = b""
        conn = self._conn()
        conn.request("POST", "/unknown", body=body, headers={"Content-Length": "0"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 404)

    # ── Size limit ──────────────────────────────────────────────────────────

    def test_upload_over_size_limit_413(self):
        server.MAX_UPLOAD_BYTES = 10
        try:
            body, ct = _multipart([("file", b"x" * 100, "clip.mp4")])
            conn = self._conn()
            conn.request("POST", "/upload", body=body,
                         headers={"Content-Type": ct, "Content-Length": str(len(body))})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 413)
        finally:
            server.MAX_UPLOAD_BYTES = self._orig_limit

    # ── Disk full simulation ────────────────────────────────────────────────

    def test_upload_disk_full_returns_507(self):
        def _raise_nospc(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("shutil.copyfileobj", side_effect=_raise_nospc):
            resp = self._upload([("file", b"data", "clip.mp4")])
            body = resp.read()
        self.assertEqual(resp.status, 507)
        self.assertIn(b"space", body.lower())

    def test_upload_disk_full_no_partial_file(self):
        def _raise_nospc(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("shutil.copyfileobj", side_effect=_raise_nospc):
            self._upload([("file", b"data", "clip.mp4")]).read()

        self.assertFalse((server.VIDEO_DIR / "clip.mp4").exists())

    def test_upload_disk_full_config_not_updated(self):
        def _raise_nospc(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("shutil.copyfileobj", side_effect=_raise_nospc):
            self._upload([("file", b"data", "clip.mp4")]).read()

        self.assertEqual(server.load_media(), [])

    # ── Assign ──────────────────────────────────────────────────────────────

    def test_assign_button(self):
        self._upload([("file", b"data", "clip.mp4")]).read()
        resp = self._assign({"index": "0", "button": "2"})
        resp.read()
        self.assertIn(resp.status, (200, 303))
        media = server.load_media()
        self.assertEqual(media[0]["button"], 2)

    def test_assign_unassign_button(self):
        self._upload([("button", "1", None), ("file", b"data", "clip.mp4")]).read()
        self.assertEqual(server.load_media()[0]["button"], 1)
        resp = self._assign({"index": "0", "button": ""})
        resp.read()
        self.assertIn(resp.status, (200, 303))
        self.assertIsNone(server.load_media()[0]["button"])

    def test_assign_duplicate_button_400(self):
        self._upload([("button", "1", None), ("file", b"data", "a.mp4")]).read()
        self._upload([("file", b"data", "b.mp4")]).read()
        resp = self._assign({"index": "1", "button": "1"})
        body = resp.read()
        self.assertEqual(resp.status, 400)
        self.assertIn(b"already assigned", body)

    def test_assign_invalid_index_400(self):
        resp = self._assign({"index": "99", "button": "1"})
        resp.read()
        self.assertEqual(resp.status, 400)

    def test_assign_reassign_same_button_ok(self):
        """Reassigning the same button to the same media entry should succeed."""
        self._upload([("button", "1", None), ("file", b"data", "a.mp4")]).read()
        resp = self._assign({"index": "0", "button": "1"})
        resp.read()
        self.assertIn(resp.status, (200, 303))

    # ── Delete ──────────────────────────────────────────────────────────────

    def test_delete_removes_from_config_and_disk(self):
        self._upload([("file", b"data", "tmp.mp4")]).read()
        self.assertTrue((server.VIDEO_DIR / "tmp.mp4").exists())
        self.assertEqual(len(server.load_media()), 1)
        resp = self._delete({"index": "0"})
        resp.read()
        self.assertIn(resp.status, (200, 303))
        self.assertEqual(len(server.load_media()), 0)
        self.assertFalse((server.VIDEO_DIR / "tmp.mp4").exists())

    def test_delete_invalid_index_400(self):
        resp = self._delete({"index": "99"})
        resp.read()
        self.assertEqual(resp.status, 400)

    def test_delete_shifts_indices(self):
        self._upload([("file", b"data", "a.mp4")]).read()
        self._upload([("file", b"data", "b.mp4")]).read()
        self._upload([("file", b"data", "c.mp4")]).read()
        self._delete({"index": "0"}).read()
        media = server.load_media()
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0]["file"], "b.mp4")
        self.assertEqual(media[1]["file"], "c.mp4")


# ── Upload XSS via HTTP ───────────────────────────────────────────────────

class TestHTTPXss(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        server.VIDEO_DIR = Path(cls.tmpdir)
        server.CONFIG_PATH = Path(cls.tmpdir) / "config.json"

        cls.httpd = server.HTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _upload(self, fields):
        body, ct = _multipart(fields)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/upload", body=body,
                     headers={"Content-Type": ct, "Content-Length": str(len(body))})
        return conn.getresponse()

    def test_xss_filename_escaped_in_page(self):
        xss = '<script>alert(1)</script>.mp4'
        self._upload([("file", b"data", xss)]).read()

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        page = conn.getresponse().read().decode()
        self.assertNotIn("<script>alert", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
