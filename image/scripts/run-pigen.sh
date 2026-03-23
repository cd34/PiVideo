#!/usr/bin/env bash
set -euo pipefail

PIGEN="${PIGEN_DIR:-$HOME/pi-gen}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STAGE_SRC="$REPO_ROOT/image/stage-pivideo"

if [[ ! -d "$PIGEN" ]]; then
  echo "ERROR: pi-gen not found at $PIGEN"
  echo "Clone it: git clone https://github.com/RPi-Distro/pi-gen.git $PIGEN"
  exit 1
fi

# Skip image generation for base stages; skip stage3/4/5 entirely
for stage in stage0 stage1 stage2; do
  touch "$PIGEN/$stage/SKIP_IMAGES"
done
for stage in stage3 stage4 stage5; do
  touch "$PIGEN/$stage/SKIP"
done

# Link our stage into pi-gen
rm -rf "$PIGEN/stage-pivideo"
ln -sf "$STAGE_SRC" "$PIGEN/stage-pivideo"

# Write pi-gen config
cat > "$PIGEN/config" <<EOF
IMG_NAME="pivideo"
RELEASE="bookworm"
DEPLOY_COMPRESSION=xz
ENABLE_SSH=1
STAGE_LIST="stage0 stage1 stage2 stage-pivideo"
EOF

echo "==> Running pi-gen (this takes 10-20 minutes)..."
cd "$PIGEN"
sudo bash build.sh

echo ""
echo "==> Image ready:"
ls "$PIGEN/deploy/"
