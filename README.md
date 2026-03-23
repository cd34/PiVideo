# PiVideo

A Raspberry Pi kiosk system that plays videos when physical buttons are pressed. Designed for National Parks, museums, and similar venues — press a button, watch a video. Supports up to 7 buttons.

Videos are managed through a browser-based interface: upload a file, assign it to a button, done. No renaming, no command line.

### Modes

| Mode | Setup | Behavior |
|------|-------|----------|
| **Splash image + buttons** | Upload an idle image; assign videos to buttons | A still image is displayed until a visitor presses a button, then the video plays fullscreen. Returns to the image when done. |
| **Splash video + buttons** | Upload an idle video; assign videos to buttons | A looping video plays when idle. A button press immediately interrupts it and plays the assigned video. Returns to the looping video when done. |
| **Single repeating video** | Upload an idle video; leave buttons empty | One video loops continuously — no buttons needed. |

### Why PiVideo?

- **Low cost, durable hardware.** A Raspberry Pi costs a fraction of a full computer and is easy to replace if damaged.
- **No moving parts.** No fan, no spinning hard drive — nothing to wear out or fail in a dusty exhibit environment.
- **Self-contained storage.** A 32 GB SD card holds roughly 8–30 hours of video content depending on resolution and bitrate — more than enough for a typical kiosk deployment.

### Future

A selector interface is planned to support more than 7 videos on the same hardware: a menu with up/down navigation and a play button would let visitors browse a larger library without needing additional GPIO pins.

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

Connect momentary push buttons between each GPIO pin and any GND pin. The Pi's GPIO pins have internal pull-up resistors that the daemon enables at startup — no external pull-up resistors or other components are needed for the switch circuit.

See [wiring.svg](wiring.svg) for the full diagram. For optional LED illumination on the buttons, see [wiring-led.svg](wiring-led.svg).

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

If you are using illuminated buttons, use a second terminal strip as a 3.3V power bus — run one wire from Pi Pin 1 or Pin 17 (3.3V) to the strip, then a short wire from the strip to each button's LED+ terminal. This keeps wiring tidy and avoids running seven individual wires back to the Pi.

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

### Idle screen

When no button video is playing, the screen shows an idle splash. Two options, configured from the web UI:

- **Idle video** — a video that loops continuously (takes priority over idle image)
- **Idle image** — a still image displayed indefinitely (fallback if no idle video is set)

If neither is configured, the screen goes black when idle.

Supported idle image formats: `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`, `.gif`

### Button press behavior

Pressing a button always interrupts whatever is currently on screen — whether that is the idle splash or a video from a different button — and immediately starts the assigned video. When the video finishes, the display returns to the idle splash.

### Single-video loop

To loop one video endlessly without any buttons: assign it as the **idle video** and leave all button slots empty.

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
- Any button press kills the current video (or idle splash) and starts the new one immediately
- Waits for button release before resuming (debounce)
- Shows idle splash (looping video or still image) when no button video is playing
- Runs as a systemd service, starts on boot

**Web UI (`web/`)** — Lightweight Python web server (no dependencies beyond stdlib) that:
- Shows idle screen controls (idle video + idle image) and 7 button slots
- Accepts video and image uploads and writes assignments to `config.json`
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
make deb      # build pivideo_VERSION_arm64.deb (requires aarch64 binary + dpkg-dev)
make image    # cross-compile, build .deb, and assemble rootfs overlay
make clean    # clean build artifacts
```

`make image` cross-compiles the daemon, builds the `.deb`, copies the web server, generates a default `config.json`, and assembles `image/rootfs-overlay/`. Pass the result to `pi-gen` to produce a bootable `.img`.

### Releasing updates to deployed kiosks

PiVideo uses a self-hosted apt repository on GitHub Pages so deployed kiosks receive daemon and web server updates automatically via `unattended-upgrades` — no reflashing required.

**One-time setup (do this before your first release):**

1. Generate a GPG signing key:
   ```bash
   bash scripts/gen-signing-key.sh
   ```
2. Follow the printed instructions:
   - Store the private key in GitHub repo **Settings → Secrets → Actions** as `GPG_PRIVATE_KEY`
   - The public key is written to `image/rootfs-overlay/etc/apt/trusted.gpg.d/pivideo.asc` — commit it
   - Copy the fingerprint into `reprepro/conf/distributions`, replacing `FILL_IN_KEY_FINGERPRINT`
3. Commit and push:
   ```bash
   git add image/rootfs-overlay/etc/apt/trusted.gpg.d/pivideo.asc reprepro/conf/distributions
   git commit -m "Add apt signing key and repo config"
   git push
   ```
4. In GitHub repo **Settings → Pages**, set the source to the `gh-pages` branch.

**To publish a release:**

```bash
# Bump the version
echo "1.1.0" > VERSION
git add VERSION
git commit -m "Release v1.1.0"
git tag v1.1.0
git push && git push --tags
```

GitHub Actions builds the `.deb`, signs it, publishes it to the `gh-pages` apt repo, and creates a GitHub Release. Deployed kiosks pick it up on their next nightly `unattended-upgrades` run. The postinst script restarts both services automatically.

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

## Appendix: Build Requirements

Requirements for running `make build` and `make image` on your development machine.

### macOS

1. **Rust** — install via [rustup](https://rustup.rs/):
   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   ```
2. **aarch64 cross-compilation target:**
   ```bash
   rustup target add aarch64-unknown-linux-gnu
   ```
3. **Cross-compiler toolchain** — install via Homebrew:
   ```bash
   brew install aarch64-unknown-linux-gnu
   ```
   This provides the `aarch64-unknown-linux-gnu-gcc` linker that Cargo needs. You may also need to tell Cargo where to find it — add to `~/.cargo/config.toml`:
   ```toml
   [target.aarch64-unknown-linux-gnu]
   linker = "aarch64-unknown-linux-gnu-gcc"
   ```
4. **Python 3** — ships with macOS; or install via `brew install python`.
5. **`pi-gen`** — runs on Linux only. On macOS, run it inside Docker or a Linux VM to produce the final `.img`.

### Linux

1. **Rust** — install via [rustup](https://rustup.rs/):
   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   ```
2. **aarch64 cross-compilation target and toolchain** (Debian/Ubuntu):
   ```bash
   rustup target add aarch64-unknown-linux-gnu
   sudo apt-get install gcc-aarch64-linux-gnu
   ```
   Add to `~/.cargo/config.toml`:
   ```toml
   [target.aarch64-unknown-linux-gnu]
   linker = "aarch64-linux-gnu-gcc"
   ```
3. **`pi-gen` dependencies** (Debian/Ubuntu):
   ```bash
   sudo apt-get install coreutils quilt parted qemu-user-static debootstrap \
     zerofree zip dosfstools libarchive-tools libcap2-bin grep rsync xz-utils \
     file git curl bc
   ```
   See the [pi-gen README](https://github.com/RPi-Distro/pi-gen) for the current full list.
4. **Python 3** — install via your distro package manager if not already present.

### Windows

Cross-compiling for aarch64 Linux and running `pi-gen` are not natively supported on Windows. Use one of these approaches:

- **WSL 2 (recommended)** — install Ubuntu via the Microsoft Store, then follow the Linux instructions above inside the WSL environment.
- **Docker** — the [pi-gen Docker workflow](https://github.com/RPi-Distro/pi-gen#running-in-docker) runs the full image build in a container. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and follow those instructions.

For development without building an image (daemon compilation only), install Rust for Windows from [rustup.rs](https://rustup.rs/) and use WSL for the cross-compilation step.

---

## Appendix: Parts List

### Raspberry Pi

Available from [raspberrypi.com](https://www.raspberrypi.com/products/) and authorized resellers. The **Pi 3 B+**, **Pi 4 B**, or **Pi 5 (2GB or 4GB)** are the recommended choices — right balance of performance and cost. Older models (Pi 1, Pi 2) are not recommended; video playback will be sluggish.

| Model | Power | HDMI | Notes |
|-------|-------|------|-------|
| Pi 3 Model B | Micro USB, 5V 2.5A | Full-size HDMI | |
| Pi 3 Model B+ | Micro USB, 5V 2.5A | Full-size HDMI | Recommended |
| Pi 4 Model B | USB-C, 5V 3A | micro-HDMI | Recommended |
| Pi 5 (2GB) | USB-C, 5V 5A | micro-HDMI | Recommended |
| Pi 5 (4GB) | USB-C, 5V 5A | micro-HDMI | Recommended |
| Pi 5 (8GB) | USB-C, 5V 5A | micro-HDMI | More than needed |
| Pi 5 (16GB) | USB-C, 5V 5A | micro-HDMI | Overkill |
| Pi Zero 2 W | Micro USB, 5V 2.5A | mini-HDMI | Budget option; adequate for 1080p |

**Where to buy:**

| Model | Adafruit | Sparkfun |
|-------|----------|----------|
| Pi 3 Model B | [Adafruit](https://www.adafruit.com/product/3055) | — |
| Pi 3 Model B+ | [Adafruit](https://www.adafruit.com/product/3775) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-3-b.html) |
| Pi 4 Model B (1 GB) | [Adafruit](https://www.adafruit.com/product/4295) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-4-model-b-1gb.html) |
| Pi 4 Model B (2 GB) | [Adafruit](https://www.adafruit.com/product/4292) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-4-model-b-2-gb.html) |
| Pi 4 Model B (4 GB) | [Adafruit](https://www.adafruit.com/product/4296) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-4-model-b-4-gb.html) |
| Pi 4 Model B (8 GB) | [Adafruit](https://www.adafruit.com/product/4564) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-4-model-b-8-gb.html) |
| Pi 5 (2GB) | [Adafruit](https://www.adafruit.com/product/6007) | — |
| Pi 5 (4GB) | [Adafruit](https://www.adafruit.com/product/5813) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-5-4gb.html) |
| Pi 5 (8GB) | [Adafruit](https://www.adafruit.com/product/5812) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-5-8gb.html) |
| Pi 5 (16GB) | [Adafruit](https://www.adafruit.com/product/6125) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-5-16gb.html) |

### Power Supply

Use the official Raspberry Pi power supply for your model. Third-party supplies that skimp on current cause instability under video load.

| Pi Model | Connector | Adafruit | Sparkfun |
|----------|-----------|----------|----------|
| Pi 3 Model B / B+ / Pi Zero 2 W | Micro USB 2.5A | [Adafruit](https://www.adafruit.com/product/1995) | [Sparkfun](https://www.sparkfun.com/wall-adapter-power-supply-5-1v-dc-2-5a-usb-micro-b.html) |
| Pi 4 Model B | USB-C 3A | [Adafruit](https://www.adafruit.com/product/4298) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-wall-adapter-power-supply-5-1vdc-3-0a-15-3w-usb-c.html) |
| Pi 5 | USB-C 5A (27W) | [Adafruit](https://www.adafruit.com/product/5814) | [Sparkfun](https://www.sparkfun.com/raspberry-pi-27w-usb-c-pd-power-supply-black.html) |

### HDMI Cable

Match the cable to your Pi model.

| Pi Model | Cable needed | Buy |
|----------|-------------|-----|
| Pi 3 Model B / B+ | Standard HDMI to HDMI | [Adafruit](https://www.adafruit.com/product/608) · [Sparkfun](https://www.sparkfun.com/raspberry-pi-official-hdmi-cable-1m.html) |
| Pi 4 Model B | micro-HDMI to HDMI | [Adafruit](https://www.adafruit.com/product/1322) · [Sparkfun](https://www.sparkfun.com/raspberry-pi-official-micro-hdmi-to-hdmi-a-cable-2m.html) |
| Pi 5 | micro-HDMI to HDMI | [Adafruit](https://www.adafruit.com/product/1322) · [Sparkfun](https://www.sparkfun.com/raspberry-pi-official-micro-hdmi-to-hdmi-a-cable-2m.html) |
| Pi Zero 2 W | mini-HDMI to HDMI | [Adafruit](https://www.adafruit.com/product/2775) · [Sparkfun](https://www.sparkfun.com/mini-hdmi-cable-3ft.html) |

### GPIO Screw Terminal HAT

Plugs onto the Pi's 40-pin header; every pin becomes a labeled screw terminal. No soldering required.

- [Adafruit — Pi-EzConnect Terminal Block Breakout HAT](https://www.adafruit.com/product/2711)
- [CZH Labs — Ultra Small RPi GPIO Terminal Block Breakout Board](https://czh-labs.com/products/ultra-small-rpi-gpio-terminal-block-breakout-board-module-for-raspberry-pi)

### Momentary Push Buttons

One per video slot, up to 7. Any normally-open momentary contact switch works. Three common options:

**Metal pushbuttons with LED ring (recommended)** — chrome-plated, 16mm panel-mount, rated for heavy use. The built-in LED is optional; the switch works without it. See [wiring-led.svg](wiring-led.svg) for LED wiring details (connect LED+ to 3.3V, LED− to GND — LEDs are always on, no software changes needed). The Sparkfun and Adafruit buttons listed below have a built-in current-limiting resistor, so no external resistor is required. If you use a different LED switch that lacks one, add a 150 Ω resistor in series with LED+.

| Color | Adafruit | Sparkfun |
|-------|----------|----------|
| Red | [Adafruit](https://www.adafruit.com/product/559) | [Sparkfun](https://www.sparkfun.com/metal-pushbutton-momentary-16mm-red-1.html) |
| Blue | [Adafruit](https://www.adafruit.com/product/481) | [Sparkfun](https://www.sparkfun.com/metal-pushbutton-momentary-16mm-blue-1.html) |
| Green | [Adafruit](https://www.adafruit.com/product/560) | [Sparkfun](https://www.sparkfun.com/metal-pushbutton-momentary-16mm-green-1.html) |
| White | [Adafruit](https://www.adafruit.com/product/558) | [Sparkfun](https://www.sparkfun.com/metal-pushbutton-momentary-16mm-white-1.html) |
| Yellow | — | [Sparkfun](https://www.sparkfun.com/metal-pushbutton-momentary-16mm-yellow-1.html) |

**Arcade-style buttons** — large, colorful, satisfying click. Popular for kiosks. No LED, no resistors, simple two-wire connection.
- Search: *"arcade button 30mm momentary non-illuminated"*

**Any momentary switch** — any normally-open momentary contact switch works. If it has two terminals and closes a circuit when pressed, it will work.

### Arcade Button Quick-Connect Wires — 0.25" (6.3mm)

Slip directly onto the spade terminals of arcade buttons — no crimping or soldering needed. Two per button (one for signal, one for ground).

- Adafruit — [link TBD]
- Search: *"Arcade Button and Switch Quick-Connect Wires 0.25"*

### Wire

**22 AWG stranded hookup wire.** Buy two colors — one for signal, one for ground.

- Flexible enough to route through an enclosure
- Seats cleanly in screw terminals
- 24 AWG works too; avoid solid-core (fatigues at screw terminals)

### Ground Bus

All button ground wires meet at a ground bus, then a single wire runs from the bus to a GND terminal on the HAT.

- [Square D 9-Terminal Ground Bar Kit (Home Depot)](https://www.homedepot.com/p/Square-D-9-Terminal-Ground-Bar-Kit-for-QO-Homeline-Electrical-Panel-Load-Center-PK9GTACP/100161420) — solid, screw-terminal ground bar; more than enough for 7 buttons
- Search: *"barrier strip terminal block"* for smaller alternatives

### Ferrules + Crimper (optional but recommended)

Crimp a ferrule onto each wire end before inserting into a screw terminal. Prevents fraying and makes re-termination clean.

- **Size:** 0.5 mm² for 22 AWG
- Search: *"ferrule crimper kit"* — kits typically include the crimper and an assortment of sizes

### Tools

- Wire stripper (22–24 AWG range)
- Small flathead screwdriver (for screw terminal HAT)
- Ferrule crimper (if using ferrules)
