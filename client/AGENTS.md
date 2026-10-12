# AGENTS.md — client (for AI coding agents: Claude Code, Codex, Cursor ...)

You are running the **client** of the AI video pipeline on the user's PC. The heavy work (ComfyUI, models, the web
app) is on a separate **server PC**; this client talks to it over **UDP** (default port 9999). Human guide: `README.md`.

## Run
1. Ask the user for the server's **IP**, **port** (default 9999) and **password** (from the server's
   `udp_server/server_config.json`).
2. Windows: run `AIVideoClient.exe` (no Python needed). Or from source with Python 3.11+: `python udp_client.py`
   (`run_client.bat`) — standard library only (tkinter, socket, http.server).
3. In the window: fill IP / port / password → **연결**. Success text: "연결됨 · ComfyUI 켜짐 · Ollama 켜짐".
4. **🌐 웹 화면 열기** opens the server's whole web UI in this PC's browser at `http://127.0.0.1:8765` (a local relay,
   `web_tunnel.py`, forwards every browser request over UDP). All settings are there: place, people, voices, per-cut
   line / motion / tone / framing / camera / prompt, uploads, the admin screen (`/?admin=1`).

## Check the connection without the window
```python
from udp_client import UdpClient
c = UdpClient("SERVER_IP", 9999, "PASSWORD")
print(c.request("ping"))          # {'ok': True, 'app': True, 'comfyui': True, 'ollama': True, ...}
print(c.request("catalog"))       # presets
```
"서버가 응답하지 않아요" = wrong IP/port, server not running, or the server's firewall blocks inbound UDP 9999.

## Files
- `udp_client.py` — window + `UdpClient` (request/resend, chunked downloads with SHA-256, `http()` for the relay)
- `web_tunnel.py` — local HTTP relay (127.0.0.1 only) → UDP; caches pictures/clips 10 min, answers video Range requests
- `proto.py` — datagram format, identical to the server's copy
- `build.bat` — rebuilds `AIVideoClient.exe` with PyInstaller (creates `.buildenv` itself)
