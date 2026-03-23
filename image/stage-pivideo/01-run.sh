#!/bin/bash -e
OVERLAY="$(cd "$(dirname "${BASH_SOURCE[0]}")/../rootfs-overlay" && pwd)"
echo "==> Installing PiVideo overlay..."
rsync -a --chown=root:root "$OVERLAY/" "$ROOTFS_DIR/"
chmod 755 "$ROOTFS_DIR/usr/local/bin/pivideo-daemon"
chmod 700 "$ROOTFS_DIR/usr/local/bin/pivideo-firstrun.sh"
echo "==> PiVideo overlay installed."
