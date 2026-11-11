#!/usr/bin/env bash
# Starts the Halcyon concierge service.
# Requires PORT and AGENT_TOKEN in the environment. Optional: ANTHROPIC_API_KEY,
# HALCYON_MODEL, HALCYON_DATA_DIR, CLAUDE_BIN.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -u -m halcyon
