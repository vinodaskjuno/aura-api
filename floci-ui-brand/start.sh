#!/usr/bin/env bash
#
# Floci's own console, co-branded for Aura.
#
# Aura does NOT start floci-ui — the agent only probes :4500 and links to it if the
# operator is running one. This is that command, with two files bind-mounted over the
# image so the console carries an Aura mark beside Floci's own.
#
# Bind mounts, not a rebuilt image: floci-ui is a separate MIT-licensed product, and
# overlaying two files keeps upgrading it a plain `podman pull`. Floci's own logo,
# wordmark and title are untouched — this adds, it does not replace.
#
#   ./start.sh          start (or restart) the console on :4500
#   ./start.sh --plain  start it unbranded, to compare
#
set -euo pipefail
cd "$(dirname "$0")"

NAME=floci-ui
PORT=4500
# The network the emulators are on, so the console can reach Floci by its alias rather
# than through the host. Created by Aura when it starts an emulator; created here too so
# this works before the first one.
NETWORK=aura-floci

podman network exists "$NETWORK" 2>/dev/null || podman network create "$NETWORK" >/dev/null
podman rm -f "$NAME" >/dev/null 2>&1 || true

mounts=()
if [[ "${1:-}" != "--plain" ]]; then
  # :z relabels for SELinux; harmless on macOS, required on Fedora/RHEL hosts.
  mounts+=(-v "$PWD/index.html:/app/public/index.html:ro,z")
  mounts+=(-v "$PWD/aura-mark.svg:/app/public/assets/aura-mark.svg:ro,z")
fi

podman run -d --name "$NAME" \
  --network "$NETWORK" \
  -p "$PORT:4500" \
  -e FLOCI_ENDPOINT="http://floci:4566" \
  "${mounts[@]+"${mounts[@]}"}" \
  docker.io/floci/floci-ui:latest >/dev/null

echo "Floci UI on http://localhost:$PORT"
[[ "${1:-}" == "--plain" ]] && echo "  (unbranded)" || echo "  (with the Aura mark)"
