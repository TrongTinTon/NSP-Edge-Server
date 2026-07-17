#!/usr/bin/env bash
set -euo pipefail
ADDONS_DIR="${1:-/mnt/extra-addons}"
LEGACY_MODULES=(
  nsp_whitelist
)
for m in "${LEGACY_MODULES[@]}"; do
  if [ -d "$ADDONS_DIR/$m" ]; then
    echo "Removing legacy module: $ADDONS_DIR/$m"
    rm -rf "$ADDONS_DIR/$m"
  fi
done

echo "Legacy cleanup completed. Current NSP modules:"
find "$ADDONS_DIR" -maxdepth 1 -type d -printf '%f\n' | sort | grep -E '^(CoreApp|t4_coreapi|nsp_)' || true
