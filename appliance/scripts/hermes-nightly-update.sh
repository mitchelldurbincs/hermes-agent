#!/usr/bin/env bash
# Only the receipt consumer may promote; no fallback merge or gateway deployment.
set -euo pipefail
exec /usr/bin/python3 /home/aspect/appliance/scripts/nightly_crew_promote.py \
  --config /home/aspect/appliance/nightly/config.json "$@"
