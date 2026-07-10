#!/usr/bin/env bash
# Agentic review driver (SEARCH-DESIGN §6, docs/OPERATIONS.md §4a).
#
# For every saved search that is due AND carries an `agent_prompt`, build its
# read-only wake bundle and hand it to your agent. This is the optional agentic
# periphery beside the deterministic `brief` loop — it never sends alerts
# itself; the agent decides what (if anything) to do with each bundle.
#
# AGENT_CMD is USER-SPECIFIC: set it to however you invoke your agent, reading
# the wake bundle JSON on stdin — e.g.
#   AGENT_CMD='hermes run --skill flight-deals-review'
#   AGENT_CMD='claude -p "review this saved flight search"'
# If AGENT_CMD is unset the bundle is just printed (dry-run), so you can inspect
# the loop before wiring an agent in.
set -euo pipefail

FLIGHT_DEALS="${FLIGHT_DEALS:-flight-deals}"
AGENT_CMD="${AGENT_CMD:-}"

# `searches due --agentic` prints {"due": [names...], ...}; extract the names
# without assuming jq is installed.
due_json="$("$FLIGHT_DEALS" searches due --agentic)"
names="$(printf '%s' "$due_json" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["due"]))')"

if [ -z "${names//[$'\t\r\n ']/}" ]; then
  echo "agentic-wake: nothing due" >&2
  exit 0
fi

while IFS= read -r name; do
  [ -z "$name" ] && continue
  echo "agentic-wake: waking '$name'" >&2
  bundle="$("$FLIGHT_DEALS" wake "$name")"
  if [ -n "$AGENT_CMD" ]; then
    printf '%s' "$bundle" | eval "$AGENT_CMD"
  else
    # Dry-run: no agent configured, just show the bundle.
    printf '%s\n' "$bundle"
  fi
done <<< "$names"
