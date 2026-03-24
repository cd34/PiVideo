#!/usr/bin/env python3
"""PiVideo web UI — manage video slots and idle splash."""

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

# Slot number → hardware info (must match daemon/src/main.rs)
SLOTS = {
    1: {"gpio": 4,  "pin": 7},
    2: {"gpio": 17, "pin": 11},
    3: {"gpio": 22, "pin": 15},
    4: {"gpio": 23, "pin": 16},
    5: {"gpio": 24, "pin": 18},
    6: {"gpio": 25, "pin": 22},
    7: {"gpio": 27, "pin": 13},
}

ALLOWED_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".flv", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".m4v",
}

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

ACCEPT_VIDEO_ATTR = ",".join(sorted(ALLOWED_EXTS))
ACCEPT_IMAGE_ATTR = ",".join(sorted(ALLOWED_IMAGE_EXTS))


# ── Config I/O ─────────────────────────────────────────────────────────────

def _default_slot(info):
    return {"gpio": info["gpio"], "pin": info["pin"], "video": None}


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


def load_config():
    """Return {slot_int: {gpio, pin, video}} for all 7 slots."""
    raw = _load_raw()
    config = {}
    for slot_num, info in SLOTS.items():
        entry = raw.get(str(slot_num), {})
        if isinstance(entry, dict):
            config[slot_num] = {
                "gpio":  entry.get("gpio",  info["gpio"]),
                "pin":   entry.get("pin",   info["pin"]),
                "video": entry.get("video"),
            }
        else:
            # migrate old string-value format
            config[slot_num] = {**_default_slot(info), "video": entry or None}
    return config


def save_config(config):
    raw = _load_raw()
    for k in sorted(config):
        raw[str(k)] = config[k]
    _save_raw(raw)


def load_splash():
    """Return {"image": str|None, "video": str|None}."""
    s = _load_raw().get("splash", {})
    if isinstance(s, dict):
        return {"image": s.get("image"), "video": s.get("video")}
    return {"image": None, "video": None}


def save_splash(splash):
    raw = _load_raw()
    raw["splash"] = splash
    _save_raw(raw)


def needs_reboot():
    return Path("/var/run/reboot-required").exists()


# ── HTML rendering ─────────────────────────────────────────────────────────

def _file_info_html(filename, label_for_missing="&#9888; file missing"):
    """Return (size_html) for a file in VIDEO_DIR."""
    if filename:
        fpath = VIDEO_DIR / filename
        if fpath.exists():
            return f"{fpath.stat().st_size / (1024 * 1024):.1f} MB"
        return label_for_missing
    return ""


def render_page(message="", error=""):
    config = load_config()
    splash = load_splash()

    # ── Splash section ─────────────────────────────────────────────────────
    def splash_row(kind, current_file, accept_attr, allowed_exts, label):
        if current_file:
            size = _file_info_html(current_file)
            current_html = f"""
              <span class="fname">{html.escape(current_file)}</span>
              <span class="fsize">{size}</span>
              <form method="POST" action="/clear" style="display:inline">
                <input type="hidden" name="splash" value="{kind}">
                <button class="btn-clear"
                  onclick="return confirm('Remove idle {label}?')">Clear</button>
              </form>"""
            upload_label = "Replace"
        else:
            current_html = f'<span class="empty">— none —</span>'
            upload_label = "Upload"

        return f"""
      <div class="slot-body splash-row">
        <div class="splash-kind">{label}</div>
        <div class="current">{current_html}</div>
        <form class="upload-row" method="POST" action="/upload" enctype="multipart/form-data">
          <input type="hidden" name="splash" value="{kind}">
          <input type="file" name="file" accept="{accept_attr}" required>
          <button class="btn-upload">{upload_label}</button>
        </form>
      </div>"""

    splash_html = f"""
    <div class="slot splash-section">
      <div class="slot-head">
        <span class="slot-title">Idle Screen</span>
        <span class="slot-meta">shown when no button video is playing</span>
      </div>
      {splash_row("video", splash.get("video"), ACCEPT_VIDEO_ATTR, ALLOWED_EXTS, "Idle video (loops)")}
      {splash_row("image", splash.get("image"), ACCEPT_IMAGE_ATTR, ALLOWED_IMAGE_EXTS, "Idle image (fallback)")}
    </div>"""

    # ── Button slots ───────────────────────────────────────────────────────
    slots_html = ""
    for slot_num, info in SLOTS.items():
        slot_cfg = config.get(slot_num, {})
        assigned = slot_cfg.get("video")
        gpio = slot_cfg.get("gpio", info["gpio"])
        pin  = slot_cfg.get("pin",  info["pin"])

        if assigned:
            size = _file_info_html(assigned)
            current_html = f"""
              <span class="fname">{html.escape(assigned)}</span>
              <span class="fsize">{size}</span>
              <form method="POST" action="/clear" style="display:inline">
                <input type="hidden" name="slot" value="{slot_num}">
                <button class="btn-clear"
                  onclick="return confirm('Remove video from Button {slot_num}?')">Clear</button>
              </form>"""
            upload_label = "Replace"
        else:
            current_html = '<span class="empty">— no video assigned —</span>'
            upload_label = "Upload"

        slots_html += f"""
      <div class="slot">
        <div class="slot-head">
          <span class="slot-title">Button {slot_num}</span>
          <span class="slot-meta">GPIO {gpio} &nbsp;&middot;&nbsp; Physical Pin {pin}</span>
        </div>
        <div class="slot-body">
          <div class="current">{current_html}</div>
          <form class="upload-row" method="POST" action="/upload" enctype="multipart/form-data">
            <input type="hidden" name="slot" value="{slot_num}">
            <input type="file" name="file" accept="{ACCEPT_VIDEO_ATTR}" required>
            <button class="btn-upload">{upload_label}</button>
          </form>
        </div>
      </div>"""

    msg_html    = f'<p class="msg ok">{html.escape(message)}</p>' if message else ""
    err_html    = f'<p class="msg err">{html.escape(error)}</p>'   if error   else ""
    reboot_html = '<p class="msg reboot">&#9888; A reboot is required for recent updates to take effect.</p>' if needs_reboot() else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PiVideo</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box}}
    body{{font-family:sans-serif;max-width:680px;margin:2rem auto;padding:0 1rem;color:#222}}
    h1{{color:#2d6a4f;margin-bottom:.2rem}}
    .sub{{color:#666;font-size:.9rem;margin-bottom:1.5rem}}
    .msg{{padding:.5rem 1rem;border-radius:4px;margin-bottom:1rem}}
    .ok{{color:#1b4332;background:#d8f3dc}}
    .err{{color:#9b2226;background:#fde8e8}}
    .reboot{{color:#7d4e00;background:#fff3cd}}
    .slot{{border:1px solid #ddd;border-radius:8px;margin-bottom:.75rem;overflow:hidden}}
    .slot-head{{background:#f5f5f5;padding:.5rem 1rem;display:flex;justify-content:space-between;align-items:center}}
    .slot-title{{font-weight:bold}}
    .slot-meta{{font-family:monospace;font-size:.8rem;color:#888}}
    .slot-body{{padding:.75rem 1rem;display:flex;flex-direction:column;gap:.6rem}}
    .current{{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;min-height:1.6rem}}
    .fname{{font-family:monospace;font-size:.9rem}}
    .fsize{{font-size:.8rem;color:#888}}
    .empty{{color:#bbb;font-style:italic;font-size:.9rem}}
    .upload-row{{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}}
    button{{cursor:pointer;padding:.35rem .8rem;border:none;border-radius:4px;font-size:.85rem}}
    .btn-upload{{background:#2d6a4f;color:#fff}}
    .btn-clear{{background:#e5383b;color:#fff}}
    .splash-section .slot-head{{background:#eef4fb}}
    .splash-row{{border-top:1px solid #eee;display:grid;grid-template-columns:10rem 1fr auto;align-items:center;gap:.75rem;padding:.6rem 1rem}}
    .splash-row:first-of-type{{border-top:none}}
    .splash-kind{{font-size:.85rem;color:#555}}
    .splash-row .upload-row{{justify-content:flex-end}}
    h2{{font-size:1rem;color:#555;margin:1.25rem 0 .5rem;text-transform:uppercase;letter-spacing:.05em}}
  </style>
</head>
<body>
  <h1>PiVideo</h1>
  <p class="sub">Assign videos to buttons. Preferred format: <strong>.mp4</strong>. Also accepted: {html.escape(", ".join(sorted(ALLOWED_EXTS - {".mp4"})))}</p>
  {reboot_html}{msg_html}{err_html}
  <h2>Idle Screen</h2>
  {splash_html}
  <h2>Buttons</h2>
  {slots_html}
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
        elif self.path == "/clear":
            self._handle_clear()
        else:
            self.send_html("<h1>Not Found</h1>", 404)

    def _handle_upload(self):
        ct = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ct:
            self.send_html(render_page(error="Invalid request."), 400)
            return

        length = int(self.headers.get("Content-Length", 0))
        form = _parse_multipart(self.rfile, ct, length)

        splash_kind = form.getvalue("splash")  # "image" | "video" | None

        if splash_kind in ("image", "video"):
            self._upload_splash(form, splash_kind)
        else:
            self._upload_slot(form)

    def _upload_slot(self, form):
        try:
            slot = int(form.getvalue("slot", 0))
        except (ValueError, TypeError):
            slot = 0
        if slot not in SLOTS:
            self.send_html(render_page(error="Invalid slot."), 400)
            return

        field = form.get("file")
        if not field or not field.filename:
            self.send_html(render_page(error="No file selected."), 400)
            return

        filename = os.path.basename(field.filename)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTS:
            self.send_html(
                render_page(error=f"Unsupported format '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTS))}"),
                400,
            )
            return

        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        with open(VIDEO_DIR / filename, "wb") as f:
            shutil.copyfileobj(field.file, f)

        config = load_config()
        config[slot]["video"] = filename
        save_config(config)
        self.redirect()

    def _upload_splash(self, form, kind):
        field = form.get("file")
        if not field or not field.filename:
            self.send_html(render_page(error="No file selected."), 400)
            return

        filename = os.path.basename(field.filename)
        ext = os.path.splitext(filename)[1].lower()

        if kind == "image":
            if ext not in ALLOWED_IMAGE_EXTS:
                self.send_html(
                    render_page(error=f"Unsupported image format '{ext}'. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTS))}"),
                    400,
                )
                return
        else:  # video
            if ext not in ALLOWED_EXTS:
                self.send_html(
                    render_page(error=f"Unsupported format '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTS))}"),
                    400,
                )
                return

        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        with open(VIDEO_DIR / filename, "wb") as f:
            shutil.copyfileobj(field.file, f)

        splash = load_splash()
        splash[kind] = filename
        save_splash(splash)
        self.redirect()

    def _handle_clear(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)

        splash_kind = params.get("splash")

        if splash_kind in ("image", "video"):
            splash = load_splash()
            splash[splash_kind] = None
            save_splash(splash)
            self.redirect()
            return

        try:
            slot = int(params.get("slot", 0))
        except ValueError:
            slot = 0
        if slot not in SLOTS:
            self.send_html(render_page(error="Invalid slot."), 400)
            return

        config = load_config()
        config[slot]["video"] = None
        save_config(config)
        self.redirect()


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config({s: _default_slot(SLOTS[s]) for s in SLOTS})

    server = HTTPServer(("", PORT), Handler)
    print(f"PiVideo  http://0.0.0.0:{PORT}")
    print(f"Videos:  {VIDEO_DIR}")
    print(f"Config:  {CONFIG_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
