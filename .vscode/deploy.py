import os
import sys
import json
import subprocess
import datetime
import getpass

# Make stdout line-buffered and flush prints so prompts/logs don't smear together
try:
  sys.stdout.reconfigure(line_buffering=True)
except Exception:
  pass

def log(message):
  # Fixed-width timestamp + flush to avoid interleaving with input() prompts
  ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print(f"[{ts}] {message}", flush=True)

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
import keyring
from scp import SCPClient

# ---------------------------
# Profile storage (keyring)
# ---------------------------
profileService = "deploy:profiles"

def getProfileName():
  # Default to current directory name; allow override with env var
  return os.getenv("DEPLOY_PROFILE") or os.path.basename(os.getcwd()) or "default"

def loadProfile(profileName):
  blob = keyring.get_password(profileService, profileName)
  if not blob:
    return None
  try:
    return json.loads(blob)
  except Exception:
    return None  # treat corrupted/legacy as missing

def saveProfile(profileName, profile):
  keyring.set_password(profileService, profileName, json.dumps(profile))

def deleteProfile(profileName):
  try:
    keyring.delete_password(profileService, profileName)
  except Exception:
    pass

def promptProfile(existing=None):
  """Interactive prompt; keeps existing defaults if you hit Enter."""
  p = dict(existing or {})

  def ask(label, default=None, transform=lambda x: x):
    # Ensure the prompt starts on a fresh line in some consoles
    print("", flush=True)
    raw = input(f"{label}{f' [{default}]' if default is not None else ''}: ").strip()
    return transform(raw) if raw else default

  p["host"] = ask("SSH host", p.get("host"))
  p["port"] = ask("SSH port", p.get("port", 22), transform=lambda s: int(s))
  p["user"] = ask("SSH user", p.get("user", getpass.getuser()))
  p["remotePath"] = ask("Remote path under ~", p.get("remotePath", "eqbcs"))

  p["auth"] = (ask("Auth method (password/key)", p.get("auth", "password")) or "password").lower()
  if p["auth"].startswith("p"):
    pw = getpass.getpass("SSH password (leave blank to keep existing): ")
    if pw or not existing:
      p["password"] = pw
    p.pop("keyFile", None)
    p.pop("passphrase", None)
  else:
    p["keyFile"] = ask("Private key file path", p.get("keyFile", os.path.expanduser("~/.ssh/id_rsa")))
    phr = getpass.getpass("Key passphrase (blank for none; blank keeps existing): ")
    if phr or not existing:
      p["passphrase"] = phr or None
    p.pop("password", None)

  return p

def ensureComplete(profile):
  if not profile:
    return False
  baseOk = all(profile.get(k) for k in ("host", "user", "remotePath", "auth"))
  if not baseOk:
    return False
  if profile["auth"] == "password":
    return bool(profile.get("password"))
  return bool(profile.get("keyFile"))

# ---------------------------
# Upload filters
# ---------------------------
skipNames = {"README.md", "buildspec.yaml"}
skipSuffixes = {".code-workspace"}
skipDirs = {"node_modules", "__pycache__", "dist", "build", "venv", ".venv"}

def isValidPath(path):
  parts = os.path.normpath(path).split(os.sep)

  # Disallow any hidden directories in the path (e.g., .git, .vscode)
  for part in parts[:-1]:
    if part.startswith("."):
      return False

  name = parts[-1]

  if name in skipNames:
    return False

  if any(name.endswith(sfx) for sfx in skipSuffixes):
    return False

  return True

# ---------------------------
# Acquire / create profile
# ---------------------------
profileName = getProfileName()
profile = loadProfile(profileName)

if not ensureComplete(profile):
  log(f"No deploy profile found for '{profileName}' (or it is incomplete). Let's set it up.")
  # Keep prompting until it's actually complete so we don't bounce back here
  while True:
    profile = promptProfile(existing=profile)
    if ensureComplete(profile):
      saveProfile(profileName, profile)
      log(f"Saved profile '{profileName}' to Windows Credential Manager.")
      break
    else:
      log("Profile is still missing required fields. Please fill them in.")

sshHost = profile["host"]
sshUser = profile["user"]
remotePath = profile["remotePath"]
sshPort = profile.get("port", 22)

# ---------------------------
# Connect (retry / edit / quit)
# ---------------------------
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

def tryConnect(p):
  kwargs = dict(
    hostname=p["host"],
    username=p["user"],
    port=p.get("port", 22),
    look_for_keys=False,
    allow_agent=False,
  )
  if p["auth"] == "password":
    kwargs["password"] = p["password"]
  else:
    kwargs["key_filename"] = p["keyFile"]
    if p.get("passphrase"):
      kwargs["passphrase"] = p["passphrase"]
  ssh.connect(**kwargs)

for _ in range(10):
  try:
    tryConnect(profile)
    break
  except paramiko.AuthenticationException:
    log("Authentication failed.")
    choice = (input("[r]etry with same creds, [e]dit profile, or [q]uit? ").strip().lower() or "e")
    if choice.startswith("q"):
      sys.exit("Exiting.")
    if choice.startswith("e"):
      profile = promptProfile(existing=profile)
      saveProfile(profileName, profile)
  except Exception as e:
    log(f"SSH connection error: {e}")
    choice = (input("[e]dit profile to fix, or [q]uit? ").strip().lower() or "e")
    if choice.startswith("q"):
      sys.exit("Exiting.")
    profile = promptProfile(existing=profile)
    saveProfile(profileName, profile)
else:
  sys.exit("Unable to establish SSH connection after multiple attempts.")

# Refresh in case user edited
sshHost = profile["host"]
sshUser = profile["user"]
remotePath = profile["remotePath"]
sshPort = profile.get("port", 22)

log(f"Connected to {sshUser}@{sshHost} using profile '{profileName}'.")

# ---------------------------
# Prepare remote directory
# ---------------------------
log(f"Preparing ~/{remotePath} on remote...")

checkCmd = f"test -d ~/{remotePath}"
stdin, stdout, stderr = ssh.exec_command(checkCmd)
exitStatus = stdout.channel.recv_exit_status()

if exitStatus == 0:
  log(f"Remote path ~/{remotePath} exists. Deleting its contents...")
  clearCmd = f"rm -rf ~/{remotePath}/*"
  stdin, stdout, stderr = ssh.exec_command(clearCmd)
  errors = stderr.read().decode(errors="replace").strip()
  if errors:
    ssh.close()
    sys.exit(f"Error clearing ~/{remotePath}: {errors}")
else:
  log(f"Remote path ~/{remotePath} does not exist. Creating it...")
  createCmd = f"mkdir -p ~/{remotePath}"
  stdin, stdout, stderr = ssh.exec_command(createCmd)
  errors = stderr.read().decode(errors="replace").strip()
  if errors:
    ssh.close()
    sys.exit(f"Error creating ~/{remotePath}: {errors}")

# ---------------------------
# Upload files
# ---------------------------
log("Uploading files...")
with SCPClient(ssh.get_transport()) as scp:
  createdDirs = set()
  for root, dirs, files in os.walk("."):
    if not isValidPath(root):
      continue

    # Prune directories in-place
    dirs[:] = [d for d in dirs if d not in skipDirs and isValidPath(os.path.join(root, d))]

    for f in files:
      fullPath = os.path.join(root, f)
      if isValidPath(fullPath):
        relPath = os.path.relpath(fullPath, ".")
        remoteRel = os.path.join(remotePath, relPath).replace("\\", "/")
        remoteDir = os.path.dirname(remoteRel)

        if remoteDir and remoteDir not in createdDirs:
          ssh.exec_command(f"mkdir -p ~/{remoteDir}")
          createdDirs.add(remoteDir)

        log(f"Uploading {remoteRel}")
        scp.put(fullPath, remote_path=remoteRel)

# ---------------------------
# Docker compose (remote)
# ---------------------------
log("Performing docker compose up/down...")
for cmd in ["docker compose down", "docker compose build --no-cache && docker compose up -d"]:
  fullCmd = f"cd ~/{remotePath} && {cmd}"
  stdin, stdout, stderr = ssh.exec_command(fullCmd)
  out = stdout.read().decode(errors="replace").strip()
  err = stderr.read().decode(errors="replace").strip()
  if out:
    log(out)
  if err:
    log(err)

ssh.close()
log("Deployment complete.")
