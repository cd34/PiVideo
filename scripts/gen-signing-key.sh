#!/usr/bin/env bash
# Generate a GPG signing key for the PiVideo apt repository.
# Run this ONCE before your first release.
#
# After running:
#   1. Copy the "==> PRIVATE KEY" block → GitHub repo Settings → Secrets →
#      Actions → New secret → name: GPG_PRIVATE_KEY
#   2. The public key is written to image/rootfs-overlay/etc/apt/trusted.gpg.d/pivideo.asc
#      Commit that file.
#   3. Copy the fingerprint printed below into reprepro/conf/distributions,
#      replacing FILL_IN_KEY_FINGERPRINT.
#   4. Commit reprepro/conf/distributions and pivideo.asc together.

set -euo pipefail

KEY_NAME="PiVideo Apt Signing Key"
KEY_EMAIL="pivideo-signing@noreply.github.com"
PUBKEY_FILE="image/rootfs-overlay/etc/apt/trusted.gpg.d/pivideo.asc"

cd "$(dirname "$0")/.."

if gpg --list-keys "$KEY_EMAIL" &>/dev/null; then
  echo "A key for $KEY_EMAIL already exists. Delete it first if you want a new one:"
  echo "  gpg --delete-secret-and-public-key \$(gpg --list-keys --with-colons $KEY_EMAIL | awk -F: '/^fpr/{print \$10; exit}')"
  exit 1
fi

echo "==> Generating ed25519 signing key (no passphrase)..."
gpg --batch --gen-key <<EOF
Key-Type: EdDSA
Key-Curve: ed25519
Key-Usage: sign
Name-Real: ${KEY_NAME}
Name-Email: ${KEY_EMAIL}
Expire-Date: 0
%no-passphrase
%commit
EOF

FINGERPRINT=$(gpg --list-keys --with-colons "$KEY_EMAIL" \
  | awk -F: '/^fpr/{print $10; exit}')

echo ""
echo "==> Fingerprint: $FINGERPRINT"

echo "==> Exporting public key → $PUBKEY_FILE"
mkdir -p "$(dirname "$PUBKEY_FILE")"
gpg --armor --export "$FINGERPRINT" > "$PUBKEY_FILE"

echo ""
echo "==> PRIVATE KEY — copy everything between the dashes into GitHub secret GPG_PRIVATE_KEY:"
echo "----"
gpg --armor --export-secret-key "$FINGERPRINT"
echo "----"

echo ""
echo "==> Next steps:"
echo "    1. Copy the private key block above → GitHub repo Settings → Secrets → Actions → GPG_PRIVATE_KEY"
echo "    2. Edit reprepro/conf/distributions: replace FILL_IN_KEY_FINGERPRINT with:"
echo "       $FINGERPRINT"
echo "    3. git add image/rootfs-overlay/etc/apt/trusted.gpg.d/pivideo.asc reprepro/conf/distributions"
echo "    4. git commit -m 'Add apt signing key and repo config'"
echo "    5. git push"
