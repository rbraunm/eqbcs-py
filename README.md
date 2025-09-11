# EQBCS‑PY
MacroQuest‑compatible **EQBCS** server reimplemented in Python, with multi‑instance support, Docker/Compose packaging, and an optional VS Code‑driven deploy workflow.

Wire‑compatible with the EQBCS protocol used by MacroQuest plugins. Point your MQ clients at the host:port you expose here and (optionally) a password; no client changes required.

---

## Quick start

### A) Docker Compose (recommended)
The repo ships with a ready‑to‑use `docker-compose.yml` that can launch **N** parallel servers on a contiguous port range.

1. Clone the repo and open it in a shell at the repo root.
2. (Optional) Edit `docker-compose.yml` environment values to your liking (see [Environment variables](#environment-variables)).
3. Bring it up:
   ```bash
   docker compose up -d
   ```
4. MQ clients can connect to any of the exposed ports (e.g., `22112`–`22116` by default).

> **Heads‑up on port mapping:** The `ports:` stanza maps a *range*, and **must** match `EQBCS_PY_PORT_RANGE_START` and `EQBCS_PY_SERVER_COUNT`. Example mapping from the included compose file:
>
> ```yaml
> ports:
>   - "22112-22116:22112-22116"
> ```
> With `EQBCS_PY_PORT_RANGE_START=22112` and `EQBCS_PY_SERVER_COUNT=5`, that exposes instances on 22112..22116.

#### One‑shot overrides
You can override any env just for a run:
- Linux/macOS:
  ```bash
  EQBCS_PY_SERVER_COUNT=3 EQBCS_PY_PORT_RANGE_START=23112 docker compose up -d
  ```
- PowerShell:
  ```powershell
  $env:EQBCS_PY_SERVER_COUNT=3; $env:EQBCS_PY_PORT_RANGE_START=23112; docker compose up -d
  ```

---

### B) VS Code deploy flow (optional “dev‑toolchain as deploy”)
The repo includes `.vscode/deploy.py`, an **interactive** SSH deploy helper that:
- Stores connection profiles in your OS keyring
- Rsync‑style uploads the repo to your server (excludes dotfolders and build junk)
- Runs `docker compose down` then `docker compose build && docker compose up -d` remotely

**Run it directly**
```bash
python .vscode/deploy.py
```
It will prompt once for a profile (host, port, user, `~/remotePath`, and either a key file or password), then remember it. You can choose a profile name via `DEPLOY_PROFILE` (defaults to the current folder name).

**Run from VS Code Tasks (if you wire one)**
If you prefer Tasks, add a simple task that invokes the script:
```jsonc
// .vscode/tasks.json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "Deploy: Upload & Compose up",
      "type": "shell",
      "command": "python .vscode/deploy.py",
      "group": "none",
      "problemMatcher": []
    }
  ]
}
```
Then `⇧⌘P` / `Ctrl+Shift+P` → **Tasks: Run Task** → *Deploy: Upload & Compose up*.

---

## Local run (no Docker)

```bash
python server.py -p 2112 -i 0.0.0.0                # default EQBCS port
# optional: require a password; clients must use LOGIN:<password>=
python server.py -p 2112 -s "MySecret"
# enable verbose logging
python server.py -p 2112 -v
```

> When running in Docker, the entrypoint spawns multiple instances for you. For a single local instance, you typically don’t need any env vars.

---

## Environment variables

These are read by the server and/or the Docker entrypoint.

### Core
| Name | Type / Values | Default | Used by | Description |
|---|---|---|---|---|
| `EQBCS_PY_PORT_RANGE_START` | integer | `22112` (Docker) / CLI port | entrypoint + server | First port in the range. Instance **N** runs on `PORT_RANGE_START + N`. The server uses this to compute the instance number for password selection. |
| `EQBCS_PY_SERVER_COUNT` | integer ≥ 1 | `1` | entrypoint | How many instances to spawn in the container. Compose must map the entire range. |
| `EQBCS_PY_BIND` | IP | `0.0.0.0` | entrypoint → server `-i` | Bind address for each instance. |
| `EQBCS_PY_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | `INFO` | server | Sets the server log level (overrides `-v`). |
| `EQBCS_PY_LOG_FILE` | path | *(unset)* | server | If set, logs also go to this file (in addition to stdout). |

### Logging detail toggles (booleans)
Accepts `1/true/yes/on` (case‑insensitive) for **on**.
| Name | Default | What it does |
|---|---:|---|
| `EQBCS_PY_LOG_KEEPALIVE` | `0` | Log PING/PONG keepalive traffic. |
| `EQBCS_PY_LOG_RX_RAW` | `0` | Log raw inbound frames (noisy; for debugging protocol issues). |
| `EQBCS_PY_LOG_STATE` | `0` | Log connection lifecycle / state transitions. |
| `EQBCS_PY_LOG_CTRL_SUMMARY` | `0` | Log control command summaries. |
| `EQBCS_PY_LOG_CTRL_RECIPIENTS` | `0` | Log resolved recipients for TELL/BCI, etc. |

### Timeouts
| Name | Type | Default | What it does |
|---|---|---:|---|
| `EQBCS_PY_CLIENT_TIMEOUT` | seconds (int) | `120` | If a client fails to PONG for this many seconds, disconnect it. Set `0` to disable disconnect (legacy advisory mode). |

### Password policy
You can set a **master** policy and optionally override it per instance. Each policy accepts:
- `"none"` / `"null"` / `"false"` / `"0"` / `"off"` / *(unset)* → **no password required**
- `"auto"` → generates a secure password **once per run**, logs it at startup
- any other string → **use that exact password**

| Name | Applies to | Example |
|---|---|---|
| `EQBCS_PY_MASTER_PASSWORD` | default for **all** instances | `EQBCS_PY_MASTER_PASSWORD=auto` |
| `EQBCS_PY_INSTANCE{N}_PASSWORD` | overrides instance **N** only (N = 0‑based) | `EQBCS_PY_INSTANCE3_PASSWORD=MySecret` |

**Instance number (N) mapping:**  
If your range starts at `22112`, then:
- instance **0** → port **22112**
- instance **1** → port **22113**
- …

> Tip: You can mix policies. Example: `EQBCS_PY_MASTER_PASSWORD=auto` but `EQBCS_PY_INSTANCE0_PASSWORD=none` for a public “no‑password” local instance alongside passworded ones.

---

## Docker details

### docker-compose.yml (included)
Key bits of the shipped compose file:
```yaml
services:
  eqbcs:
    build: .
    container_name: eqemu_eqbcs_py
    environment:
      EQBCS_PY_SERVER_COUNT: 5
      EQBCS_PY_PORT_RANGE_START: 22112
      EQBCS_PY_LOG_LEVEL: INFO
      EQBCS_PY_LOG_CTRL_SUMMARY: 0
      EQBCS_PY_LOG_CTRL_RECIPIENTS: 0
      EQBCS_PY_LOG_STATE: 0
      EQBCS_PY_LOG_RX_RAW: 0
      EQBCS_PY_LOG_KEEPALIVE: 0
    ports:
      - "22112-22116:22112-22116"
    restart: unless-stopped
    stop_signal: SIGTERM
    stop_grace_period: 10s
```

### Manual docker run (alternative)
```bash
# Build
docker build -t eqemu_eqbcs_py .

# Run 5 instances on 22112..22116
docker run -d --name eqemu_eqbcs_py \
  -e EQBCS_PY_SERVER_COUNT=5 \
  -e EQBCS_PY_PORT_RANGE_START=22112 \
  -e EQBCS_PY_LOG_LEVEL=INFO \
  -p 22112-22116:22112-22116 \
  --restart unless-stopped \
  eqemu_eqbcs_py
```

---

## CLI flags (for `server.py` directly)
```
-p, --port <int>      Port to listen on (default 2112)
-i, --bind <addr>     Bind address (default 0.0.0.0)
-l, --logfile <path>  Also write logs to this file
-v, --debug           Enable verbose logging (INFO→DEBUG)
-s, --password <str>  Require password; clients must send LOGIN:<password>=
```

> In Docker, the entrypoint computes port per instance using `EQBCS_PY_PORT_RANGE_START + index` and passes `-i`/`-p` to each process.

---

## Security notes
- If you set `"auto"` passwords, the generated secret is printed at startup logs—treat logs accordingly.
- Exposed ports are open; restrict with firewall/security groups as needed.
- Anyone with write access to your GitHub/CI that can change envs or compose can change passwords—protect those paths.

---

## Troubleshooting
- **Clients can’t connect:** Check that the exposed port range matches the envs (`docker compose ps` + your firewall).  
- **Password rejected:** Confirm which instance you’re on and its policy (master vs `INSTANCE{N}` override).  
- **Too chatty logs:** Leave detail toggles at `0` and set `EQBCS_PY_LOG_LEVEL=INFO`.

---

## Project status
Actively iterating. Protocol behaviors are tracked in `buildspec.yaml` (spec doc, not AWS CodeBuild).

---

## License
TBD.
