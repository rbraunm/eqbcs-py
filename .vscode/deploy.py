import os
import sys
import subprocess
import datetime

def log(message):
  print(f"[{datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}] {message}")

def ensure(module, pipName=None):
  try:
    __import__(module)
  except ImportError:
    pipName = pipName or module
    log(f"Module '{module}' not found. Installing '{pipName}'...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", pipName])
    log(f"'{pipName}' installed successfully.\n")

ensure("paramiko")
ensure("scp")
ensure("keyring")

import paramiko
import getpass
import keyring
from scp import SCPClient

# Deployment targets
SSH_HOST = "10.111.111.211"
SSH_USER = "eqemu"
REMOTE_PATH = "eqbcs"
CRED_SERVICE = f"deploy:{SSH_USER}@{SSH_HOST}"

# Upload filters
SKIP_NAMES = {"README.md"}                # exact filename blocks
SKIP_SUFFIXES = {".code-workspace"}       # suffix-based blocks (generalizes pok.code-workspace)
SKIP_DIRS = {"node_modules", "__pycache__", "dist", "build", "venv", ".venv"}  # pruned during walk

def getPassword():
  return keyring.get_password(CRED_SERVICE, SSH_USER)

def promptAndStorePassword():
  pw = getpass.getpass(f"Enter SSH password for {SSH_USER}@{SSH_HOST}: ")
  keyring.set_password(CRED_SERVICE, SSH_USER, pw)
  return pw

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

password = getPassword()
for attempt in range(3):
  try:
    if not password:
      password = promptAndStorePassword()
    ssh.connect(SSH_HOST, username=SSH_USER, password=password)
    break
  except paramiko.AuthenticationException:
    log("Authentication failed.")
    if attempt == 2:
      sys.exit("Too many failed attempts. Exiting.")
    keyring.delete_password(CRED_SERVICE, SSH_USER)
    password = None
  except Exception as e:
    sys.exit(f"SSH connection error: {e}")

log(f"Preparing ~/{REMOTE_PATH} on remote...")

checkCmd = f"test -d ~/{REMOTE_PATH}"
stdin, stdout, stderr = ssh.exec_command(checkCmd)
exitStatus = stdout.channel.recv_exit_status()

if exitStatus == 0:
  log(f"Remote path ~/{REMOTE_PATH} exists. Deleting its contents...")
  # Note: this won't remove dotfiles (like .env) which is usually desirable.
  clearCmd = f"rm -rf ~/{REMOTE_PATH}/*"
  stdin, stdout, stderr = ssh.exec_command(clearCmd)
  errors = stderr.read().decode().strip()
  if errors:
    ssh.close()
    sys.exit(f"Error clearing ~/{REMOTE_PATH}: {errors}")
else:
  log(f"Remote path ~/{REMOTE_PATH} does not exist. Creating it...")
  createCmd = f"mkdir -p ~/{REMOTE_PATH}"
  stdin, stdout, stderr = ssh.exec_command(createCmd)
  errors = stderr.read().decode().strip()
  if errors:
    ssh.close()
    sys.exit(f"Error creating ~/{REMOTE_PATH}: {errors}")

def isValidPath(path):
  parts = os.path.normpath(path).split(os.sep)

  # Disallow any hidden directories in the path (e.g., .git, .vscode)
  for part in parts[:-1]:
    if part.startswith("."):
      return False

  name = parts[-1]

  # Block specific filenames
  if name in SKIP_NAMES:
    return False

  # Block by suffix (e.g., *.code-workspace)
  if any(name.endswith(sfx) for sfx in SKIP_SUFFIXES):
    return False

  return True

log("Uploading files...")
with SCPClient(ssh.get_transport()) as scp:
  createdDirs = set()
  for root, dirs, files in os.walk("."):
    if not isValidPath(root):
      continue

    # Prune directories in-place (skip known dirs and hidden dirs caught by isValidPath)
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS and isValidPath(os.path.join(root, d))]

    for f in files:
      fullPath = os.path.join(root, f)
      if isValidPath(fullPath):
        relPath = os.path.relpath(fullPath, ".")
        remotePath = os.path.join(REMOTE_PATH, relPath).replace("\\", "/")
        remoteDir = os.path.dirname(remotePath)

        if remoteDir and remoteDir not in createdDirs:
          ssh.exec_command(f"mkdir -p ~/{remoteDir}")
          createdDirs.add(remoteDir)

        log(f"Uploading {remotePath}")
        scp.put(fullPath, remote_path=remotePath)

log("Performing docker compose up/down...")
for cmd in ["docker compose down", "docker compose build --no-cache && docker compose up -d"]:
  fullCmd = f"cd ~/{REMOTE_PATH} && {cmd}"
  stdin, stdout, stderr = ssh.exec_command(fullCmd)
  out = stdout.read().decode().strip()
  err = stderr.read().decode().strip()
  if out:
    log(out)
  if err:
    log(err)

ssh.close()
log("Deployment complete.")
