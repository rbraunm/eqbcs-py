#!/usr/bin/env python3
import os, sys, signal, time, subprocess, datetime, socket, shutil
from pathlib import Path

# ---- Fixed / defaults ----
BindAddress = "0.0.0.0"         # always bind all
DefaultPortRangeStart = 22112
DefaultServerCount = 1
CheckIntervalSec = float(os.environ.get("EQBCS_SUP_CHECK_INTERVAL", "2.0"))
MaxBackoffSec = float(os.environ.get("EQBCS_SUP_MAX_BACKOFF", "30"))
InitialWaitSec = float(os.environ.get("EQBCS_SUP_INITIAL_WAIT", "6.0"))  # time to wait after launch for port to come up

EQBCS_ROOT = "/app/eqbcs"
INSTANCES_ROOT = f"{EQBCS_ROOT}/instances"
PREFIXES_ROOT = "/app/wineprefixes"   # per-instance Wine prefixes

def log(msg: str) -> None:
  ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
  print(f'[{ts}] {msg}', flush=True)

def find_exe(root: str) -> str:
  cand = os.path.join(root, 'EQBCS.exe')
  if os.path.isfile(cand):
    return cand
  for p in Path(root).glob('*[Ee][Qq][Bb][Cc][Ss]*.exe'):
    return str(p)
  raise FileNotFoundError('Could not find EQBCS.exe under ' + root)

def write_ini(dest_dir: str, port: int) -> str:
  os.makedirs(dest_dir, exist_ok=True)
  ini_path = os.path.join(dest_dir, 'eqbcs.ini')
  lines = [
    '[EQBCS]',
    f'Address={BindAddress}',
    f'Port={port}',
    # add Password=... here if you want one
  ]
  with open(ini_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines) + '\n')
  return ini_path

def ensure_prefix(prefix_dir: str) -> None:
  """Create a fresh 32-bit Wine prefix if missing (idempotent)."""
  os.makedirs(prefix_dir, exist_ok=True)
  system_reg = os.path.join(prefix_dir, "system.reg")
  if not os.path.isfile(system_reg):
    env = os.environ.copy()
    env['WINEPREFIX'] = prefix_dir
    env['WINEARCH'] = 'win32'  # set ONLY on first creation
    log(f'Initializing Wine prefix at {prefix_dir}...')
    subprocess.run(['wineboot', '--init'], env=env, check=True)

def wine_env(prefix_dir: str):
  env = os.environ.copy()
  env['WINEPREFIX'] = prefix_dir
  # do NOT set WINEARCH after creation
  return env

def kill_prefix_processes(prefix_dir: str) -> None:
  """Hard-stop anything lingering in this prefix."""
  try:
    subprocess.run(['wineserver', '-k'], env=wine_env(prefix_dir), timeout=10)
  except Exception:
    pass

def is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.settimeout(timeout)
    return s.connect_ex((host, port)) == 0

def ensure_service_files(prefix_dir: str, port: int, host_exe_path: str, ini_source_path: str) -> tuple[str, str]:
  """
  Ensure C:\\eqbcs\\<port>\\ contains EQBCS.exe and eqbcs.ini (copied from host paths).
  Returns (bin_path_win, working_dir_win).
  """
  cdrive_root = os.path.join(prefix_dir, 'drive_c')
  app_dir = os.path.join(cdrive_root, 'eqbcs', str(port))
  os.makedirs(app_dir, exist_ok=True)

  exe_dest = os.path.join(app_dir, 'EQBCS.exe')
  ini_dest = os.path.join(app_dir, 'eqbcs.ini')

  # refresh EXE if missing or older; always refresh INI
  try:
    if (not os.path.isfile(exe_dest) or
      os.path.getmtime(host_exe_path) > os.path.getmtime(exe_dest)):
      shutil.copy2(host_exe_path, exe_dest)
    shutil.copy2(ini_source_path, ini_dest)
  except Exception as e:
    raise RuntimeError(f'Failed copying service files to {app_dir}: {e}')

  bin_path_win = f'C:\\eqbcs\\{port}\\EQBCS.exe'
  working_dir_win = f'C:\\eqbcs\\{port}'
  return bin_path_win, working_dir_win

def wine_start_detached(prefix_dir: str, bin_path_win: str, working_dir_win: str, port: int) -> None:
  """
  Launch EQBCS headlessly using wineconsole's TTY backend (no X/desktop).
  Fully detached; output goes to per-port log.
  """
  host_workdir = os.path.join(prefix_dir, 'drive_c', 'eqbcs', str(port))
  os.makedirs(host_workdir, exist_ok=True)

  env = wine_env(prefix_dir)
  # quiet Wine a bit
  env.setdefault('WINEDEBUG', '-all')

  log_path = os.path.join(host_workdir, 'eqbcs.log')
  log_fh = open(log_path, 'ab', buffering=0)

  # Important: use Popen (not run), TTY backend, and don't rely on X/Explorer.
  # Use host cwd mapped to the same "C:\\eqbcs\\<port>" so relative config loads work.
  cmd = ['wineconsole', '--backend=tty', bin_path_win]

  subprocess.Popen(
    cmd,
    env=env,
    cwd=host_workdir,
    stdout=log_fh,
    stderr=subprocess.STDOUT,
    start_new_session=True,   # fully detach from this process group
    close_fds=True
  )

def wait_for_port(port: int, deadline: float) -> bool:
  while time.time() < deadline:
    if is_listening(port):
      return True
    time.sleep(0.1)
  return is_listening(port)

def main() -> int:
  exe = find_exe(EQBCS_ROOT)

  base_port = int(os.environ.get('EQBCS_PORT_RANGE_START', str(DefaultPortRangeStart)))
  count = max(1, int(os.environ.get('EQBCS_SERVER_COUNT', str(DefaultServerCount))))
  desired_ports = [base_port + i for i in range(count)]

  os.makedirs(INSTANCES_ROOT, exist_ok=True)
  os.makedirs(PREFIXES_ROOT, exist_ok=True)

  backoff = {}     # port -> current backoff seconds
  last_try = {}    # port -> last attempt timestamp

  log(f"Starting {count} EQBCS instance(s) starting at port {base_port}...")

  # Graceful shutdown
  def shutdown(_sig=None, _frm=None):
    log('Shutdown requested; terminating EQBCS instances (per-prefix kill)...')
    for port in desired_ports:
      prefix_dir = os.path.join(PREFIXES_ROOT, str(port))
      kill_prefix_processes(prefix_dir)
    sys.exit(0)

  signal.signal(signal.SIGTERM, shutdown)
  signal.signal(signal.SIGINT, shutdown)

  def start_if_needed(port: int):
    # If something already owns the port, nothing to do.
    if is_listening(port):
      return

    now = time.time()
    next_ok = last_try.get(port, 0) + backoff.get(port, 0)
    if now < next_ok:
      return

    inst_dir = os.path.join(INSTANCES_ROOT, str(port))
    prefix_dir = os.path.join(PREFIXES_ROOT, str(port))

    ini_host_path = write_ini(inst_dir, port)
    ensure_prefix(prefix_dir)

    bin_path_win, working_dir_win = ensure_service_files(prefix_dir, port, exe, ini_host_path)

    # Defensive: clear any stale processes in this prefix before (re)starting
    kill_prefix_processes(prefix_dir)

    try:
      bin_path_win, working_dir_win = ensure_service_files(prefix_dir, port, exe, ini_host_path)
      kill_prefix_processes(prefix_dir)
      log(f'Starting EQBCS (tty) on port {port} (prefix={prefix_dir})...')
      wine_start_detached(prefix_dir, bin_path_win, working_dir_win, port)
    except Exception as e:
      log(f'ERROR starting port {port}: {e}')
    else:
      # give it a moment to bind its socket
      if wait_for_port(port, time.time() + InitialWaitSec):
        # Success: nudge backoff downward
        backoff[port] = max(0.0, min(backoff.get(port, 0.0) * 0.5, MaxBackoffSec))
      else:
        # Didn't come up yet; next loop will increase backoff
        pass

    last_try[port] = now

  def schedule_backoff_on_down():
    for port in desired_ports:
      if not is_listening(port):
        new_b = backoff.get(port, 0.0)
        new_b = 1.0 if new_b == 0.0 else min(new_b * 2.0, MaxBackoffSec)
        if backoff.get(port, 0.0) != new_b:
          log(f"Port {port} not listening; scheduling restart with backoff {new_b:.1f}s")
        backoff[port] = new_b

  # Initial pass
  for port in desired_ports:
    start_if_needed(port)

  # Supervisor loop
  while True:
    schedule_backoff_on_down()
    for port in desired_ports:
      start_if_needed(port)
    time.sleep(CheckIntervalSec)

if __name__ == '__main__':
  raise SystemExit(main())
