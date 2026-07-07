#!/usr/bin/env bash
# Print a Claude Science (CS / operon) login URL, rewritten to 127.0.0.1.
#
# Why the rewrite: CS binds IPv4-only, so `localhost` (which resolves to ::1 first
# on macOS) refuses the connection. The daemon prints links as http://localhost:PORT/…;
# we must navigate the automation tab to the 127.0.0.1 form instead.
#
# The link is a daemon-scoped magic link: single-use and ~3-minute expiry. Generate
# it immediately before you navigate to it, then click the "Sign in" button on the
# page. A daemon restart expires any existing login (and any unused link).
#
# Env: CS_BIN overrides the launcher path (default ~/.local/bin/claude-science).
set -euo pipefail

BIN="${CS_BIN:-$HOME/.local/bin/claude-science}"
if [ ! -x "$BIN" ]; then
  BIN="$(command -v claude-science 2>/dev/null || command -v operon 2>/dev/null || true)"
fi
[ -n "$BIN" ] || { echo "claude-science/operon launcher not found; set CS_BIN" >&2; exit 1; }

if ! "$BIN" status 2>/dev/null | grep -qE '"running"[[:space:]]*:[[:space:]]*true'; then
  echo "CS daemon is not running. Start it with: $BIN serve" >&2
  exit 1
fi

raw="$("$BIN" url 2>/dev/null || true)"
url="$(printf '%s\n' "$raw" | grep -oE 'https?://[^[:space:]]+' | grep -i 'nonce=' | head -1)"
[ -n "$url" ] || { echo "no login URL from '$BIN url' (daemon up but no link?)" >&2; exit 1; }

# rewrite only the host; keep port/path/nonce intact
printf '%s\n' "${url/localhost/127.0.0.1}"
