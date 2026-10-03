#!/bin/sh
# Applies the SRT latency in config/srt.env: recreates the srs service when the value it is running with differs
# from the file (a crash restart keeps the old env; only `compose up -d` re-reads env_file, so compare, don't guess).
# Run it from cron every minute (`* * * * * /path/to/delay_relay/scripts/apply-srt.sh`), a systemd timer, or by hand
# after `PUT :9090/srs/srt-latency?ms=N`. Podman: COMPOSE="podman compose" in the environment. The cron user needs
# access to the container engine; compose reads .env from the project directory (the cd below).
set -e
cd "$(dirname "$0")/.."
COMPOSE="${COMPOSE:-docker compose}"
cfg="${CONFIG_DIR:-$(sed -n 's/^CONFIG_DIR=//p' .env 2>/dev/null | head -1 | sed "s/[\"']//g")}"   # same CONFIG_DIR compose uses (quotes stripped)
f="${cfg:-./config}/srt.env"
[ -f "$f" ] || exit 0
want=$(sed -n 's/^SRS_SRT_SERVER_LATENCY=//p' "$f" | head -1)
have=$($COMPOSE exec -T srs sh -c 'echo "$SRS_SRT_SERVER_LATENCY"' 2>/dev/null | tr -d '[:space:]' || true)
[ -n "$have" ] || exit 0                                     # srs not running (no value) = nothing to apply; do not wake it
[ -n "$want" ] && [ "$want" != "$have" ] || exit 0          # already running with this value
if out=$($COMPOSE up -d srs 2>&1); then
    echo "srs recreated with SRS_SRT_SERVER_LATENCY=$want (was $have)"
else
    echo "apply-srt: compose up -d srs failed: $(printf '%s' "$out" | tail -1)" >&2
    exit 1
fi
