#!/usr/bin/env bash
set -Eeuo pipefail

: "${EQBCS_PY:=/app/server.py}"
: "${EQBCS_PY_BIND:=0.0.0.0}"
: "${EQBCS_PY_PORT_RANGE_START:=22112}"
: "${EQBCS_PY_SERVER_COUNT:=1}"

pids=()

stop_all() {
  if ((${#pids[@]} > 0)); then
    kill "${pids[@]}" 2>/dev/null || true
    wait || true
  fi
}
trap stop_all SIGTERM SIGINT

run_one() {
  local idx="$1"
  shift
  local port=$((EQBCS_PY_PORT_RANGE_START + idx))
  python3 "$EQBCS_PY" -i "$EQBCS_PY_BIND" -p "$port" "$@" &
  pids+=("$!")
}

main() {
  local count="$EQBCS_PY_SERVER_COUNT"
  if ! [[ "$count" =~ ^[0-9]+$ ]] || (( count < 1 )); then
    echo "ERROR: EQBCS_PY_SERVER_COUNT must be a positive integer (got: $count)" >&2
    exit 2
  fi

  for ((i=0; i<count; i++)); do
    run_one "$i" "$@"
  done

  wait -n || true
  stop_all
}
main "$@"
