# PiVideo

A Raspberry Pi kiosk system that plays videos when physical buttons are pressed. Designed for National Parks, museums, and similar venues — press a button, watch a video. Supports up to 7 buttons.

Videos are managed through a browser-based interface: upload a file, assign it to a button, done. No renaming, no command line.

---

## 1. Install

### Download Raspberry Pi Imager

**[raspberrypi.com/software](https://www.raspberrypi.com/software/)** — available for Windows, macOS, and Linux.

### Download the PiVideo image

Download the latest `pivideo.img.xz` from the [GitHub Releases page](https://github.com/cd34/PiVideo/releases/latest).

### Flash the SD card

1. Open Raspberry Pi Imager
2. **Choose Device** → select your Pi model
3. **Choose OS** → scroll to the bottom → **Use custom** → select `pivideo.img.xz`
4. **Choose Storage** → select your SD card
5. Click **Next** — when asked *"Would you like to apply OS customisation settings?"* click **Edit Settings**
6. Fill in the **General** tab:
   - **Hostname** — the Pi will be reachable at `http://<hostname>.local:8080`
     - Pick something short and descriptive for the installation location, e.g. `dam-construction` or `glacier-trailhead`
     - Must be **unique on your network** — if two Pis share a hostname, neither will be reliably reachable
     - Letters, numbers, and hyphens only — no spaces, underscores, dots, or other punctuation; cannot start or end with a hyphen; maximum 63 characters
   - **Username and password** — credentials for SSH access
   - **Configure wireless LAN** — WiFi SSID, password, and country code
7. On the **Services** tab: enable **SSH** → *Use password authentication*
8. Click **Save** → **Yes** → **Yes** to write

Insert the SD card, power on the Pi, and wait about 2 minutes. First boot runs a full system update before the unit comes online.

---

## 2. Electronics

Connect momentary push buttons between each GPIO pin and any GND pin. The software uses internal pull-up resistors — no external resistors or other components are needed.

See [wiring.svg](wiring.svg) for the full diagram.

| Button | GPIO | Physical Pin |
|--------|------|-------------|
| 1      |  4   | 7           |
| 2      | 17   | 11          |
| 3      | 22   | 15          |
| 4      | 23   | 16          |
| 5      | 24   | 18          |
| 6      | 25   | 22          |
| 7      | 27   | 13          |

All buttons share a common ground. Connect the ground side of each button to a ground bus (terminal block strip), then run a single wire from the bus to any GND terminal on the HAT (pins 6, 9, 14, 20, 25…).

### Using the GPIO Screw Terminal HAT

The GPIO Screw Terminal HAT plugs directly onto the Pi's 40-pin header. Each GPIO pin becomes a labeled screw terminal — no soldering required.

1. Press the HAT onto the 40-pin header
2. Run a wire from the **GPIO terminal** to one side of the button
3. Run a wire from the **GND terminal** to the other side of the button
4. Tighten with a small flathead screwdriver

For arcade-style buttons, use **0.25" quick-connect wires** — they slip directly onto the button's spade terminals, no crimping or soldering needed.

---

## 3. Managing Videos

Open a browser and go to `http://<hostname>.local:8080`.

The page shows all 7 button slots. Each slot displays the currently assigned video (or "no video assigned") and has an Upload button.

- **Upload** — select a video file from your computer; it is assigned to that slot immediately
- **Replace** — upload a new file to a slot that already has one
- **Clear** — remove the assignment (the file stays on the Pi; the button simply does nothing until a new video is assigned)

Changes take effect instantly — no restart required.

### Supported video formats

`.mp4` is recommended. The following formats are also accepted:

| Format | Extension(s) |
|--------|-------------|
| MPEG-4 | `.mp4`, `.m4v` |
| Matroska | `.mkv` |
| QuickTime | `.mov` |
| WebM | `.webm` |
| AVI | `.avi` |
| Flash Video | `.flv` |
| Windows Media | `.wmv` |
| MPEG | `.mpg`, `.mpeg` |
| Transport Stream | `.ts`, `.m2ts` |
| Mobile | `.3gp` |

### Admin machine folder layout

Keep a folder on your admin machine named after each Pi's hostname. This makes it easy to manage multiple deployments and re-upload videos if a Pi needs to be reflashed.

```
~/pivideo/
  glacier-visitor-center/
    videos/
      button1_glacier_intro.mp4
      button2_wildlife.mp4
      button3_trail_map.mp4

  canyon-trailhead/
    videos/
      button1_welcome.mp4
      button2_safety.mp4
```

---

## How It Works

A Rust daemon runs on the Pi and polls the GPIO pins every 10ms. When a button is pressed, it reads `config.json` to find the assigned video and plays it fullscreen using `mpv`. The web UI writes the same `config.json`, so reassignments are picked up immediately without restarting anything.

### Repository layout

```
daemon/       Rust GPIO daemon (pivideo-daemon)
image/        Scripts and overlays for building the Pi SD card image
web/          Python web UI for uploading and managing videos
```

### Components

**Daemon (`daemon/`)** — Rust binary that:
- Reads GPIO pin assignments from `config.json` at startup
- Polls pins every 10ms; plays the assigned video fullscreen via `mpv` on press
- Waits for button release before resuming (debounce)
- Runs as a systemd service, starts on boot

**Web UI (`web/`)** — Lightweight Python web server (no dependencies beyond stdlib) that:
- Shows 7 button slots with current assignments
- Accepts video uploads and writes assignments to `config.json`
- Displays a reboot notice when a system update requires one
- Starts on boot via systemd, accessible at `http://<hostname>.local:8080`

---

## Building from Source

### Prerequisites

- Rust toolchain with `aarch64-unknown-linux-gnu` target
- `pi-gen` (or compatible Raspberry Pi image builder)
- Python 3

### Commands

```bash
make build    # compile daemon (local, for development)
make test     # run tests
make image    # cross-compile for Pi and assemble rootfs overlay
make clean    # clean build artifacts
```

`make image` cross-compiles the daemon, copies the web server, generates a default `config.json`, and assembles `image/rootfs-overlay/`. Pass the result to `pi-gen` to produce a bootable `.img`.

For local testing without Pi Imager, bake in deployment settings:

```bash
cp image/config.env.example image/config.env
$EDITOR image/config.env   # set hostname, user, WiFi
make image
```

`config.env` is gitignored — credentials are never committed.

### Daemon logging

```bash
RUST_LOG=info ./pivideo-daemon
```

---

## Appendix: Parts List

Links to be added — search terms and sources are listed where links are pending.

### Raspberry Pi

Any of the following are sufficient. Older models (Pi 1, Pi 2) are not recommended — video playback will be sluggish.

| Model | Power | HDMI connector | Notes |
|-------|-------|---------------|-------|
| Raspberry Pi 3 Model B / B+ | Micro USB, 5V 2.5A | Full-size HDMI | Solid choice |
| Raspberry Pi 4 Model B | USB-C, 5V 3A | micro-HDMI | Best performance |
| Raspberry Pi 5 | USB-C, 5V 5A (27W) | micro-HDMI | Overkill but works |
| Raspberry Pi Zero 2 W | Micro USB, 5V 2.5A | mini-HDMI | Budget option; adequate for 1080p |

### Power Supply

Use the official Raspberry Pi power supply for your model — third-party supplies that skimp on current cause instability under video load.

| Pi Model | Connector | Minimum current |
|----------|-----------|----------------|
| Pi 3 Model B / B+ | Micro USB | 2.5A |
| Pi Zero 2 W | Micro USB | 2.5A |
| Pi 4 Model B | USB-C | 3A |
| Pi 5 | USB-C | 5A (27W) |

- Raspberry Pi official power supplies — [link TBD]

### HDMI Cable

Match the cable to your Pi model's port.

| Pi Model | Cable needed |
|----------|-------------|
| Pi 3 Model B / B+ | Standard HDMI to HDMI |
| Pi 4 Model B | micro-HDMI to HDMI |
| Pi 5 | micro-HDMI to HDMI |
| Pi Zero 2 W | mini-HDMI to HDMI |

### GPIO Screw Terminal HAT

Plugs onto the Pi's 40-pin header; every pin becomes a labeled screw terminal.

- Adafruit — [link TBD]
- Search: *"Raspberry Pi GPIO screw terminal HAT"*

### Momentary Push Buttons (non-illuminated)

One per video slot, up to 7. Use normally-open momentary buttons without LEDs — no extra wiring or resistors needed.

- Adafruit — [link TBD]
- Search: *"arcade button 30mm momentary non-illuminated"*

### Arcade Button Quick-Connect Wires — 0.25" (6.3mm)

Slip directly onto the spade terminals of arcade buttons — no crimping or soldering needed. One wire per button terminal (two per button).

- Adafruit — [link TBD]
- Search: *"Arcade Button and Switch Quick-Connect Wires 0.25"*

### Wire

**22 AWG stranded hookup wire.** Buy two colors — one for signal, one for ground.

- Flexible enough to route through an enclosure
- Seats cleanly in screw terminals
- 24 AWG works too; avoid solid-core (fatigues at screw terminals)

### Ground Bus

All button ground wires need to meet at a single point before running to the Pi's GND pin. A small terminal block strip used as a bus bar is the cleanest approach — connect all ground wires to the strip, then run one wire from the strip to a GND pin on the HAT.

- Search: *"terminal block bus bar"* or *"barrier strip terminal block"*
- Any small multi-position terminal strip (6–10 positions) works

### Ferrules + Crimper (optional but recommended)

Crimp a ferrule onto each wire end before inserting into a screw terminal. Prevents fraying and makes re-termination clean.

- **Size:** 0.5 mm² for 22 AWG
- Search: *"ferrule crimper kit"* — kits typically include the crimper and an assortment of sizes

### Tools

- Wire stripper (22–24 AWG range)
- Small flathead screwdriver (for screw terminal HAT)
- Ferrule crimper (if using ferrules)
