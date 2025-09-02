from __future__ import annotations
import argparse
import logging
import os
import selectors
import socket
import sys
import time
import signal
import base64
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

# ---------------- constants ----------------
eqbcDefaultPort = 2112
maxPortSize = 65535
loginStartToken = "LOGIN="

# keepalive (matches legacy timing closely)
pingSeconds = 30
pingTimeoutSeconds = 75  # advisory-only per spec v0.1.3

# message type bytes used by the legacy EQBCS wire protocol
msgTypeNormal   = 1
msgTypeNbmsg    = 2
msgTypeMsgAll   = 3   # /bca, /bcaa -> broadcast command (as plain text wire)
msgTypeTell     = 4
msgTypeChannels = 5
msgTypeBci      = 6

TYPE_NAMES = {
  msgTypeNormal:   "NORMAL",
  msgTypeNbmsg:    "NBMSG",
  msgTypeMsgAll:   "MSGALL",
  msgTypeTell:     "TELL",
  msgTypeChannels: "CHANNELS",
  msgTypeBci:      "BCI",
}

# ---------------- centralized logger (env-driven) ----------------
logger = logging.getLogger("eqbcs-py")

def _parseLogLevel(s: Optional[str]) -> Optional[int]:
  if not s:
    return None
  t = s.strip().upper()
  m = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING, "WARNING": logging.WARNING,
    "ERROR": logging.ERROR, "ERR": logging.ERROR,
    "CRITICAL": logging.CRITICAL, "FATAL": logging.CRITICAL,
    "TRACE": logging.DEBUG,
  }
  if t in m:
    return m[t]
  if t.isdigit():
    n = int(t)
    if 0 <= n <= 50:
      return n
  return None

def setupLogger(logPath: Optional[str], level: int, *, port_prefix: Optional[int] = None) -> None:
  logger.handlers.clear()
  logger.setLevel(level)
  fmt_str = "%(asctime)s [%(levelname)s]"
  if port_prefix is not None:
    fmt_str += f" {port_prefix}"
  fmt_str += " %(message)s"
  fmt = logging.Formatter(fmt_str)

  ch = logging.StreamHandler(sys.stdout)
  ch.setLevel(level)
  ch.setFormatter(fmt)
  logger.addHandler(ch)

  if logPath:
    fh = logging.FileHandler(logPath, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

# env toggles
def _env_bool(name: str, default: bool=False) -> bool:
  v = os.getenv(name)
  if v is None:
    return default
  return v.strip().lower() in ("1","true","yes","on")

def _env_int(name: str, default: int) -> int:
  v = os.getenv(name)
  if v is None or v.strip() == "":
    return default
  try:
    return max(0, int(v.strip()))
  except Exception:
    return default

LOG_KEEPALIVE = _env_bool("EQBCS_PY_LOG_KEEPALIVE", False)
LOG_RX_RAW = _env_bool("EQBCS_PY_LOG_RX_RAW", False)
LOG_STATE = _env_bool("EQBCS_PY_LOG_STATE", False)
LOG_CTRL_SUMMARY = _env_bool("EQBCS_PY_LOG_CTRL_SUMMARY", False)
LOG_CTRL_RECIPIENTS = _env_bool("EQBCS_PY_LOG_CTRL_RECIPIENTS", False)
CLIENT_TIMEOUT = _env_int("EQBCS_PY_CLIENT_TIMEOUT", 120)  # seconds; 0 disables disconnect (legacy)

# -------- password policy helpers (master/instance; supports null/auto/defined) --------
def _parse_pw_policy(raw: Optional[str]) -> Tuple[str, Optional[str]]:
  """
  Returns (mode, value):
    mode: "none" | "auto" | "defined"
    value: the password when mode == "defined", else None
  Accepts None/""/"null"/"none" -> "none"
  Accepts "auto" (case-insensitive) -> "auto"
  Any other string -> "defined"
  """
  if raw is None:
    return ("none", None)
  t = raw.strip()
  if t == "":
    return ("none", None)
  tl = t.lower()
  # treat common falsy toggles as "none" (in addition to null/none/unset/default)
  if tl in ("null","none","unset","default","false","0","off","no"):
    return ("none", None)
  if tl == "auto":
    return ("auto", None)
  return ("defined", t)

def _gen_base64_22() -> str:
  return base64.b64encode(os.urandom(16)).decode("ascii").rstrip("=")

def resolve_instance_password(instance_no: int) -> Optional[str]:
  """
  Resolve the effective password for the given instance number based on:
    EQBCS_PY_MASTER_PASSWORD: null|auto|<string>
    EQBCS_PY_INSTANCEX_PASSWORD: null|auto|<string>  (X is instance_no)
  Rules:
    - Instance env missing or "none" -> inherit master behavior
    - Master "none" -> no password
    - "auto" -> generate once per scope, log it
    - "defined" -> use provided string
  """
  master_raw = os.getenv("EQBCS_PY_MASTER_PASSWORD")
  inst_raw = os.getenv(f"EQBCS_PY_INSTANCE{instance_no}_PASSWORD")
  mMode, mValue = _parse_pw_policy(master_raw)
  iMode, iValue = _parse_pw_policy(inst_raw)
  # Cache for auto master so we reuse the same across instances
  global _AUTO_MASTER_PASSWORD_CACHE
  try:
    _AUTO_MASTER_PASSWORD_CACHE
  except NameError:
    _AUTO_MASTER_PASSWORD_CACHE = None

  def _master_effective() -> Optional[str]:
    nonlocal mMode, mValue
    global _AUTO_MASTER_PASSWORD_CACHE
    if mMode == "none":
      return None
    if mMode == "defined":
      return mValue
    if mMode == "auto":
      if _AUTO_MASTER_PASSWORD_CACHE is None:
        _AUTO_MASTER_PASSWORD_CACHE = _gen_base64_22()
        logger.info("[security] Generated master password (auto): %s", _AUTO_MASTER_PASSWORD_CACHE)
      return _AUTO_MASTER_PASSWORD_CACHE
    return None

  # Instance policy
  if iMode == "none":
    # inherit master behavior
    pw = _master_effective()
    if pw is None:
      logger.info("[security] No password required for instance %s (inherits master=none).", instance_no)
    else:
      # Do not log the actual master password here (already logged if auto)
      logger.info("[security] Using master password for instance %s.", instance_no)
    return pw

  if iMode == "defined":
    logger.info("[security] Using explicit password for instance %s.", instance_no)
    return iValue

  if iMode == "auto":
    pw = _gen_base64_22()
    logger.info("[security] Generated password for instance %s (auto): %s", instance_no, pw)
    return pw

  # Fallback safety
  return _master_effective()


# ---------------- helpers ----------------
def nowSeconds() -> int:
  return int(time.time())

def _preview(s: str, n: int = 200) -> str:
  s = s.replace("\n", "\\n")
  return s if len(s) <= n else s[:n] + "…"

def _sendLine(sock: socket.socket, line: str) -> None:
  payload = line.encode("utf-8", "ignore")
  if not payload.endswith(b"\n"):
    payload += b"\n"
  sock.sendall(payload)

def _sendTyped(sock: socket.socket, typeByte: int, text: str) -> None:
  # Typed wire format used only for legacy NBPKT and some control lines; for
  # MSGALL/TELL/BCI we intentionally send *plain text* lines per spec.
  payload = bytearray()
  payload.extend(b"\t")
  payload.append(typeByte & 0xFF)
  payload.extend(text.encode("utf-8", "ignore"))
  if not payload.endswith(b"\n"):
    payload.extend(b"\n")
  sock.sendall(payload)

def _split_name_payload(text: str) -> Tuple[str, str]:
  # For typed frames where text is "Name\tpayload"
  if "\t" in text:
    a, b = text.split("\t", 1)
    return a, b
  return text, ""

def _unescape_text(s: str) -> str:
  # Backslash escapes the next character (TELL/BCI payloads)
  out = []
  i = 0
  while i < len(s):
    c = s[i]
    if c == "\\" and i + 1 < len(s):
      out.append(s[i+1])
      i += 2
    else:
      out.append(c)
      i += 1
  return "".join(out)

def _collapse_spaces(s: str) -> str:
  # collapse multiple spaces to single (cosmetic; preserves tabs)
  out = []
  prev_space = False
  for ch in s:
    if ch == " ":
      if not prev_space:
        out.append(ch)
      prev_space = True
    else:
      out.append(ch)
      prev_space = False
  return "".join(out)

@dataclass
class Client:
  sock: socket.socket
  addr: tuple
  charName: str = ""
  authorized: bool = False
  lastPingAt: int = field(default_factory=nowSeconds)
  lastPongAt: int = field(default_factory=nowSeconds)
  readBuf: bytearray = field(default_factory=bytearray)
  pendingMsgType: Optional[int] = None
  localEcho: bool = False
  chanList: str = ""  # space-separated channel tokens
  isClosing: bool = False  # reentrancy guard for disconnect

# ---------------- server ----------------
class EqbcsPyServer:
  def __init__(self, bindAddr: str, port: int, *, password: Optional[str] = None, maxClients: int = 250) -> None:
    self.bindAddr = bindAddr
    self.port = port
    self.sel = selectors.DefaultSelector()
    self.clients: Dict[socket.socket, Client] = {}
    self._shouldStop = False
    self._lsock: Optional[socket.socket] = None
    self.password = password
    self.maxClients = maxClients

  # lifecycle
  def start(self) -> None:
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((self.bindAddr, self.port))
    lsock.listen(1)  # spec backlog: 1
    lsock.setblocking(False)
    self._lsock = lsock
    self.sel.register(lsock, selectors.EVENT_READ, data=None)
    logger.info("EQBCS(py) listening on %s:%s", self.bindAddr, self.port)

    # graceful shutdown
    signal.signal(signal.SIGINT, lambda *_: self._stop())
    signal.signal(signal.SIGTERM, lambda *_: self._stop())

    try:
      while not self._shouldStop:
        events = self.sel.select(timeout=1.0)
        for key, mask in events:
          if key.data is None:
            self._accept(key.fileobj)
          else:
            self._service(key, mask)
        self._tick()
    finally:
      self._closeAll()

  def _stop(self) -> None:
    self._shouldStop = True

  # accept / service
  def _accept(self, lsock: socket.socket) -> None:
    try:
      conn, addr = lsock.accept()
      conn.setblocking(False)
    except Exception as e:
      logger.error("[accept] error: %s", e)
      return

    if len(self.clients) >= self.maxClients:
      try:
        _sendLine(conn, "-- Server full.")
      except Exception:
        pass
      try:
        conn.close()
      finally:
        return

    cli = Client(sock=conn, addr=addr)
    self.clients[conn] = cli
    self.sel.register(conn, selectors.EVENT_READ, data=cli)
    logger.info("[conn] %s:%s connected (clients=%d)", addr[0], addr[1], len(self.clients))

  def _service(self, key, mask) -> None:
    cli: Client = key.data
    if mask & selectors.EVENT_READ:
      try:
        data = cli.sock.recv(4096)
      except ConnectionResetError:
        self._disconnect(cli, reason="reset by peer")
        return
      if not data:
        self._disconnect(cli, reason="eof")
        return
      cli.readBuf.extend(data)
      # process full lines (split on \n). Preserve possible \t inside lines.
      while True:
        nl = cli.readBuf.find(b"\n")
        if nl < 0:
          break
        raw = cli.readBuf[:nl].rstrip(b"\r")  # ignore CR in command lines per spec
        del cli.readBuf[:nl+1]
        try:
          line = raw.decode("utf-8", "ignore")
        except Exception:
          line = ""
        self._rx_line(cli, line)

  # disconnect
  def _disconnect(self, cli: Client, reason: str = "") -> None:
    # Reentrancy-safe & remove-before-broadcast to avoid recursion
    if cli.isClosing:
      return
    cli.isClosing = True
    nm = cli.charName or f"{cli.addr[0]}:{cli.addr[1]}"
    # Remove from I/O and client map first
    try:
      self.sel.unregister(cli.sock)
    except Exception:
      pass
    try:
      cli.sock.close()
    except Exception:
      pass
    self.clients.pop(cli.sock, None)

    # Now it's safe to log and notify others
    logger.info("[disc] %s: %s", nm, reason)
    if cli.authorized and cli.charName:
      # NBQUIT to all others
      self._broadcastControl(f"\tNBQUIT={cli.charName}")
      # system line (human logs only)
      self._broadcastSystem(f"-- {cli.charName} has left the server.")
      # push updated roster
      self._broadcastNbClientList()

  def _closeAll(self) -> None:
    if self._lsock:
      try:
        self.sel.unregister(self._lsock)
      except Exception:
        pass
      try:
        self._lsock.close()
      except Exception:
        pass
    for cli in list(self.clients.values()):
      self._disconnect(cli, reason="server shutdown")

  # inbound line handling
  def _rx_line(self, cli: Client, line: str) -> None:
    if not cli.authorized:
      self._handle_login_or_buffer(cli, line)
      return

    # Command line?
    if line.startswith("\t"):
      self._handle_command(cli, line[1:].strip())
      return

    # Blank line (often seen after PONG on some clients)
    if line == "":
      if LOG_KEEPALIVE:
        logger.debug("Ignored blank line from %s", cli.charName or f"{cli.addr[0]}:{cli.addr[1]}")
      return

    # normal line
    if cli.pendingMsgType is not None:
      t = cli.pendingMsgType
      cli.pendingMsgType = None
      if t == msgTypeNbmsg:
        logger.debug("RX_TYPE payload from %s: type=%d(%s) payload=%r",
                     cli.charName, t, TYPE_NAMES.get(t,"?"), _preview(line))
        self._broadcastNbpkt(cli, line)
        return
      if t == msgTypeMsgAll:
        logger.debug("RX_TYPE payload from %s: type=%d(%s) payload=%r",
                     cli.charName, t, TYPE_NAMES.get(t,"?"), _preview(line))
        self._broadcastMsgAll(cli, line)
        return
      if t == msgTypeTell:
        logger.debug("RX_TYPE payload from %s: type=%d(%s) payload=%r",
                     cli.charName, t, TYPE_NAMES.get(t,"?"), _preview(line))
        # <target> <text>
        target, msg = (line.split(" ", 1) + [""])[:2]
        self._routeTell(cli, target, _unescape_text(msg))
        return
      if t == msgTypeChannels:
        logger.debug("RX_TYPE payload from %s: type=%d(%s) payload=%r",
                     cli.charName, t, TYPE_NAMES.get(t,"?"), _preview(line))
        cli.chanList = line.strip()
        announce = f"{cli.charName} joined channels {cli.chanList}."
        self._tx_text(cli, announce, kind="CHANNELS")
        # no broadcast; confirmation only to the caller per spec
        return
      if t == msgTypeBci:
        logger.debug("RX_TYPE payload from %s: type=%d(%s) payload=%r",
                     cli.charName, t, TYPE_NAMES.get(t,"?"), _preview(line))
        # <target> <text>
        target, msg = (line.split(" ", 1) + [""])[:2]
        self._handleBci(cli, target, _unescape_text(msg))
        return

      # Unknown pending -> ignore
      return

    # untyped -> treat as MSGALL
    logger.debug("Coercing untyped line to MSGALL from %s: %r", cli.charName, line)
    self._broadcastMsgAll(cli, line)

  # authentication & login parsing
  def _handle_login_or_buffer(self, cli: Client, line: str) -> None:
    # Expect LOGIN forms:
    #   LOGIN=<CharName>;
    #   LOGIN:<Password>=<CharName>;
    if not line.startswith("LOGIN"):
      # ignore anything until proper login
      return

    # carve off anything after the first ';' then process the remainder (e.g., "\tLOCALECHO 1")
    if ";" in line:
      loginPart, remainder = line.split(";", 1)
    else:
      loginPart, remainder = line, ""

    name = ""
    providedPassword: Optional[str] = None

    if loginPart.startswith("LOGIN:"):
      # With-password form: LOGIN:<password>=<name>
      try:
        after = loginPart[len("LOGIN:"):]
        providedPassword, name = after.split("=", 1)
      except ValueError:
        name = ""
    elif loginPart.startswith("LOGIN="):
      name = loginPart[len("LOGIN="):]
    else:
      name = ""

    name = name.strip()
    if not name:
      self._disconnect(cli, reason="empty login name")
      return

    if self.password is not None:
      if providedPassword != self.password:
        self._disconnect(cli, reason="bad password")
        return

    # store and authorize
    cli.charName = name
    cli.authorized = True
    cli.lastPingAt = nowSeconds()
    cli.lastPongAt = nowSeconds()
    logger.info("[login] %s:%s -> %s", cli.addr[0], cli.addr[1], cli.charName)

    # kick existing with same (case-insensitive per spec duplicate policy uses case-sensitive? spec clarifies CS for duplicates)
    self._kickSameName(cli)

    # control lines & roster
    self._broadcastControl(f"\tNBJOIN={cli.charName}")
    self._tx_control(cli, f"\tNBCLIENTLIST={self._roster_line()}")
    self._broadcastSystem(f"-- {cli.charName} has joined the server.")
    self._broadcastNbClientList()  # push roster to others as well

    # If remainder contains additional commands (e.g., "\tLOCALECHO 1"), handle it
    remainder = remainder.lstrip()
    if remainder.startswith("\t"):
      self._handle_command(cli, remainder[1:].strip())

  # command dispatcher
  def _handle_command(self, cli: Client, cmdLine: str) -> None:
    # cmdLine already stripped and without leading TAB
    if not cmdLine:
      return
    # token and args separated by a single space (spec)
    parts = cmdLine.split(" ", 1)
    token = parts[0].strip().upper()
    arg = parts[1] if len(parts) == 2 else ""

    if token == "PONG":
      cli.lastPongAt = nowSeconds()
      if LOG_KEEPALIVE:
        logger.debug("PONG from %s (lastPongAt=%d)", cli.charName, cli.lastPongAt)
      return
    if token == "NAMES":
      self._handleNames(cli)
      return
    if token == "NBNAMES":
      self._tx_control(cli, f"\tNBCLIENTLIST={self._roster_line()}")
      return
    if token == "DISCONNECT":
      self._disconnect(cli, reason="client requested disconnect")
      return
    if token == "LOCALECHO":
      v = arg.strip()
      newVal = (v == "1" or v.upper() in ("ON","TRUE"))
      cli.localEcho = newVal
      self._tx_text(cli, f"-- Local Echo: {'ON' if cli.localEcho else 'OFF'}", kind="LOCALECHO")
      return

    # arm-and-next-line commands
    if token == "NBMSG":
      cli.pendingMsgType = msgTypeNbmsg
      logger.debug("RX_TYPE preface from %s: pending=%d(%s)", cli.charName, cli.pendingMsgType, TYPE_NAMES.get(cli.pendingMsgType,"?"))
      return
    if token == "MSGALL":
      cli.pendingMsgType = msgTypeMsgAll
      logger.debug("RX_TYPE preface from %s: pending=%d(%s)", cli.charName, cli.pendingMsgType, TYPE_NAMES.get(cli.pendingMsgType,"?"))
      return
    if token == "TELL":
      cli.pendingMsgType = msgTypeTell
      logger.debug("RX_TYPE preface from %s: pending=%d(%s)", cli.charName, cli.pendingMsgType, TYPE_NAMES.get(cli.pendingMsgType,"?"))
      return
    if token == "CHANNELS":
      cli.pendingMsgType = msgTypeChannels
      logger.debug("RX_TYPE preface from %s: pending=%d(%s)", cli.charName, cli.pendingMsgType, TYPE_NAMES.get(cli.pendingMsgType,"?"))
      return
    if token == "BCI":
      cli.pendingMsgType = msgTypeBci
      logger.debug("RX_TYPE preface from %s: pending=%d(%s)", cli.charName, cli.pendingMsgType, TYPE_NAMES.get(cli.pendingMsgType,"?"))
      return

    # unknown command
    self._tx_text(cli, f"-- Unknown Command: {token}", kind="UNKNOWN")

  # names
  def _handleNames(self, cli: Client) -> None:
    names = self._all_names_sorted()
    self._tx_text(cli, f"-- Names: {' '.join(names)} .", kind="NAMES")

  def _all_names_sorted(self) -> List[str]:
    return sorted([c.charName for c in self.clients.values() if c.authorized and c.charName])

  def _roster_line(self) -> str:
    return " ".join(self._all_names_sorted())

  # outbound helpers
  def _tx_text(self, dst: Client, text: str, *, kind: str = "TEXT") -> None:
    try:
      _sendLine(dst.sock, text)
      logger.debug("TX_TEXT(%s) -> [%s] %r",
                   kind, dst.charName or f"{dst.addr[0]}:{dst.addr[1]}", _preview(text))
    except Exception:
      self._disconnect(dst, reason="send failure")

  def _tx_control(self, dst: Client, text: str) -> None:
    # control lines are emitted as-is; ensure newline
    try:
      _sendLine(dst.sock, text)
      t_ctrl = text.strip("\r\n")
      if not ((t_ctrl in ("\tPING", "\tPONG")) and not LOG_KEEPALIVE):
        logger.debug("TX_CTRL -> [%s] %r", dst.charName or f"{dst.addr[0]}:{dst.addr[1]}", _preview(text))
    except Exception:
      self._disconnect(dst, reason="send failure")

  def _tx_typed(self, dst: Client, typeByte: int, text: str) -> None:
    try:
      _sendTyped(dst.sock, typeByte, text)
      logger.debug("TX_TYPE -> [%s] type=%d(%s) name_payload=%r",
                   dst.charName or f"{dst.addr[0]}:{dst.addr[1]}",
                   typeByte, TYPE_NAMES.get(typeByte, "?"), _preview(text))
    except Exception:
      self._disconnect(dst, reason="send failure")

  # broadcast helpers
  def _broadcastControl(self, line: str) -> None:
    for dst in list(self.clients.values()):
      if not dst.authorized:
        continue
      self._tx_control(dst, line)

  def _broadcastMsgAll(self, src: Client, msg: str) -> None:
    body = _collapse_spaces(msg.strip())
    # If it looks like /bca or /bcaa payload (starts with //), send per-recipient with their name.
    if body.startswith("//"):
      for dst in list(self.clients.values()):
        if not dst.authorized:
          continue
        if dst is src and not src.localEcho:
          continue
        per = f"<{src.charName}> {dst.charName} {body}"
        self._tx_text(dst, per, kind="MSGALL")
      return
    # Plain chat: no roster names.
    line = f"<{src.charName}> {body}"
    for dst in list(self.clients.values()):
      if not dst.authorized:
        continue
      if dst is src and not src.localEcho:
        continue
      self._tx_text(dst, line, kind="MSGALL")
  def _broadcastNbpkt(self, src: Client, msg: str) -> None:
    # Legacy NBMSG format: "\tNBPKT:<name>:<payload>" (never echoed locally)
    line = f"\tNBPKT:{src.charName}:{msg}"
    for dst in list(self.clients.values()):
      if not dst.authorized:
        continue
      if dst is src:
        continue
      self._tx_control(dst, line)

  def _routeTell(self, src: Client, target: str, msg: str) -> None:
    if not target:
      return
    # Case-insensitive match on character names
    for dst in self.clients.values():
      if dst.authorized and dst.charName.lower() == target.lower():
        self._tx_text(dst, f"[{src.charName}] {msg}", kind="TELL")
        return
    # Else treat target as channel token, case-sensitive
    delivered = False
    for dst in self.clients.values():
      if not dst.authorized:
        continue
      if dst is src and not src.localEcho:
        continue
      if not dst.chanList:
        continue
      chans = set(dst.chanList.split())
      if target in chans:
        self._tx_text(dst, f"[{src.charName}] {msg}", kind="TELL")
        delivered = True
    if not delivered:
      self._tx_text(src, f"-- {target}: No such name.", kind="TELL")

  def _handleBci(self, src: Client, target: str, msg: str) -> None:
    if not target:
      return
    # direct name (case-insensitive)
    for dst in self.clients.values():
      if dst.authorized and dst.charName.lower() == target.lower():
        self._tx_text(dst, f"{{{src.charName}}} {msg}", kind="BCI")
        return
    # channel delivery (case-sensitive tokens)
    delivered = False
    for dst in self.clients.values():
      if not dst.authorized:
        continue
      if dst is src and not src.localEcho:
        continue
      if not dst.chanList:
        continue
      chans = set(dst.chanList.split())
      if target in chans:
        self._tx_text(dst, f"{{{src.charName}}} {msg}", kind="BCI")
        delivered = True
    if not delivered:
      self._tx_text(src, f"-- {target}: No such name.", kind="BCI")

  def _broadcastLocal(self, src: Client, text: str) -> None:
    for dst in list(self.clients.values()):
      if not dst.authorized or dst is src:
        continue
      self._tx_text(dst, text, kind="LOCAL")

  def _broadcastSystem(self, text: str) -> None:
    # Log-only system line; do not send to clients (matches legacy behavior)
    logger.info("[system] %s", text)
  def _broadcastNbClientList(self) -> None:
    line = f"\tNBCLIENTLIST={self._roster_line()}"
    self._broadcastControl(line)

  def _kickSameName(self, newcomer: Client) -> None:
    # Duplicate-login detection is case-sensitive according to 0.1.3 notes,
    # but previous behavior kicked on case-insensitive match. Follow spec:
    for dst in list(self.clients.values()):
      if dst is newcomer or not dst.authorized:
        continue
      if dst.charName and dst.charName == newcomer.charName:
        self._disconnect(dst, reason="duplicate name (replaced by new connection)")

  # timers / keepalive
  def _tick(self) -> None:
    t = nowSeconds()
    for cli in list(self.clients.values()):
      if not cli.authorized:
        continue
      # periodic PING
      if cli.lastPingAt + pingSeconds <= t:
        self._tx_control(cli, "\tPING")
        cli.lastPingAt = t
        if LOG_KEEPALIVE:
          logger.debug("TX keepalive PING -> %s", cli.charName)
      # timeout handling
      if CLIENT_TIMEOUT > 0:
        if cli.lastPongAt + CLIENT_TIMEOUT <= t:
          self._disconnect(cli, reason=f"timeout > {CLIENT_TIMEOUT}s without PONG")
          continue
      else:
        # legacy advisory-only mode
        if cli.lastPongAt + pingTimeoutSeconds <= t:
          if LOG_KEEPALIVE:
            logger.debug("PONG timeout (advisory) for %s (lastPongAt=%d, now=%d)", cli.charName, cli.lastPongAt, t)
          # bump window so we don't spam logs
          cli.lastPongAt = t

# ---------------- CLI ----------------
def main() -> int:
  ap = argparse.ArgumentParser(description="EQBCS-like server in Python")
  ap.add_argument("-p","--port", type=int, default=eqbcDefaultPort)
  ap.add_argument("-i","--bind", default="0.0.0.0")
  ap.add_argument("-l","--logfile", default=None)
  ap.add_argument("-v","--debug", action="store_true", help="enable verbose debug logging")
  ap.add_argument("-s","--password", default=None, help="require password; changes accepted login token to LOGIN:<password>=")

  args = ap.parse_args()

  if not (0 < args.port <= maxPortSize):
    print(f"Invalid port: {args.port}", file=sys.stderr)
    return 2

  # ENV overrides flags (for Docker Compose)
  envLogLevel = _parseLogLevel(os.getenv("EQBCS_PY_LOG_LEVEL"))
  level = envLogLevel if envLogLevel is not None else (logging.DEBUG if args.debug else logging.INFO)
  logFile = os.getenv("EQBCS_PY_LOG_FILE") or args.logfile

  # Logger prefix = port from CLI only (no env vars)
  setupLogger(logFile, level, port_prefix=args.port)
  logger.info("Starting EQBCS(py) on %s:%s", args.bind, args.port)
  
  # ---- config banner (proves env + effective level) ----
  lvlname = logging.getLevelName(level)
  _cfglog = logging.getLogger("eqbcs-py")
  _cfglog.debug("[config] level=%s(%s) EQBCS_PY_LOG_LEVEL=%r port=%s "
               "EQBCS_PY_LOG_RX_RAW=%s EQBCS_PY_LOG_STATE=%s EQBCS_PY_LOG_KEEPALIVE=%s "
               "EQBCS_PY_LOG_CTRL_SUMMARY=%s EQBCS_PY_LOG_CTRL_RECIPIENTS=%s client_timeout=%ss",
               lvlname, level, os.getenv("EQBCS_PY_LOG_LEVEL"), args.port,
               LOG_RX_RAW, LOG_STATE, LOG_KEEPALIVE, 
               LOG_CTRL_SUMMARY, LOG_CTRL_RECIPIENTS, CLIENT_TIMEOUT)

  # ----- password resolution (per-instance -> master -> none, then CLI fallback) -----
  try:
    pr_start = int(os.getenv("EQBCS_PY_PORT_RANGE_START", str(args.port)))
  except Exception:
    pr_start = args.port
  instance_no = args.port - pr_start
  if instance_no < 0:
    instance_no = 0

  env_password = resolve_instance_password(instance_no)
  if env_password is None and args.password:
    logger.info("[security] Using explicit CLI password for instance %s.", instance_no)
  effective_password = env_password if env_password is not None else (args.password or None)

  srv = EqbcsPyServer(args.bind, args.port, password=effective_password)
  srv.start()
  return 0

if __name__ == "__main__":
  sys.exit(main())
