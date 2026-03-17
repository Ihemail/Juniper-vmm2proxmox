#!/bin/bash
# Manual cleanup script for Proxmox VMs and bridges
# Usage: ./scripts/manual_cleanup.sh

set -euo pipefail

STATE_DIR="$(dirname "$0")/../state"
CFG="$(dirname "$0")/../config.yaml"
SHUTDOWN_SCRIPT="$(dirname "$0")/shutdown_all.py"
DELETE_BRIDGES_SCRIPT="$(dirname "$0")/delete_bridges.py"

cd "$(dirname "$0")/.."

echo "[MANUAL CLEANUP] Starting manual cleanup..."

if [ -f "$STATE_DIR/started_vmids.json" ]; then
  echo "[MANUAL CLEANUP] Shutting down recorded VMs"
  python3 "$SHUTDOWN_SCRIPT" --config "$CFG" --state "$STATE_DIR" || true
else
  echo "[MANUAL CLEANUP] No started_vmids.json; skipping shutdown"
fi

read -r -t 30 -p "[MANUAL CLEANUP] Delete created vmc_* bridges? (yes/NO) [timeout 30s]: " REPLY || true
DELETE_BR="no"
case "$REPLY" in
  y|Y|yes|YES|Yes) DELETE_BR="yes" ;;
  *) DELETE_BR="no" ;;
esac

if [ "$DELETE_BR" = "yes" ]; then
  if [ -f "$STATE_DIR/created_bridges.json" ]; then
    echo "[MANUAL CLEANUP] Deleting created bridges"
    python3 "$DELETE_BRIDGES_SCRIPT" --config "$CFG" --state-dir "$STATE_DIR" || true
  else
    echo "[MANUAL CLEANUP] No created_bridges.json; skipping bridge delete"
  fi
else
  echo "[MANUAL CLEANUP] Skipping bridge deletion (default No)"
fi

echo "[MANUAL CLEANUP] Cleanup done."
