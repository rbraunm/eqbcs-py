import datetime
import io
import os
import posixpath
import subprocess
import sys

def log(message):
  print(f"[{datetime.datetime.now().isoformat(sep=' ', timespec='seconds')}] {message}", flush=True)

def ensure(module, pipName=None):
  try:
    __import__(module)
  except ImportError:
    pipName = pipName or module
    log(f"Module '{module}' not found. Installing '{pipName}'...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", pipName])
    log(f"'{pipName}' installed successfully.\n")

ensure("paramiko")

import paramiko

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mcps", "facet", "python"))
from facetClient.secretStore import readLogin

sshHost = "peridot.strata.unseencolor.com"
sshUser = "claude"
credentialTarget = "hosts/peridot/claude"
stagingPath = ".eqbcs-deploy"
appOwner = "eqemu"
appPath = "/opt/eqemu/eqbcs"
knownHostsPath = os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")
repositoryRoot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
skipNames = {"README.md", "buildspec.yaml"}
skipSuffixes = (".code-workspace",)
skipDirs = {"node_modules", "__pycache__", "dist", "build", "venv", ".venv"}

def run(ssh, command):
  stdin, stdout, stderr = ssh.exec_command(command)
  output = stdout.read().decode(errors="replace").strip()
  errors = stderr.read().decode(errors="replace").strip()
  status = stdout.channel.recv_exit_status()
  if output:
    log(output)
  if errors:
    log(errors)
  if status != 0:
    ssh.close()
    sys.exit(f"'{command}' failed with exit status {status}.")

def isDeployed(fileName):
  return fileName not in skipNames and not fileName.endswith(skipSuffixes)

userName, privateKey = readLogin(credentialTarget)
if userName != sshUser:
  sys.exit(f"{credentialTarget} is for {userName}, not {sshUser}.")

ssh = paramiko.SSHClient()
ssh.load_system_host_keys(knownHostsPath)
ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
try:
  ssh.connect(sshHost, username=sshUser, pkey=paramiko.Ed25519Key.from_private_key(io.StringIO(privateKey)),
    look_for_keys=False, allow_agent=False)
except Exception as e:
  sys.exit(f"SSH connection error: {e}")

log(f"Staging files in ~/{stagingPath}...")
run(ssh, f"rm -rf ~/{stagingPath} && mkdir -p ~/{stagingPath}")
sftp = ssh.open_sftp()
createdDirs = {stagingPath}
for root, dirs, files in os.walk(repositoryRoot):
  dirs[:] = [d for d in dirs if d not in skipDirs and not d.startswith(".")]
  for f in files:
    if not isDeployed(f):
      continue
    relativePath = os.path.relpath(os.path.join(root, f), repositoryRoot)
    remotePath = posixpath.join(stagingPath, relativePath.replace("\\", "/"))
    remoteDir = posixpath.dirname(remotePath)
    if remoteDir not in createdDirs:
      run(ssh, f"mkdir -p ~/{remoteDir}")
      createdDirs.add(remoteDir)
    log(f"Uploading {relativePath}")
    sftp.put(os.path.join(root, f), remotePath, confirm=True)
sftp.close()

log(f"Replacing {appPath} with the staged files, owned by {appOwner}...")
run(ssh, f"sudo -n rsync -a --delete --chown={appOwner}:{appOwner} ~/{stagingPath}/ {appPath}/ && rm -rf ~/{stagingPath}")

log("Building, then replacing the running container...")
run(ssh, f"sudo -n -u {appOwner} -H sh -c 'cd {appPath} && docker compose build --no-cache && docker compose down && docker compose up -d'")

ssh.close()
log("Deployment complete.")
