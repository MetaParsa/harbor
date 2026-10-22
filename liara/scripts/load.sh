#!/usr/bin/env bash
# Steady traffic for the demo: mostly shoppers, some image previews (v3's /resize).
# Usage: bash liara/scripts/load.sh https://demo-shop.liara.run [requests_per_second=2] [seconds=180]
# Prints one line per request: status, seconds, path — so the slowdown is visible live while filming.
set -u
URL="${1:?usage: load.sh <base-url> [rps] [seconds]}"
RPS="${2:-2}"
SECONDS_TOTAL="${3:-180}"
PATHS=("/" "/checkout?id=1" "/api/products" "/resize?id=2" "/resize?id=3")
SLEEP=$(awk "BEGIN { print 1 / $RPS }")
END=$(( $(date +%s) + SECONDS_TOTAL ))
i=0
while [ "$(date +%s)" -lt "$END" ]; do
  P="${PATHS[$(( i % ${#PATHS[@]} ))]}"
  ( curl -s -o /dev/null -m 20 -w "%{http_code} %{time_total}s $P\n" "$URL$P" || echo "ERR - $P" ) &
  i=$(( i + 1 ))
  sleep "$SLEEP"
done
wait
