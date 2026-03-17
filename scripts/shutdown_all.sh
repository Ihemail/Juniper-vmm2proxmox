
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$DIR/scripts/shutdown_all.py" --config "$DIR/config.yaml" --state "$DIR/state" "$@"
