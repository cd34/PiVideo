# Developer Notes

## How It Works

A Rust daemon runs on the Pi and reads `config.json` for a media library (up to 10 files). Media not assigned to a button loops as a kiosk slideshow via mpv's playlist mode. Media assigned to a button plays fullscreen when the corresponding GPIO pin goes low. The web UI writes the same `config.json`, so changes are picked up immediately without restarting anything.

### Kiosk rotation

Media entries without a button assignment form the kiosk playlist. The daemon loads them into mpv with `loop-playlist=inf`. Videos play to completion; images display for 10 seconds (controlled by mpv's `--image-display-duration`). If the playlist is empty, the screen stays black.

### Button press behavior

Pressing a button always interrupts whatever is currently on screen — whether that is the kiosk slideshow or a video from a different button — and immediately starts the assigned video. When the video finishes, the kiosk playlist resumes from the beginning.

### Kiosk-only mode

If no media entries are assigned to buttons, the daemon skips GPIO initialization entirely and runs as a pure kiosk slideshow.

### Repository layout

```
daemon/       Rust GPIO daemon (pivideo-daemon)
image/        Scripts and overlays for building the Pi SD card image
web/          Python web UI for uploading and managing videos
```

### Components

**Daemon (`daemon/`)** — Rust binary that:
- Reads media library and button assignments from `config.json`
- Loads unassigned media into mpv as a looping kiosk playlist
- Polls GPIO pins every 10ms; plays the assigned video fullscreen on button press
- Waits for button release before resuming (debounce)
- Returns to kiosk playlist when button video finishes
- Skips GPIO entirely in kiosk-only mode (no button assignments)
- Runs as a systemd service, starts on boot

**Web UI (`web/`)** — Lightweight Python web server (no dependencies beyond stdlib) that:
- Shows a media library (up to 10 files) with button assignment dropdowns
- Accepts image and video uploads; manages assignments via `/upload`, `/assign`, `/delete`
- Automatically migrates v1 config format to v2 on first load
- Displays a reboot notice when a system update requires one
- Starts on boot via systemd, accessible at `http://<hostname>.local:8080`

---

## Building from Source

**No special hardware is required to build, test, or release PiVideo.** The entire build pipeline runs on GitHub Actions — forking the repo and pushing a tag is enough to produce a flashable `.img.xz`.

### Fork and deploy your own copy

1. **Fork** this repo on GitHub.

2. **Generate a GPG signing key** (needed to sign the apt repo):
   ```bash
   bash scripts/gen-signing-key.sh
   ```
   Follow the printed instructions:
   - Store the private key in your fork's **Settings → Secrets and variables → Actions** as `GPG_PRIVATE_KEY`
   - The public key is written to `image/rootfs-overlay/etc/apt/trusted.gpg.d/pivideo.asc` — commit it
   - Copy the fingerprint into `reprepro/conf/distributions`, replacing `FILL_IN_KEY_FINGERPRINT`
   ```bash
   git add image/rootfs-overlay/etc/apt/trusted.gpg.d/pivideo.asc reprepro/conf/distributions
   git commit -m "Add apt signing key"
   git push
   ```

3. **Enable GitHub Pages** in your fork's **Settings → Pages** — set source to the `gh-pages` branch.

4. **Tag a release** to trigger the build:
   ```bash
   echo "1.0.0" > VERSION
   git add VERSION && git commit -m "Release v1.0.0"
   git tag v1.0.0
   git push && git push --tags
   ```

GitHub Actions cross-compiles the daemon, builds the `.deb`, downloads the latest Raspberry Pi OS Lite, injects the overlay, compresses the image, publishes the apt repo to `gh-pages`, and creates a GitHub Release — all automatically. No Pi, no local toolchain required.

5. **Add your fork's os_list.json to Pi Imager** (App Options → Content Repository):
   ```
   https://<your-github-username>.github.io/<your-repo-name>/os_list.json
   ```

### Running tests locally

Tests only require Rust and Python 3 — no Pi hardware needed.

```bash
make test     # cargo test (daemon) + python3 web/test_server.py
```

### Local build commands

```bash
make build    # compile daemon for host (development only)
make deb      # build pivideo_VERSION_arm64.deb (requires cross-toolchain + dpkg-dev)
make image    # cross-compile, build .deb, and assemble rootfs overlay
make clean    # clean build artifacts
```

`make deb` and `make image` require the aarch64 cross-compilation toolchain — see [Build Requirements](#build-requirements) below. These are optional; CI handles the full build on every tag.

### Releasing updates to deployed kiosks

PiVideo uses a self-hosted apt repository on GitHub Pages. Deployed kiosks receive updates automatically via `unattended-upgrades` — no reflashing required. Push a new tag and CI handles everything.

For local testing with a baked-in WiFi/hostname (skips Pi Imager):

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

## Build Requirements

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
3. **Python 3** — install via your distro package manager if not already present.

### Windows

Cross-compiling for aarch64 Linux is not natively supported on Windows. Use one of these approaches:

- **WSL 2 (recommended)** — install Ubuntu via the Microsoft Store, then follow the Linux instructions above inside the WSL environment.
- **Docker** — run the build inside a Linux container with the cross-compilation toolchain installed.

For development without cross-compiling, install Rust for Windows from [rustup.rs](https://rustup.rs/) and use WSL for the cross-compilation step.
