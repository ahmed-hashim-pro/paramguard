#!/usr/bin/env bash
# Fly the link-loss run in PX4 SITL once per parameter file, logging
# telemetry for each.
#
#   sitl/run.sh <out_dir> <name>=<params file> [<name>=<params file> ...]
#
# Needs Docker and the paramguard-sitl:dev image (make sitl-image). PX4 is
# restarted for each run, so every run starts from firmware defaults and only
# the parameter file differs.
set -euo pipefail

out=$(mkdir -p "$1" && cd "$1" && pwd)
shift
repo=$(cd "$(dirname "$0")/.." && pwd)
px4_image=${PX4_IMAGE:-jonasvautherin/px4-gazebo-headless:1.15.4}
client_image=${CLIENT_IMAGE:-paramguard-sitl:dev}

cleanup() { docker rm -f pg-px4 >/dev/null 2>&1 || true; }
trap cleanup EXIT

for spec in "$@"; do
  name=${spec%%=*}
  file=${spec#*=}
  echo "== $name ($file)"
  cleanup
  # -t: PX4's shell spins on a prompt without a terminal and floods the log.
  docker run -d -t --name pg-px4 --network host "$px4_image" 127.0.0.1 127.0.0.1 >/dev/null
  for _ in $(seq 120); do
    docker logs pg-px4 2>&1 | grep -q "Ready for takeoff" && break
    sleep 2
  done
  docker logs pg-px4 2>&1 | grep -q "Ready for takeoff" || { echo "PX4 did not become ready"; exit 1; }
  docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$repo":/repo:ro -v "$out":/out "$client_image" \
    python3 /repo/sitl/fly.py --params "/repo/$file" --log "/out/$name.csv" --events "/out/$name.json"
  docker logs pg-px4 > "$out/$name-px4.log" 2>&1 || true
done
