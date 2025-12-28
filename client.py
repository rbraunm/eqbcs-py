#!/usr/bin/env python3
"""
EQ server web client (single-file)

- Backend: Flask + Flask-Sock websocket endpoint that opens a raw TCP socket
  to the EQ server and proxies the text-based wire protocol defined in server.py.

- Frontend: single-page dark-themed UI (served inline) that shows:
  - roster (list of characters)
  - controls (Names, NBNAMES, Disconnect, LocalEcho toggle)
  - chat box + message type selector (MSGALL, NBMSG, TELL, BCI, CHANNELS)
  - raw server log output

Usage:
  pip install flask flask_sock
  python eq_client.py --host 127.0.0.1 --port 2112 --name MyChar [--password secret]

Then open http://127.0.0.1:8080/ in your browser, connect using the UI, and the
client will perform the LOGIN handshake and proxy commands over the EQ wire
protocol exactly as the server expects.

Notes:
  - The backend <-> server speaks the original text wire protocol (LOGIN, \t CMD,
    typed frames, etc.). The browser <-> backend uses a websocket and JSON; this
    keeps the server protocol untouched on the wire between the backend and the
    EQ server.

This file intentionally stays small and dependency-light.
"""

from __future__ import annotations
import argparse
import socket
import threading
import time
import json
from typing import Optional

from flask import Flask, render_template_string, request
from flask_sock import Sock

# ---------------- simple helpers ----------------

def _sendLine(sock: socket.socket, line: str) -> None:
  payload = line.encode("utf-8", "ignore")
  if not payload.endswith(b"\n"):
    payload += b"\n"
  sock.sendall(payload)

# ---------------- flask app ----------------
app = Flask(__name__)
sock = Sock(app)

INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>EQ Web Client</title>
<style>
  :root{--bg:#0b0f12;--panel:#0f1518;--muted:#9aa7b0;--accent:#7c9bf2;--glass:rgba(255,255,255,0.03)}
  html,body{height:100%;margin:0;background:var(--bg);color:#e6eef6;font-family:Inter,Segoe UI,Helvetica,Arial}
  .wrap{display:grid;grid-template-columns:320px 1fr;gap:12px;padding:12px;height:100%}
  .panel{background:linear-gradient(180deg,var(--panel),#0b1114);border-radius:10px;padding:12px;box-shadow:0 6px 20px rgba(0,0,0,0.6)}
  .roster{height:32vh;overflow:auto}
  .controls{display:flex;flex-wrap:wrap;gap:8px}
  button{background:var(--glass);border:1px solid rgba(255,255,255,0.03);padding:8px 10px;border-radius:8px;color:inherit}
  button.tog{padding:6px 10px}
  .log{height:56vh;overflow:auto;font-family:monospace;background:rgba(0,0,0,0.2);padding:8px;border-radius:8px}
  .chatbar{display:flex;gap:8px;margin-top:8px}
  select,input[type=text]{background:transparent;border:1px solid rgba(255,255,255,0.06);padding:8px;border-radius:8px;color:inherit}
  .big{font-size:1.05rem}
  .status{font-size:0.9rem;color:var(--muted);margin-bottom:8px}
</style>
</head>
<body>
<div class="wrap">
  <div class="panel">
    <div class="status" id="status">Disconnected</div>
    <div style="margin-bottom:8px">
      <label class="big">Character</label>
      <div style="display:flex;gap:8px;margin-top:6px">
        <input id="charName" type="text" placeholder="Character name"/>
        <input id="password" type="text" placeholder="Password (optional)"/>
        <button id="connectBtn">Connect</button>
      </div>
    </div>
    <div class="controls" style="margin-bottom:8px">
      <button id="namesBtn">NAMES</button>
      <button id="nbnamesBtn">NBNAMES</button>
      <button id="disconnectBtn">DISCONNECT</button>
      <button id="localechoBtn" class="tog">LocalEcho: <span id="echoState">OFF</span></button>
    </div>
    <h4>Roster</h4>
    <div class="roster" id="roster"></div>
    <h4 style="margin-top:8px">Send</h4>
    <div>
      <div style="display:flex;gap:8px;align-items:center">
        <select id="msgType">
          <option value="MSGALL">MSGALL</option>
          <option value="NBMSG">NBMSG</option>
          <option value="TELL">TELL</option>
          <option value="BCI">BCI</option>
          <option value="CHANNELS">CHANNELS</option>
        </select>
        <input id="target" type="text" placeholder="target / channels (for TELL/BCI/CHANNELS)" style="flex:1"/>
      </div>
      <div class="chatbar">
        <input id="msg" type="text" placeholder="Type message and press Send or Enter" style="flex:1"/>
        <button id="sendBtn">Send</button>
      </div>
    </div>
  </div>

  <div class="panel">
    <h3>Server Log</h3>
    <div class="log" id="log"></div>
  </div>
</div>
<script>
let ws = null;
let localEcho = false;
function appendLog(s){const el=document.getElementById('log');el.innerText+=s+'\n';el.scrollTop=el.scrollHeight}
function setStatus(s){document.getElementById('status').innerText=s}
function setRoster(names){const el=document.getElementById('roster');el.innerHTML='';names.forEach(n=>{const d=document.createElement('div');d.innerText=n;el.appendChild(d)})}

function wsSend(obj){if(!ws || ws.readyState!==1)return;ws.send(JSON.stringify(obj))}

document.getElementById('connectBtn').onclick = ()=>{
  const c=document.getElementById('charName').value.trim();
  const p=document.getElementById('password').value;
  if(!c){alert('Enter a character name');return}
  if(ws) ws.close();
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
  ws.onopen = ()=>{setStatus('Connected to proxy'); appendLog('[frontend] websocket open'); wsSend({type:'login',name:c,password:p})}
  ws.onmessage = (ev)=>{
    try{const msg=JSON.parse(ev.data);
      if(msg.type==='line'){appendLog(msg.text)}
      else if(msg.type==='roster'){setRoster(msg.names);appendLog('[roster updated] '+msg.names.join(', '))}
      else if(msg.type==='status'){setStatus(msg.status)}
    }catch(e){appendLog('[raw] '+ev.data)}
  }
  ws.onclose = ()=>{setStatus('Disconnected');appendLog('[frontend] websocket closed')}
}

document.getElementById('namesBtn').onclick = ()=>{wsSend({type:'cmd','cmd':'NAMES'})}
document.getElementById('nbnamesBtn').onclick = ()=>{wsSend({type:'cmd','cmd':'NBNAMES'})}

document.getElementById('disconnectBtn').onclick = ()=>{wsSend({type:'cmd','cmd':'DISCONNECT'})}

document.getElementById('localechoBtn').onclick = ()=>{
  localEcho = !localEcho; document.getElementById('echoState').innerText = localEcho? 'ON':'OFF'; wsSend({type:'cmd','cmd':'LOCALECHO','arg': localEcho? '1':'0'})
}

function doSend(){const mt=document.getElementById('msgType').value;const payload=document.getElementById('msg').value;const target=document.getElementById('target').value;
  if(!payload && mt!=='CHANNELS'){return}
  if(mt==='MSGALL' || mt==='NBMSG'){
    // arm-and-next-line mode
    wsSend({type:'arm',cmd:mt});
    wsSend({type:'line',text:payload});
  }else if(mt==='TELL' || mt==='BCI'){
    wsSend({type:'arm',cmd:mt});
    wsSend({type:'line',text: (target||'') + (payload? ' '+payload : '')});
  }else if(mt==='CHANNELS'){
    wsSend({type:'arm',cmd:mt});
    wsSend({type:'line',text: target || payload});
  }
  document.getElementById('msg').value='';
}

document.getElementById('sendBtn').onclick = doSend;
document.getElementById('msg').addEventListener('keydown', (e)=>{if(e.key==='Enter'){doSend()}});
</script>
</body>
</html>
"""

# ---------------- websocket proxy ----------------

class TcpProxy:
  def __init__(self, host: str, port: int):
    self.host = host
    self.port = port
    self.sock: Optional[socket.socket] = None
    self.lock = threading.Lock()

  def connect(self, timeout: float = 5.0) -> None:
    with self.lock:
      if self.sock:
        try:
          self.sock.close()
        except Exception:
          pass
        self.sock = None
      s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      s.settimeout(timeout)
      s.connect((self.host, self.port))
      s.settimeout(None)
      self.sock = s

  def close(self) -> None:
    with self.lock:
      if self.sock:
        try:
          self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
          pass
        try:
          self.sock.close()
        except Exception:
          pass
        self.sock = None

  def send_line(self, line: str) -> None:
    with self.lock:
      if not self.sock:
        raise RuntimeError('not connected')
      _sendLine(self.sock, line)

  def recv_chunks(self):
    # generator that yields decoded lines as they arrive
    buf = bytearray()
    while True:
      with self.lock:
        s = self.sock
      if not s:
        break
      try:
        data = s.recv(4096)
      except Exception:
        break
      if not data:
        break
      buf.extend(data)
      while True:
        nl = buf.find(b"\n")
        if nl < 0:
          break
        raw = buf[:nl].rstrip(b"\r")
        del buf[:nl+1]
        try:
          line = raw.decode('utf-8', 'ignore')
        except Exception:
          line = ''
        yield line

# Single global proxy instance per websocket connection handled in handler

@sock.route('/ws')
def ws_proxy(ws):
  # simple per-connection state
  proxy: Optional[TcpProxy] = None
  tcp_thread = None
  stop_flag = threading.Event()

  def tcp_reader_loop():
    try:
      if not proxy:
        return
      for line in proxy.recv_chunks():
        # detect NBCLIENTLIST to update roster for frontend
        if line.startswith('\tNBCLIENTLIST='):
          names = line.split('=',1)[1].strip()
          names_list = names.split() if names else []
          ws.send(json.dumps({'type':'roster','names':names_list}))
        # forward every line as raw text message
        ws.send(json.dumps({'type':'line','text':line}))
    except Exception as e:
      try:
        ws.send(json.dumps({'type':'line','text': f'-- proxy recv error: {e}'}))
      except Exception:
        pass
    finally:
      # notify frontend that underlying TCP disconnected
      try:
        ws.send(json.dumps({'type':'status','status':'Disconnected from server'}))
      except Exception:
        pass

  try:
    while True:
      data = ws.receive()
      if data is None:
        break
      try:
        msg = json.loads(data)
      except Exception:
        ws.send(json.dumps({'type':'line','text':'-- invalid json from frontend'}))
        continue

      mtype = msg.get('type')
      if mtype == 'login':
        # create and connect tcp proxy then send LOGIN form
        host = request.args.get('host','127.0.0.1')
        port = int(request.args.get('port','2112'))
        name = msg.get('name','')
        password = msg.get('password')
        proxy = TcpProxy(host, port)
        try:
          proxy.connect()
        except Exception as e:
          ws.send(json.dumps({'type':'line','text': f'-- tcp connect failed: {e}'}))
          continue
        # start reader thread
        stop_flag.clear()
        tcp_thread = threading.Thread(target=tcp_reader_loop, daemon=True)
        tcp_thread.start()
        # send login
        if password:
          _sendLine(proxy.sock, f'LOGIN:{password}={name};')
        else:
          _sendLine(proxy.sock, f'LOGIN={name};')
        ws.send(json.dumps({'type':'status','status':'Connected to server (login sent)'}))

      elif mtype == 'cmd':
        if not proxy or not proxy.sock:
          ws.send(json.dumps({'type':'line','text':'-- not connected to server'}))
          continue
        cmd = msg.get('cmd','')
        arg = msg.get('arg','')
        if arg:
          _sendLine(proxy.sock, f'\t{cmd} {arg}')
        else:
          _sendLine(proxy.sock, f'\t{cmd}')

      elif mtype == 'arm':
        # arm-and-next-line: send the preface then wait for next 'line' msg
        if not proxy or not proxy.sock:
          ws.send(json.dumps({'type':'line','text':'-- not connected to server'}))
          continue
        cmd = msg.get('cmd','')
        _sendLine(proxy.sock, f'\t{cmd}')

      elif mtype == 'line':
        # the subsequent raw payload line for an armed command (or general text)
        if not proxy or not proxy.sock:
          ws.send(json.dumps({'type':'line','text':'-- not connected to server'}))
          continue
        text = msg.get('text','')
        _sendLine(proxy.sock, text)

      else:
        ws.send(json.dumps({'type':'line','text':'-- unknown frontend message type'}))

  except Exception as e:
    try:
      ws.send(json.dumps({'type':'line','text':f'-- websocket handler error: {e}'}))
    except Exception:
      pass
  finally:
    # cleanup
    try:
      if proxy:
        proxy.close()
    except Exception:
      pass

# ---------------- http routes ----------------

@app.route('/')
def index():
  return render_template_string(INDEX_HTML)

# ---------------- CLI ----------------

def main():
  ap = argparse.ArgumentParser(description='EQ web client proxy')
  ap.add_argument('--host', default='127.0.0.1', help='EQ server host')
  ap.add_argument('--port', type=int, default=2112, help='EQ server port')
  ap.add_argument('--bind', default='0.0.0.0', help='HTTP bind host')
  ap.add_argument('--http-port', type=int, default=8080, help='HTTP port')
  args = ap.parse_args()

  # Pass host/port via query params on websocket connect so the WS handler can use them
  print(f'Visit http://{args.bind}:{args.http_port}/')
  # Run built-in Flask server (fine for dev). In production use gunicorn/uvicorn.
  app.run(host=args.bind, port=args.http_port, debug=False)

if __name__ == '__main__':
  main()
