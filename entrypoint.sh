#!/usr/bin/env bash
set -Eeuo pipefail

log() { printf '[%(%F %T)T] %s\n' -1 "$*"; }

# ---- Config (env overridable) ----
EQBCS_BIN="${EQBCS_BIN:-/app/eqbcs/EQBCS.exe}"

BASE_PREFIX="${WINEPREFIX:-/app/wineprefix}"
BOOT_PREFIX="${BOOTSTRAP_WINEPREFIX:-/app/wineprefixes/bootstrap}"

COUNT="${EQBCS_SERVER_COUNT:-1}"
START_PORT="${EQBCS_PORT_RANGE_START:-22112}"
MODE="${EQBCS_MODE:-tty}"            # "tty" (wineconsole curses) or "nox" (wine)
EXTRA_ARGS="${EQBCS_ARGS:-}"         # extra flags to pass to EQBCS if needed

PROBE_HOST="${EQBCS_PROBE_HOST:-127.0.0.1}"
PROBE_TIMEOUT="${EQBCS_PROBE_TIMEOUT:-30}"    # seconds to wait for each port
PROBE_INTERVAL="${EQBCS_PROBE_INTERVAL:-0.5}" # seconds between probes

# ---- Cleanup on exit ----
cleanup() { log "Shutting down..."; kill 0 >/dev/null 2>&1 || true; }
trap cleanup SIGINT SIGTERM

# ---- Sanity checks ----
if [[ ! -f "$EQBCS_BIN" ]]; then
  log "ERROR: EQBCS binary not found at $EQBCS_BIN"
  exit 1
fi

if [[ ! -f "$BOOT_PREFIX/system.reg" ]]; then
  log "ERROR: Bootstrap Wine prefix missing at $BOOT_PREFIX (vcrun6). Rebuild the image."
  exit 1
fi

# Ensure a general runtime prefix exists (useful for tools; instances use per-port)
if [[ ! -f "$BASE_PREFIX/system.reg" ]]; then
  log "Initializing Wine prefix at $BASE_PREFIX..."
  WINEPREFIX="$BASE_PREFIX" wineboot -u
fi

mkdir -p /app/eqbcs/logs

# ---- Helpers ----
wait_until_listening() {
  local host="$1" port="$2" timeout="$3" interval="$4"
  local start end
  start=$(date +%s)
  end=$((start + timeout))
  while true; do
    if bash -c "exec 3<>/dev/tcp/${host}/${port}" >/dev/null 2>&1; then
      exec 3>&- || true
      return 0
    fi
    [[ "$(date +%s)" -ge "$end" ]] && return 1
    sleep "${interval}"
  done
}

start_instance() {
  local port="$1" prefix="/app/wineprefixes/${port}" log_file="/app/eqbcs/logs/eqbcs-${port}.log"

  # Seed per-port prefix from the bootstrap (contains vcrun6)
  if [[ ! -f "$prefix/system.reg" ]]; then
    log "Seeding Wine prefix for port ${port} from ${BOOT_PREFIX} -> ${prefix}..."
    mkdir -p "$prefix"
    cp -a "${BOOT_PREFIX}/." "$prefix/"
  fi

  log "Starting EQBCS on port ${port} (prefix=${prefix})..."
  if [[ "$MODE" == "tty" ]]; then
    ( WINEPREFIX="$prefix" wineconsole --backend=curses "$EQBCS_BIN" -p "${port}" ${EXTRA_ARGS} ) \
      >"$log_file" 2>&1 &
  else
    ( WINEPREFIX="$prefix" wine "$EQBCS_BIN" -p "${port}" ${EXTRA_ARGS} ) \
      >"$log_file" 2>&1 &
  fi
}

# ---- Launch all instances ----
log "Starting ${COUNT} EQBCS instance(s) from port ${START_PORT}..."
p="$START_PORT"
for _ in $(seq 1 "$COUNT"); do
  start_instance "$p"
  p=$((p + 1))
done

# ---- Readiness probes ----
sleep 1
fail=0
p="$START_PORT"
for _ in $(seq 1 "$COUNT"); do
  if wait_until_listening "$PROBE_HOST" "$p" "$PROBE_TIMEOUT" "$PROBE_INTERVAL"; then
    log "✅ Port ${p} is LISTENING"
  else
    log "❌ Port ${p} did NOT start listening within ${PROBE_TIMEOUT}s (see /app/eqbcs/logs/eqbcs-${p}.log)"
    fail=1
  fi
  p=$((p + 1))
done

if [[ "$fail" -ne 0 ]]; then
  log "One or more instances failed readiness. Container will remain up so you can inspect logs."
fi

# ---- Stream logs and keep container in foreground ----
tail -F /app/eqbcs/logs/eqbcs-*.log &
wait -n || true
