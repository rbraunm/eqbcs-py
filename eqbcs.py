from __future__ import annotations
import argparse
import logging
import os
import selectors
import socket
import sys
import time
import signal
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

# ---------------- constants ----------------
eqbcDefaultPort = 2112
maxPortSize = 65535
loginStartToken = "LOGIN="

# keepalive (matches legacy timing closely)
pingSeconds = 30
pingTimeoutSeconds = 75

# message type bytes used by the legacy EQBCS wire protocol
msgTypeNormal   = 1
msgTypeNbmsg    = 2
msgTypeMsgAll   = 3   # /bca, /bcaa -> broadcast command (forwarded as plain text)
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
logger = logging.getLogger("eqbcs")

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

def setupLogger(logPath: Optional[str], level: int) -> None:
  logger.handlers.clear()
  logger.setLevel(level)
  fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

  ch = logging.StreamHandler(sys.stdout)
  ch.setLevel(level)
  ch.setFormatter(fmt)
  logger.addHandler(ch)

  if logPath:
    fh = logging.FileHandler(logPath, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

# ---------------- helpers ----------------
def nowSeconds() -> int:
  return int(time.time())

def _split_name_payload(text: str) -> Tuple[str, str]:
  # For typed frames where text is "Name\tpayload"
  if "\t" in text:
    a, b = text.split("\t", 1)
    return a, b
  return text, ""

def _preview(s: str, n: int = 200) -> str:
  s = s.replace("\n", "\\n")
  return s if len(s) <= n else s[:n] + "…"

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

# ---------------- low-level TX ----------------
def _sendLine(sock: socket.socket, text: str) -> None:
  payload = text.encode("utf-8", "ignore")
  if not payload.endswith(b"\n"):
    payload += b"\n"
  sock.sendall(payload)

def _sendTyped(sock: socket.socket, typeByte: int, text: str) -> None:
  payload = bytearray(b"\t")
  payload.append(typeByte & 0xFF)
  payload.extend(text.encode("utf-8", "ignore"))
  if not payload.endswith(b"\n"):
    payload.extend(b"\n")
  sock.sendall(payload)

# ---------------- server ----------------
class EqbcsPyServer:
  def __init__(self, bindAddr: str, port: int, *, logKeepalive: bool = False) -> None:
    self.bindAddr = bindAddr
    self.port = port
    self.sel = selectors.DefaultSelector()
    self.clients: Dict[socket.socket, Client] = {}
    self._shouldStop = False
    self._lsock: Optional[socket.socket] = None
    self.logKeepalive = logKeepalive  # NEW: gate keepalive logging

  # lifecycle
  def request_stop(self, reason: str = "signal") -> None:
    logger.info("Stopping EQBCS(py) on %s:%s (%s)", self.bindAddr, self.port, reason)
    self._shouldStop = True
    try:
      if self._lsock:
        try:
          self.sel.unregister(self._lsock)
        except Exception:
          pass
        self._lsock.close()
        self._lsock = None
    except Exception:
      pass

  def start(self) -> None:
    # socket setup
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self._lsock = lsock
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((self.bindAddr, self.port))
    lsock.listen()
    lsock.setblocking(False)
    self.sel.register(lsock, selectors.EVENT_READ, data=None)

    # single "Starting ..." line (avoid duplicates)
    logger.info("Starting EQBCS(py) on %s:%s", self.bindAddr, self.port)
    logger.info("EQBCS(py) listening on %s:%s", self.bindAddr, self.port)

    try:
      while not self._shouldStop:
        for key, mask in self.sel.select(timeout=1.0):
          if key.data is None:
            self._accept(key.fileobj)  # type: ignore[arg-type]
          else:
            self._service(key, mask)
        self._tick()
    except KeyboardInterrupt:
      logger.info("KeyboardInterrupt received; shutting down...")
    finally:
      self._closeAll()
      logger.info("EQBCS(py) stopped on %s:%s", self.bindAddr, self.port)

  # sockets
  def _accept(self, lsock: socket.socket) -> None:
    conn, addr = lsock.accept()
    conn.setblocking(False)
    cli = Client(sock=conn, addr=addr)
    self.clients[conn] = cli
    self.sel.register(conn, selectors.EVENT_READ, data=cli)
    logger.info("[conn] %s:%s connected (clients=%d)", addr[0], addr[1], self._clientCount())

  def _service(self, key: selectors.SelectorKey, mask: int) -> None:
    cli: Client = key.data  # type: ignore[assignment]
    sock = cli.sock
    try:
      if mask & selectors.EVENT_READ:
        data = sock.recv(4096)
        if not data:
          self._disconnect(cli, reason="EOF")
          return
        cli.readBuf.extend(data)
        self._drainLines(cli)
    except Exception as e:
      self._disconnect(cli, reason=f"error: {e}")

  def _drainLines(self, cli: Client) -> None:
    while True:
      nl = cli.readBuf.find(b"\n")
      if nl < 0:
        break
      raw = cli.readBuf[:nl].rstrip(b"\r")
      del cli.readBuf[: nl + 1]
      try:
        line = raw.decode("utf-8", "ignore")
      except Exception:
        line = ""
      # Gate RX log for keepalive lines
      is_keepalive = (line == "\tPONG") or (line == "") or (line == "\tPING")
      if is_keepalive and not self.logKeepalive:
        pass  # skip RX debug for keepalive chatter
      else:
        logger.debug("RX [%s] raw=%r line=%r", cli.charName or f"{cli.addr[0]}:{cli.addr[1]}", raw, line)
      self._handleLine(cli, line, raw)

  # high-level line handling
  def _handleLine(self, cli: Client, line: str, raw: bytes) -> None:
    if not cli.authorized:
      if loginStartToken in line and ";" in line:
        start = line.find(loginStartToken) + len(loginStartToken)
        end = line.find(";", start)
        cli.charName = line[start:end].strip()[:70]
        cli.authorized = True
        logger.info("[login] %s:%s -> %s", cli.addr[0], cli.addr[1], cli.charName)
        self._tx_text(cli, f"-- {cli.charName} has joined the server.", kind="SYSTEM")
        self._kickSameName(cli)
      # allow LOCALECHO immediately on same login line for MQ2EQBC
      if "LOCALECHO" in line.upper():
        v = "1" if ("1" in line or line.upper().strip().endswith("LOCALECHO")) else "0"
        cli.localEcho = (v == "1")
        self._tx_text(cli, f"-- Local Echo: {'ON' if cli.localEcho else 'OFF'}", kind="SYSTEM")
      return

    # keepalive control
    if line == "\tPONG":
      cli.lastPongAt = nowSeconds()
      if self.logKeepalive:
        logger.debug("PONG from %s (lastPongAt=%d)", cli.charName or f"{cli.addr}", cli.lastPongAt)
      return
    if line == "\tPING":
      # client shouldn’t send this, but don’t echo it either
      return

    # ignore empty lines (prevents blank chats on PONG processing)
    if line == "":
      if self.logKeepalive:
        logger.debug("Ignored blank line from %s", cli.charName or f"{cli.addr}")
      return

    ucmd = line.strip().upper()

    # simple commands
    if ucmd.startswith("LOCALECHO"):
      v = "1" if ("1" in ucmd or ucmd == "LOCALECHO") else "0"
      cli.localEcho = (v == "1")
      self._tx_text(cli, f"-- Local Echo: {'ON' if cli.localEcho else 'OFF'}", kind="SYSTEM")
      return
    if ucmd == "NAMES":
      names: List[str] = [c.charName for c in self.clients.values() if c.authorized and c.charName]
      self._tx_text(cli, "-- Names: " + (" ".join(sorted(names)) if names else "") + " .", kind="SYSTEM")
      return
    if ucmd == "NBNAMES":
      names: List[str] = [c.charName for c in self.clients.values() if c.authorized and c.charName]
      # legacy NBCLIENTLIST line (tab-prefixed)
      self._tx_text(cli, "\tNBCLIENTLIST=" + " ".join(sorted(names)), kind="NBMSG")
      return

    # two-step typed commands
    if ucmd in ("MSGALL", "NBMSG", "TELL", "CHANNELS", "BCI"):
      cli.pendingMsgType = {
        "MSGALL": msgTypeMsgAll,
        "NBMSG": msgTypeNbmsg,
        "TELL": msgTypeTell,
        "CHANNELS": msgTypeChannels,
        "BCI": msgTypeBci,
      }[ucmd]
      logger.debug("RX_TYPE preface from %s: pending=%d(%s)",
                   cli.charName, cli.pendingMsgType, TYPE_NAMES.get(cli.pendingMsgType, "?"))
      return

    # treat unknown TAB-prefixed lines as control noise
    if raw.startswith(b"\t") and cli.pendingMsgType is None:
      logger.debug("Ignored unrecognized control line from %s: %r", cli.charName, raw)
      return

    # payload for pending type
    if cli.pendingMsgType is not None:
      t = cli.pendingMsgType
      cli.pendingMsgType = None
      payload = line  # keep original spacing
      logger.debug("RX_TYPE payload from %s: type=%d(%s) payload=%r",
                   cli.charName, t, TYPE_NAMES.get(t, "?"), payload)

      if t == msgTypeMsgAll:
        # IMPORTANT: legacy forwards MSGALL to recipients as *plain text* "<Name> payload"
        self._broadcastMsgAll(cli, payload)
        return

      if t == msgTypeNbmsg:
        # Legacy NBMSG -> \tNBPKT:<name>:<payload>
        self._broadcastNbpkt(cli, payload)
        return

      if t == msgTypeTell:
        if "\t" in payload:
          target, msg = payload.split("\t", 1)
        else:
          parts = payload.split(" ", 1)
          target, msg = (parts[0], parts[1] if len(parts) == 2 else "")
        self._sendTell(cli, target.strip(), msg)
        return

      if t == msgTypeChannels:
        cli.chanList = payload.strip()
        announce = f"{cli.charName} joined channels {cli.chanList}."
        self._tx_text(cli, announce, kind="CHANNELS")
        self._broadcastLocal(cli, announce)
        return

      if t == msgTypeBci:
        self._handleBci(cli, payload)
        return

      return

    # untyped -> treat as MSGALL so plugins can parse consistently
    logger.debug("Coercing untyped line to MSGALL from %s: %r", cli.charName, line)
    self._broadcastMsgAll(cli, line)

  # ---------------- outbound helpers ----------------
  def _tx_text(self, dst: Client, text: str, *, kind: str = "TEXT") -> None:
    try:
      _sendLine(dst.sock, text)
      # Gate keepalive TX logs
      if not (kind == "PING" and not self.logKeepalive):
        logger.debug("TX_TEXT(%s) -> [%s] %r",
                     kind, dst.charName or f"{dst.addr[0]}:{dst.addr[1]}", _preview(text))
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
  def _broadcastMsgAll(self, src: Client, msg: str) -> None:
    # Legacy: forward as *plain* "<Name> payload" (not a typed frame)
    line = f"<{src.charName}> {msg}"
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
      self._tx_text(dst, line, kind="NBMSG")

  def _sendTell(self, src: Client, target: str, msg: str) -> None:
    # Legacy user-facing format: "[src] to [target]: msg"
    for dst in self.clients.values():
      if dst.authorized and dst.charName.lower() == target.lower():
        self._tx_text(dst, f"[{src.charName}] to [{target}]: {msg}", kind="TELL")
        return
    self._tx_text(src, f"-- {target}: No such name.", kind="TELL")

  def _handleBci(self, src: Client, payload: str) -> None:
    """
    BCI delivery:
      - If first token is a name, deliver to that one.
      - Else treat token as channel and deliver to all subscribers.
    Recipients get '{name} payload' as a plain line (matches legacy user-facing text).
    """
    if not payload:
      return
    parts = payload.split(" ", 1)
    token = parts[0]
    msg = parts[1] if len(parts) == 2 else ""

    # direct name
    for dst in self.clients.values():
      if dst.authorized and dst.charName.lower() == token.lower():
        logger.debug("BCI direct -> [%s] from %s payload=%r", dst.charName, src.charName, msg)
        self._tx_text(dst, f"{{{src.charName}}} {msg}", kind="BCI")
        return

    # channel delivery
    delivered = False
    for dst in self.clients.values():
      if not dst.authorized:
        continue
      if dst is src and not src.localEcho:
        continue
      if not dst.chanList:
        continue
      chans = set(dst.chanList.split())
      if token in chans:
        logger.debug("BCI channel '%s' -> [%s] from %s payload=%r", token, dst.charName, src.charName, msg)
        self._tx_text(dst, f"{{{src.charName}}} {msg}", kind="BCI")
        delivered = True
    if not delivered:
      self._tx_text(src, f"-- {token}: No such name.", kind="BCI")

  def _broadcastLocal(self, src: Client, text: str) -> None:
    for dst in list(self.clients.values()):
      if not dst.authorized or dst is src:
        continue
      self._tx_text(dst, text, kind="SYSTEM")

  def _broadcastSystem(self, text: str) -> None:
    for dst in list(self.clients.values()):
      if not dst.authorized:
        continue
      self._tx_text(dst, text, kind="SYSTEM")

  def _kickSameName(self, newcomer: Client) -> None:
    for dst in list(self.clients.values()):
      if dst is newcomer or not dst.authorized:
        continue
      if dst.charName and dst.charName.lower() == newcomer.charName.lower():
        self._disconnect(dst, reason="duplicate name (replaced by new connection)")

  # timers / disconnect
  def _tick(self) -> None:
    t = nowSeconds()
    for cli in list(self.clients.values()):
      if not cli.authorized:
        continue
      if cli.lastPingAt + pingSeconds <= t:
        self._tx_text(cli, "\tPING", kind="PING")  # TX log gated in _tx_text
        cli.lastPingAt = t
      if cli.lastPongAt + pingTimeoutSeconds <= t:
        self._disconnect(cli, reason="pong timeout")

  def _disconnect(self, cli: Client, reason: str = "") -> None:
    try:
      nm = cli.charName or f"{cli.addr[0]}:{cli.addr[1]}"
      logger.info("[disc] %s: %s (clients=%d)", nm, reason, self._clientCount() - 1)
      if cli.authorized and cli.charName:
        self._broadcastSystem(f"-- {cli.charName} has left the server.")
    finally:
      try:
        self.sel.unregister(cli.sock)
      except Exception:
        pass
      try:
        cli.sock.close()
      except Exception:
        pass
      self.clients.pop(cli.sock, None)

  def _closeAll(self) -> None:
    for cli in list(self.clients.values()):
      self._disconnect(cli, reason="server shutdown")

  def _clientCount(self) -> int:
    return sum(1 for c in self.clients.values() if c.authorized)

# -------- CLI ----------------------------------------------------------------
def main() -> int:
  parser = argparse.ArgumentParser(description="EQBCS-like server in Python")
  parser.add_argument("-p", "--port", type=int, default=eqbcDefaultPort)
  parser.add_argument("-i", "--bind", default="0.0.0.0")
  parser.add_argument("-l", "--logfile", default=None)
  parser.add_argument("-v", "--debug", action="store_true", help="enable verbose debug logging")
  args = parser.parse_args()

  if not (0 < args.port <= maxPortSize):
    print(f"Invalid port: {args.port}", file=sys.stderr)
    return 2

  # ENV overrides flags (for Docker Compose)
  envLogLevel = _parseLogLevel(os.getenv("EQBCS_LOG_LEVEL") or os.getenv("LOG_LEVEL"))
  level = envLogLevel if envLogLevel is not None else (logging.DEBUG if args.debug else logging.INFO)
  logFile = os.getenv("EQBCS_LOG_FILE") or args.logfile
  # NEW: keepalive logging gate (default off)
  logKeepalive = (os.getenv("EQBCS_LOG_KEEPALIVE", "").strip().lower() in ("1","true","yes","on"))

  setupLogger(logFile, level)

  srv = EqbcsPyServer(args.bind, args.port, logKeepalive=logKeepalive)

  # allow SIGTERM/SIGINT to stop cleanly (useful in containers)
  def _stop(signum, frame):
    srv.request_stop(reason=f"signal {signum}")
  try:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
  except Exception:
    pass

  srv.start()
  return 0

if __name__ == "__main__":
  sys.exit(main())
