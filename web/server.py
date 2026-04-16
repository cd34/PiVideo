#!/usr/bin/env python3
"""PiVideo web UI — manage media library and button assignments."""

import errno
import html
import io
import json
import os
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


# ── Multipart parser (replaces removed cgi module) ─────────────────────────

class _Field:
    """One field from a multipart/form-data submission."""
    __slots__ = ("name", "filename", "file", "_value")

    def __init__(self, name, *, filename=None, file=None, value=None):
        self.name     = name
        self.filename = filename
        self.file     = file
        self._value   = value


class _Form:
    """Parsed multipart/form-data, mimicking the cgi.FieldStorage interface."""

    def __init__(self, fields):
        self._fields = fields

    def getvalue(self, name, default=None):
        f = self._fields.get(name)
        return f._value if f is not None else default

    def get(self, name):
        return self._fields.get(name)


def _parse_multipart(rfile, content_type, content_length):
    boundary = None
    for token in content_type.split(";"):
        token = token.strip()
        if token.lower().startswith("boundary="):
            boundary = token[9:].strip('"')
            break
    if not boundary:
        return _Form({})

    data = rfile.read(content_length)
    b = boundary.encode("ascii")
    fields = {}

    start = b"--" + b + b"\r\n"
    if not data.startswith(start):
        return _Form({})

    for part in data[len(start):].split(b"\r\n--" + b):
        if part.startswith(b"--"):
            break
        if part.startswith(b"\r\n"):
            part = part[2:]

        hdr_end = part.find(b"\r\n\r\n")
        if hdr_end == -1:
            continue

        hdr_text = part[:hdr_end].decode("utf-8", errors="replace")
        body     = part[hdr_end + 4:]

        name = filename = None
        for line in hdr_text.split("\r\n"):
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            if key.strip().lower() == "content-disposition":
                for token in val.split(";"):
                    token = token.strip()
                    if token.startswith("name="):
                        name = token[5:].strip('"')
                    elif token.startswith("filename="):
                        filename = token[9:].strip('"')

        if not name:
            continue

        if filename:
            fields[name] = _Field(name, filename=filename, file=io.BytesIO(body))
        else:
            fields[name] = _Field(name, value=body.decode("utf-8", errors="replace"))

    return _Form(fields)


# ── Upload limits & filename validation ────────────────────────────────────

# Maximum upload body size in bytes. Configurable via MAX_UPLOAD_MB env var.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "4096")) * 1024 * 1024


def _sanitize_filename(raw):
    """Strip path components and reject dangerous filenames.

    Returns (filename, error_message_or_None).
    """
    name = os.path.basename(raw)
    if not name:
        return None, "No file selected."
    if "\x00" in name:
        return None, "Invalid filename."
    if len(name.encode("utf-8")) > 255:
        return None, "Filename too long (max 255 bytes)."
    return name, None


# ── Environment detection ──────────────────────────────────────────────────

def _is_raspberry_pi():
    try:
        return "raspberry pi" in Path("/proc/device-tree/model").read_text().lower()
    except OSError:
        return False

_BASE = Path("/opt/pivideo") if _is_raspberry_pi() else Path(__file__).parent

VIDEO_DIR   = Path(os.environ.get("VIDEO_DIR",   str(_BASE / "videos")))
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", str(_BASE / "config.json")))
PORT        = int(os.environ.get("PORT", 8080))


# ── Constants ──────────────────────────────────────────────────────────────

MAX_MEDIA = 10

# Button number → hardware info (must match daemon/src/main.rs)
BUTTONS = {
    1: {"gpio": 4,  "pin": 7},
    2: {"gpio": 17, "pin": 11},
    3: {"gpio": 22, "pin": 15},
    4: {"gpio": 23, "pin": 16},
    5: {"gpio": 24, "pin": 18},
    6: {"gpio": 25, "pin": 22},
    7: {"gpio": 27, "pin": 13},
}

ALLOWED_VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".flv", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".m4v",
}

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

ALLOWED_EXTS = ALLOWED_VIDEO_EXTS | ALLOWED_IMAGE_EXTS

ACCEPT_ALL_ATTR = ",".join(sorted(ALLOWED_EXTS))


# ── Config I/O ─────────────────────────────────────────────────────────────

def _load_raw():
    """Return the full parsed config dict, or {} if missing/invalid."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_raw(raw):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(raw, indent=2) + "\n")


def _migrate_v1(raw):
    """Convert a v1 config to v2 format. Returns a v2 dict."""
    media = []
    for slot_str in sorted(raw.keys()):
        if slot_str in ("splash", "version"):
            continue
        if not slot_str.isdigit():
            continue
        entry = raw[slot_str]
        if isinstance(entry, dict) and entry.get("video"):
            media.append({"file": entry["video"], "button": int(slot_str)})
        elif isinstance(entry, str) and entry:
            media.append({"file": entry, "button": int(slot_str)})

    splash = raw.get("splash", {})
    if isinstance(splash, dict):
        if splash.get("video"):
            media.append({"file": splash["video"], "button": None})
        if splash.get("image"):
            media.append({"file": splash["image"], "button": None})

    return {
        "version": 2,
        "media": media[:MAX_MEDIA],
        "buttons": {str(k): v for k, v in BUTTONS.items()},
    }


def load_media():
    """Return list of {"file": str, "button": int|None}, max 10."""
    raw = _load_raw()
    if raw.get("version") != 2:
        if raw:
            raw = _migrate_v1(raw)
            _save_raw(raw)
        else:
            return []
    media = raw.get("media", [])
    result = []
    for entry in media[:MAX_MEDIA]:
        if isinstance(entry, dict) and "file" in entry:
            btn = entry.get("button")
            if btn is not None:
                try:
                    btn = int(btn)
                except (ValueError, TypeError):
                    btn = None
                if btn not in BUTTONS:
                    btn = None
            result.append({"file": entry["file"], "button": btn})
    return result


def save_media(media):
    """Write media list to config."""
    raw = _load_raw()
    raw["version"] = 2
    raw["media"] = media[:MAX_MEDIA]
    raw["buttons"] = {str(k): v for k, v in BUTTONS.items()}
    _save_raw(raw)


def _assigned_buttons(media):
    """Return set of button numbers currently assigned."""
    return {m["button"] for m in media if m["button"] is not None}


def needs_reboot():
    return Path("/var/run/reboot-required").exists()


# ── HTML rendering ─────────────────────────────────────────────────────────

def _file_info_html(filename, label_for_missing="&#9888; file missing"):
    """Return size info for a file in VIDEO_DIR."""
    if filename:
        fpath = VIDEO_DIR / filename
        if fpath.exists():
            return f"{fpath.stat().st_size / (1024 * 1024):.1f} MB"
        return label_for_missing
    return ""


def render_page(message="", error=""):
    media = load_media()
    assigned = _assigned_buttons(media)

    # ── Media entries ──────────────────────────────────────────────────────
    media_html = ""
    for i, entry in enumerate(media):
        fname = entry["file"]
        btn = entry["button"]
        size = _file_info_html(fname)
        ext = os.path.splitext(fname)[1].lower()
        is_image = ext in ALLOWED_IMAGE_EXTS

        # Button assignment dropdown
        options = '<option value=""' + (' selected' if btn is None else '') + '>None — kiosk rotation</option>'
        for b in sorted(BUTTONS):
            taken = b in assigned and b != btn
            sel = " selected" if b == btn else ""
            dis = " disabled" if taken else ""
            info = BUTTONS[b]
            label = f"Button {b} (GPIO {info['gpio']}, Pin {info['pin']})"
            if taken:
                label += " — in use"
            options += f'<option value="{b}"{sel}{dis}>{html.escape(label)}</option>'

        role = f"Button {btn} (GPIO {BUTTONS[btn]['gpio']}, Pin {BUTTONS[btn]['pin']})" if btn else "Kiosk rotation"
        kind = "image" if is_image else "video"

        media_html += f"""
      <div class="slot">
        <div class="slot-head">
          <span class="slot-title">{html.escape(fname)}</span>
          <span class="slot-meta">{size} &middot; {kind}</span>
        </div>
        <div class="slot-body">
          <form class="assign-row" method="POST" action="/assign">
            <input type="hidden" name="index" value="{i}">
            <label>Assign to:</label>
            <select name="button" onchange="this.form.submit()">{options}</select>
          </form>
          <div class="current">
            <span class="role">{html.escape(role)}</span>
            <form method="POST" action="/delete" style="display:inline">
              <input type="hidden" name="index" value="{i}">
              <button class="btn-clear"
                onclick="return confirm('Remove {html.escape(fname, quote=True)}?')">Delete</button>
            </form>
          </div>
        </div>
      </div>"""

    if not media:
        media_html = '<p class="empty">No media uploaded yet. Use the form below to add images or videos.</p>'

    # ── Upload form ────────────────────────────────────────────────────────
    if len(media) >= MAX_MEDIA:
        upload_html = f'<p class="msg">Library full ({MAX_MEDIA}/{MAX_MEDIA}). Delete a file to upload more.</p>'
    else:
        btn_options = '<option value="" selected>None — kiosk rotation</option>'
        for b in sorted(BUTTONS):
            taken = b in assigned
            dis = " disabled" if taken else ""
            info = BUTTONS[b]
            label = f"Button {b} (GPIO {info['gpio']}, Pin {info['pin']})"
            if taken:
                label += " — in use"
            btn_options += f'<option value="{b}"{dis}>{html.escape(label)}</option>'

        upload_html = f"""
      <form class="upload-form" method="POST" action="/upload" enctype="multipart/form-data">
        <div class="upload-row">
          <input type="file" name="file" accept="{ACCEPT_ALL_ATTR}" required>
          <label>Assign to:</label>
          <select name="button">{btn_options}</select>
          <button class="btn-upload">Upload</button>
        </div>
        <p class="hint">Accepted: images ({', '.join(sorted(ALLOWED_IMAGE_EXTS))}) and videos ({', '.join(sorted(ALLOWED_VIDEO_EXTS))})</p>
      </form>"""

    msg_html    = f'<p class="msg ok">{html.escape(message)}</p>' if message else ""
    err_html    = f'<p class="msg err">{html.escape(error)}</p>'   if error   else ""
    reboot_html = '<p class="msg reboot">&#9888; A reboot is required for recent updates to take effect.</p>' if needs_reboot() else ""

    count = len(media)
    kiosk_count = sum(1 for m in media if m["button"] is None)
    button_count = count - kiosk_count

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PiVideo</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box}}
    body{{font-family:sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;color:#222}}
    h1{{color:#2d6a4f;margin-bottom:.2rem}}
    .sub{{color:#666;font-size:.9rem;margin-bottom:1.5rem}}
    .msg{{padding:.5rem 1rem;border-radius:4px;margin-bottom:1rem}}
    .ok{{color:#1b4332;background:#d8f3dc}}
    .err{{color:#9b2226;background:#fde8e8}}
    .reboot{{color:#7d4e00;background:#fff3cd}}
    .slot{{border:1px solid #ddd;border-radius:8px;margin-bottom:.75rem;overflow:hidden}}
    .slot-head{{background:#f5f5f5;padding:.5rem 1rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem}}
    .slot-title{{font-weight:bold;font-family:monospace;font-size:.9rem}}
    .slot-meta{{font-size:.8rem;color:#888}}
    .slot-body{{padding:.75rem 1rem;display:flex;flex-direction:column;gap:.6rem}}
    .assign-row{{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}}
    .assign-row label{{font-size:.85rem;color:#555}}
    .assign-row select{{padding:.25rem .4rem;border-radius:4px;border:1px solid #ccc}}
    .current{{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;min-height:1.6rem}}
    .role{{font-size:.85rem;color:#555}}
    .empty{{color:#bbb;font-style:italic;font-size:.9rem;padding:1rem 0}}
    .upload-form{{border:1px solid #ddd;border-radius:8px;padding:1rem;background:#fafafa}}
    .upload-row{{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}}
    .upload-row label{{font-size:.85rem;color:#555}}
    .upload-row select{{padding:.25rem .4rem;border-radius:4px;border:1px solid #ccc}}
    .hint{{font-size:.8rem;color:#999;margin:.5rem 0 0}}
    button{{cursor:pointer;padding:.35rem .8rem;border:none;border-radius:4px;font-size:.85rem}}
    .btn-upload{{background:#2d6a4f;color:#fff}}
    .btn-clear{{background:#e5383b;color:#fff}}
    h2{{font-size:1rem;color:#555;margin:1.25rem 0 .5rem;text-transform:uppercase;letter-spacing:.05em}}
    .summary{{font-size:.85rem;color:#666;margin-bottom:.75rem}}
  </style>
</head>
<body>
  <h1>PiVideo</h1>
  <p class="sub">Upload images and videos, then optionally assign them to buttons.</p>
  {reboot_html}{msg_html}{err_html}
  <h2>Media Library ({count}/{MAX_MEDIA})</h2>
  <p class="summary">{kiosk_count} in kiosk rotation, {button_count} assigned to buttons</p>
  {media_html}
  <h2>Upload New Media</h2>
  {upload_html}
</body>
</html>"""


# ── Request handler ────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")

    def send_html(self, body, status=200):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location="/"):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", ""):
            self.send_html(render_page())
        else:
            self.send_html("<h1>Not Found</h1>", 404)

    def do_POST(self):
        if self.path == "/upload":
            self._handle_upload()
        elif self.path == "/assign":
            self._handle_assign()
        elif self.path == "/delete":
            self._handle_delete()
        else:
            self.send_html("<h1>Not Found</h1>", 404)

    def _handle_upload(self):
        ct = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ct:
            self.send_html(render_page(error="Invalid request."), 400)
            return

        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_UPLOAD_BYTES:
            self.send_html(
                render_page(error=f"Upload too large (limit: {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."),
                413,
            )
            return

        form = _parse_multipart(self.rfile, ct, length)

        field = form.get("file")
        if not field or not field.filename:
            self.send_html(render_page(error="No file selected."), 400)
            return

        filename, err = _sanitize_filename(field.filename)
        if err:
            self.send_html(render_page(error=err), 400)
            return

        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTS:
            self.send_html(
                render_page(error=f"Unsupported format '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTS))}"),
                400,
            )
            return

        media = load_media()
        if len(media) >= MAX_MEDIA:
            self.send_html(render_page(error=f"Library full ({MAX_MEDIA}/{MAX_MEDIA}). Delete a file first."), 400)
            return

        # Parse optional button assignment
        btn_str = form.getvalue("button", "")
        btn = None
        if btn_str:
            try:
                btn = int(btn_str)
            except (ValueError, TypeError):
                btn = None
            if btn is not None and btn not in BUTTONS:
                self.send_html(render_page(error="Invalid button number."), 400)
                return
            if btn is not None and btn in _assigned_buttons(media):
                self.send_html(render_page(error=f"Button {btn} is already assigned."), 400)
                return

        dest = VIDEO_DIR / filename
        try:
            VIDEO_DIR.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                shutil.copyfileobj(field.file, f)
        except OSError as e:
            dest.unlink(missing_ok=True)
            if e.errno == errno.ENOSPC:
                self.send_html(render_page(error="Not enough space on the SD card to save this file."), 507)
            else:
                self.send_html(render_page(error=f"Could not save file: {e.strerror}"), 500)
            return

        media.append({"file": filename, "button": btn})
        try:
            save_media(media)
        except OSError as e:
            dest.unlink(missing_ok=True)
            self.send_html(render_page(error=f"Could not update configuration: {e.strerror}"), 500)
            return

        self.redirect()

    def _handle_assign(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)

        media = load_media()

        try:
            index = int(params.get("index", -1))
        except (ValueError, TypeError):
            index = -1
        if index < 0 or index >= len(media):
            self.send_html(render_page(error="Invalid media index."), 400)
            return

        btn_str = params.get("button", "")
        btn = None
        if btn_str:
            try:
                btn = int(btn_str)
            except (ValueError, TypeError):
                btn = None
            if btn is not None and btn not in BUTTONS:
                self.send_html(render_page(error="Invalid button number."), 400)
                return
            if btn is not None:
                assigned = _assigned_buttons(media)
                current_btn = media[index]["button"]
                if current_btn is not None:
                    assigned.discard(current_btn)
                if btn in assigned:
                    self.send_html(render_page(error=f"Button {btn} is already assigned."), 400)
                    return

        media[index]["button"] = btn
        save_media(media)
        self.redirect()

    def _handle_delete(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)

        media = load_media()

        try:
            index = int(params.get("index", -1))
        except (ValueError, TypeError):
            index = -1
        if index < 0 or index >= len(media):
            self.send_html(render_page(error="Invalid media index."), 400)
            return

        removed = media.pop(index)
        save_media(media)

        # Delete the file from disk
        fpath = VIDEO_DIR / removed["file"]
        fpath.unlink(missing_ok=True)

        self.redirect()


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    # Migrate on startup if needed
    raw = _load_raw()
    if raw and raw.get("version") != 2:
        _save_raw(_migrate_v1(raw))

    server = HTTPServer(("", PORT), Handler)
    print(f"PiVideo  http://0.0.0.0:{PORT}")
    print(f"Videos:  {VIDEO_DIR}")
    print(f"Config:  {CONFIG_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
