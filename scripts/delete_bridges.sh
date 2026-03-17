
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$DIR/scripts/delete_bridges.py" --config "$DIR/config.yaml" --state-dir "$DIR/state" "$@"
