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
#   ./start.sh                        the DEFAULT account — see the warning below
#   ./start.sh --project <id>         scoped to one Aura project (what you usually want)
#   ./start.sh --account <12 digits>  scoped to an account you already know
#   ./start.sh --plain                unbranded, to compare
#
# WHY --project EXISTS
# --------------------
# One emulator is shared by every project on the machine, and they are kept apart INSIDE
# it by AWS account: `emulators.account_for()` derives a 12-digit account from the
# project id, and a populate provisions into that account. Floci reads a 12-digit access
# key as the account whose resources to return.
#
# Started with no account, the console renders perfectly and reports EVERY resource page
# empty, including Serverless, for a project whose Lambdas are up and answering. That is
# the one failure this flag exists to prevent, so the account in use is printed on every
# start.
#
# THE ACCOUNT HAS TO BE SET IN TWO PLACES, and only one of them is the container env:
#
#   1. AWS_ACCESS_KEY_ID  — used by the console's SERVER when a request carries no
#      account header. That is every direct API call (curl, scripts), and NOT the
#      browser.
#   2. localStorage `floci.accountId` — the console's own account picker, sent as the
#      `x-floci-account-id` header on every request the PAGE makes. It defaults to
#      000000000000 and OVERRIDES the env var above.
#
# So setting only the env var fixes curl and leaves the console exactly as empty as
# before — the trap this script exists to avoid, and one that verifies as fixed if you
# check it with curl. The seed injected into index.html below is what makes the browser
# agree. It only fills in an unset or default value, so picking an account by hand in
# the console's own ACCOUNT menu still wins.
#
# Floci ALSO starts a floci-ui sidecar of its own, on this same name and port, the first
# time it needs a container — and that one gets the default account. So if the console
# goes empty again after the emulator was stopped and started, it is Floci's sidecar you
# are looking at, not this one: re-run this script.
#
# `--project` may also be given as AURA_PROJECT_ID in the environment.
set -euo pipefail
cd "$(dirname "$0")"

NAME=floci-ui
PORT=4500
# The network the emulators are on, so the console can reach Floci by its alias rather
# than through the host. Created by Aura when it starts an emulator; created here too so
# this works before the first one.
NETWORK=aura-floci
# Floci's default. Correct only for a console that genuinely has no project.
DEFAULT_ACCOUNT=000000000000

plain=""
project="${AURA_PROJECT_ID:-}"
account=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plain)   plain=1; shift ;;
    --project) project="${2:-}"; shift 2 ;;
    --account) account="${2:-}"; shift 2 ;;
    # The header block IS the help, read back rather than restated — a usage string
    # kept separately is one that stops matching the flags below.
    -h|--help) awk 'NR>2 && /^#/ {sub(/^# ?/, ""); print; next} NR>2 {exit}' "$0"
               exit 0 ;;
    *)         echo "unknown argument: $1" >&2
               echo "usage: $0 [--project <id> | --account <12 digits>] [--plain]" >&2
               exit 2 ;;
  esac
done

if [[ -n "$project" && -n "$account" ]]; then
  echo "pass --project or --account, not both" >&2
  exit 2
fi

# Derived by calling Aura's OWN function, never by reimplementing the hash here. Two
# definitions of which account a project lives in is two accounts to look in, and the
# symptom of getting it wrong — an empty console — looks nothing like a mismatch.
if [[ -n "$project" ]]; then
  python=""
  for candidate in "../.venv/bin/python" "$(command -v python3 || true)"; do
    [[ -n "$candidate" && -x "$candidate" ]] && { python="$candidate"; break; }
  done
  if [[ -z "$python" ]]; then
    echo "no python found to derive the account for project $project." >&2
    echo "Pass the 12-digit account directly with --account instead." >&2
    exit 1
  fi
  account="$(PYTHONPATH=.. "$python" -c \
    'import sys; from src.qatest.emulators import account_for; print(account_for(sys.argv[1]))' \
    "$project")" || {
      echo "could not derive the account for project $project" >&2
      exit 1
    }
fi

account="${account:-$DEFAULT_ACCOUNT}"

# Refused, not silently accepted. Floci treats anything that is not exactly 12 digits as
# "use the default account", so a typo here would land the console back on the empty
# namespace this flag exists to avoid — and it would look like a Floci bug, not a typo.
if [[ ! "$account" =~ ^[0-9]{12}$ ]]; then
  echo "account must be exactly 12 digits, got: $account" >&2
  exit 2
fi

podman network exists "$NETWORK" 2>/dev/null || podman network create "$NETWORK" >/dev/null
podman rm -f "$NAME" >/dev/null 2>&1 || true

mounts=()
if [[ -z "$plain" ]]; then
  page="$PWD/index.html"
  # Seed the console's account picker, so the BROWSER asks for this project's account
  # rather than the default one. Rendered to a copy — index.html stays the canonical
  # file, and the account never gets committed into it.
  if [[ "$account" != "$DEFAULT_ACCOUNT" ]]; then
    mkdir -p .rendered
    seed="<script>(function(){try{var k='floci.accountId',v=localStorage.getItem(k);"
    seed+="if(!v||v==='$DEFAULT_ACCOUNT')localStorage.setItem(k,'$account');}catch(e){}})();</script>"
    # Matched on the marker comment, and the result CHECKED: a silent no-match here
    # would leave a console that looks scoped, prints the right account, and shows
    # nothing — which is the failure this whole flag exists to remove.
    awk -v seed="$seed" '/AURA_ACCOUNT_SEED/ { print seed; found=1; next }
                         /^ *Left as a comment otherwise/ { next }
                         { print }
                         END { exit found ? 0 : 1 }' index.html > .rendered/index.html || {
      echo "index.html has no AURA_ACCOUNT_SEED marker — cannot seed the account." >&2
      echo "Set it by hand in the console's ACCOUNT menu: $account" >&2
      exit 1
    }
    page="$PWD/.rendered/index.html"
  fi
  # :z relabels for SELinux; harmless on macOS, required on Fedora/RHEL hosts.
  mounts+=(-v "$page:/app/public/index.html:ro,z")
  mounts+=(-v "$PWD/aura-mark.svg:/app/public/assets/aura-mark.svg:ro,z")
fi

podman run -d --name "$NAME" \
  --network "$NETWORK" \
  -p "$PORT:4500" \
  -e FLOCI_ENDPOINT="http://floci:4566" \
  -e AWS_ACCESS_KEY_ID="$account" \
  -e AWS_SECRET_ACCESS_KEY=test \
  -e AWS_REGION=us-east-1 \
  "${mounts[@]+"${mounts[@]}"}" \
  docker.io/floci/floci-ui:latest >/dev/null

echo "Floci UI on http://localhost:$PORT"
[[ -n "$plain" ]] && echo "  (unbranded)" || echo "  (with the Aura mark)"
if [[ "$account" == "$DEFAULT_ACCOUNT" ]]; then
  echo "  account $account (default) — Aura-populated projects will look EMPTY here."
  echo "  Re-run with --project <id> to see one project's resources."
elif [[ -n "$plain" ]]; then
  # --plain drops the branded index.html, and the account seed rides in it. Said out
  # loud because the console would otherwise report the default account while this
  # script printed the project's.
  echo "  account $account — server side only: --plain drops the page that seeds the"
  echo "  console's ACCOUNT menu, so set $account there by hand."
else
  echo "  account $account${project:+  (project $project)}"
  echo "  The console's ACCOUNT menu is seeded to match. If it still reads"
  echo "  0000-0000-0000, pick $account there — a value you chose before wins."
fi
