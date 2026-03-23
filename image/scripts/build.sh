#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OVERLAY="$REPO_ROOT/image/rootfs-overlay"
CONFIG="$REPO_ROOT/image/config.env"
DAEMON_BIN="$REPO_ROOT/daemon/target/aarch64-unknown-linux-gnu/release/pivideo-daemon"

# ── Optional config (for local dev / CI only) ─────────────────────────────
# For distribution builds, leave config.env absent. Pi Imager will ask the
# end user for hostname, username/password, WiFi, and SSH at flash time.
if [[ -f "$CONFIG" ]]; then
  echo "==> config.env found — baking in deployment settings"
  # shellcheck source=/dev/null
  source "$CONFIG"
  WIFI_COUNTRY="${WIFI_COUNTRY:-US}"
  _HAS_CONFIG=true
else
  echo "==> No config.env — building generic image (Pi Imager sets hostname/WiFi/user)"
  _HAS_CONFIG=false
fi

# ── Build daemon ───────────────────────────────────────────────────────────
echo "==> Building daemon for aarch64..."
cd "$REPO_ROOT/daemon"
cargo build --release --target aarch64-unknown-linux-gnu

echo "==> Copying daemon binary..."
mkdir -p "$OVERLAY/usr/local/bin"
cp "$DAEMON_BIN" "$OVERLAY/usr/local/bin/pivideo-daemon"

# ── Web UI ─────────────────────────────────────────────────────────────────
echo "==> Installing web UI..."
mkdir -p "$OVERLAY/opt/pivideo/web"
cp "$REPO_ROOT/web/server.py" "$OVERLAY/opt/pivideo/web/server.py"

# ── Default config.json ────────────────────────────────────────────────────
echo "==> Writing default config.json..."
mkdir -p "$OVERLAY/opt/pivideo/videos"
cat > "$OVERLAY/opt/pivideo/config.json" <<'CONFIGEOF'
{
  "1": {"gpio": 4,  "pin": 7,  "video": null},
  "2": {"gpio": 17, "pin": 11, "video": null},
  "3": {"gpio": 22, "pin": 15, "video": null},
  "4": {"gpio": 23, "pin": 16, "video": null},
  "5": {"gpio": 24, "pin": 18, "video": null},
  "6": {"gpio": 25, "pin": 22, "video": null},
  "7": {"gpio": 27, "pin": 13, "video": null}
}
CONFIGEOF

# ── First-run script ───────────────────────────────────────────────────────
# Always: dist-upgrade on first boot.
# With config.env: also create the admin user (for local dev / CI builds).
# Pi Imager builds: Pi Imager injects its own firstrun for user/hostname/WiFi.
echo "==> Generating first-run script..."

cat > "$OVERLAY/usr/local/bin/pivideo-firstrun.sh" <<'FIRSTRUN_HEADER'
#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

echo "==> PiVideo first-run: updating system..."
apt-get update -qq
apt-get -y \
  -o Dpkg::Options::="--force-confdef" \
  -o Dpkg::Options::="--force-confold" \
  dist-upgrade

FIRSTRUN_HEADER

if [[ "$_HAS_CONFIG" == true ]]; then
  : "${ADMIN_USER:?ADMIN_USER must be set in config.env}"
  : "${ADMIN_PASSWORD:?ADMIN_PASSWORD must be set in config.env}"
  HASHED_PW=$(openssl passwd -6 "$ADMIN_PASSWORD")

  cat >> "$OVERLAY/usr/local/bin/pivideo-firstrun.sh" <<EOF
echo "==> Creating admin user: $ADMIN_USER"
id -u $ADMIN_USER &>/dev/null || useradd -m -s /bin/bash $ADMIN_USER
usermod -aG sudo,video,gpio,netdev $ADMIN_USER 2>/dev/null || true
printf '%s:%s\n' '$ADMIN_USER' '$HASHED_PW' | chpasswd -e
EOF
fi

cat >> "$OVERLAY/usr/local/bin/pivideo-firstrun.sh" <<'FIRSTRUN_FOOTER'
echo "==> PiVideo first-run complete."
systemctl disable pivideo-firstrun
rm -f /usr/local/bin/pivideo-firstrun.sh
FIRSTRUN_FOOTER

chmod 700 "$OVERLAY/usr/local/bin/pivideo-firstrun.sh"

# ── Deployment settings (config.env only) ─────────────────────────────────
if [[ "$_HAS_CONFIG" == true ]]; then
  : "${HOSTNAME:?HOSTNAME must be set in config.env}"
  : "${WIFI_SSID:?WIFI_SSID must be set in config.env}"
  : "${WIFI_PASSWORD:?WIFI_PASSWORD must be set in config.env}"

  echo "==> Setting hostname: $HOSTNAME"
  echo "$HOSTNAME" > "$OVERLAY/etc/hostname"
  mkdir -p "$OVERLAY/etc"
  cat > "$OVERLAY/etc/hosts" <<EOF
127.0.0.1       localhost
127.0.1.1       $HOSTNAME

::1             localhost ip6-localhost ip6-loopback
ff02::1         ip6-allnodes
ff02::2         ip6-allrouters
EOF

  echo "==> Configuring WiFi: $WIFI_SSID"
  mkdir -p "$OVERLAY/etc/wpa_supplicant"
  cat > "$OVERLAY/etc/wpa_supplicant/wpa_supplicant.conf" <<EOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=$WIFI_COUNTRY

network={
    ssid="$WIFI_SSID"
    psk="$WIFI_PASSWORD"
    key_mgmt=WPA-PSK
}
EOF
  chmod 600 "$OVERLAY/etc/wpa_supplicant/wpa_supplicant.conf"

  NM_DIR="$OVERLAY/etc/NetworkManager/system-connections"
  mkdir -p "$NM_DIR"
  NM_UUID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())")
  cat > "$NM_DIR/pivideo-wifi.nmconnection" <<EOF
[connection]
id=pivideo-wifi
uuid=$NM_UUID
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid=$WIFI_SSID

[wifi-security]
auth-alg=open
key-mgmt=wpa-psk
psk=$WIFI_PASSWORD

[ipv4]
method=auto

[ipv6]
addr-gen-mode=stable-privacy
method=auto
EOF
  chmod 600 "$NM_DIR/pivideo-wifi.nmconnection"

  echo "==> Enabling SSH..."
  mkdir -p "$OVERLAY/etc/systemd/system/multi-user.target.wants"
  ln -sf /lib/systemd/system/ssh.service \
      "$OVERLAY/etc/systemd/system/multi-user.target.wants/ssh.service"
fi

# ── Enable PiVideo services ────────────────────────────────────────────────
echo "==> Enabling services..."
mkdir -p "$OVERLAY/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/pivideo-firstrun.service \
    "$OVERLAY/etc/systemd/system/multi-user.target.wants/pivideo-firstrun.service"
ln -sf /etc/systemd/system/pivideo.service \
    "$OVERLAY/etc/systemd/system/multi-user.target.wants/pivideo.service"
ln -sf /etc/systemd/system/pivideo-web.service \
    "$OVERLAY/etc/systemd/system/multi-user.target.wants/pivideo-web.service"

echo ""
echo "==> Done. Run pi-gen from image/ to produce the final .img"
if [[ "$_HAS_CONFIG" == true ]]; then
  echo "    Access at http://${HOSTNAME}.local:8080"
else
  echo "    Flash with Raspberry Pi Imager and set hostname/WiFi/user in OS Customisation."
fi
