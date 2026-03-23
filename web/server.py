#!/usr/bin/env python3
"""PiVideo web UI — manage video slots."""

import cgi
import html
import json
import os
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


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

ACCEPT_ATTR = ",".join(sorted(ALLOWED_EXTS))


# ── Config I/O ─────────────────────────────────────────────────────────────

def _default_slot(info):
    return {"gpio": info["gpio"], "pin": info["pin"], "video": None}


def load_config():
    """Return {slot_int: {gpio, pin, video}} for all 7 slots."""
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text())
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
        except (json.JSONDecodeError, ValueError):
            pass
    return {s: _default_slot(SLOTS[s]) for s in SLOTS}


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(
            {str(k): config[k] for k in sorted(config)},
            indent=2,
        ) + "\n"
    )


def needs_reboot():
    return Path("/var/run/reboot-required").exists()


# ── HTML rendering ─────────────────────────────────────────────────────────

def render_page(message="", error=""):
    config = load_config()

    slots_html = ""
    for slot_num, info in SLOTS.items():
        slot_cfg = config.get(slot_num, {})
        assigned = slot_cfg.get("video")
        gpio = slot_cfg.get("gpio", info["gpio"])
        pin  = slot_cfg.get("pin",  info["pin"])

        if assigned:
            fpath = VIDEO_DIR / assigned
            if fpath.exists():
                size = f"{fpath.stat().st_size / (1024 * 1024):.1f} MB"
            else:
                size = "&#9888; file missing"
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
            <input type="file" name="video" accept="{ACCEPT_ATTR}" required>
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
  </style>
</head>
<body>
  <h1>PiVideo</h1>
  <p class="sub">Assign videos to buttons. Preferred format: <strong>.mp4</strong>. Also accepted: {html.escape(", ".join(sorted(ALLOWED_EXTS - {".mp4"})))}</p>
  {reboot_html}{msg_html}{err_html}
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
        form = cgi.FieldStorage(
            fp=self.rfile, headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ct,
                     "CONTENT_LENGTH": str(length)},
        )

        try:
            slot = int(form.getvalue("slot", 0))
        except (ValueError, TypeError):
            slot = 0
        if slot not in SLOTS:
            self.send_html(render_page(error="Invalid slot."), 400)
            return

        field = form.get("video")
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

    def _handle_clear(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)

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
        save_config({s: None for s in SLOTS})

    server = HTTPServer(("", PORT), Handler)
    print(f"PiVideo  http://0.0.0.0:{PORT}")
    print(f"Videos:  {VIDEO_DIR}")
    print(f"Config:  {CONFIG_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
