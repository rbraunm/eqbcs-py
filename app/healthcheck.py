#!/usr/bin/env python3
import os, socket, sys, time, datetime

# Constants (drinkingCamelCaseWithABBR)
DefaultPortRangeStart = 22112
DefaultServerCount = 1
CheckHost = "127.0.0.1"
ConnectTimeoutSec = 0.5
RetryCount = 1          # quick second try in case a process is just binding

def log(msg: str) -> None:
  ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
  print(f'[{ts}] {msg}', flush=True)

def is_listening(host: str, port: int, timeout: float) -> bool:
  try:
    with socket.create_connection((host, port), timeout=timeout):
      return True
  except OSError:
    return False

def main() -> int:
  try:
    basePort = int(os.environ.get('EQBCS_PORT_RANGE_START', str(DefaultPortRangeStart)))
  except ValueError:
    basePort = DefaultPortRangeStart

  try:
    serverCount = int(os.environ.get('EQBCS_SERVER_COUNT', str(DefaultServerCount)))
  except ValueError:
    serverCount = DefaultServerCount

  # Enforce at least one expected instance
  serverCount = max(1, serverCount)

  ports = [basePort + i for i in range(serverCount)]

  notListening = []
  for p in ports:
    ok = is_listening(CheckHost, p, ConnectTimeoutSec)
    if not ok and RetryCount > 0:
      time.sleep(0.2)
      ok = is_listening(CheckHost, p, ConnectTimeoutSec)
    if not ok:
      notListening.append(p)

  if notListening:
    log(f"Unhealthy: not listening on ports {notListening}")
    return 1

  # healthy
  # (keep output short for Docker healthcheck)
  print("Healthy", flush=True)
  return 0

if __name__ == '__main__':
  raise SystemExit(main())
