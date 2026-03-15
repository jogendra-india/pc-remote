# Remote Desktop Server

Control your computer from any browser — phone, tablet, or another machine.
Works on **macOS**, **Windows**, and **Linux**.

Supports two access modes:

- **LAN mode** — direct access over your local network (`http://192.168.x.x:5050`)
- **Tunnel mode** — access over the public internet via a relay server on GCP (or any VM)

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Platform Support](#platform-support)
4. [Prerequisites](#prerequisites)
5. [Quick Start — LAN Mode](#quick-start--lan-mode)
6. [Tunnel Mode — Internet Access](#tunnel-mode--internet-access)
7. [Interaction Modes](#interaction-modes)
8. [Toolbar Reference](#toolbar-reference)
9. [Virtual Keyboard](#virtual-keyboard)
10. [Virtual Trackpad](#virtual-trackpad)
11. [Clipboard Sync](#clipboard-sync)
12. [File Transfer](#file-transfer)
13. [Stream Settings](#stream-settings)
14. [Sleep Prevention](#sleep-prevention)
15. [Security Considerations](#security-considerations)
16. [Troubleshooting](#troubleshooting)
17. [Tech Stack](#tech-stack)

---

## Features

- **Live screen streaming** via WebSocket (adjustable FPS, quality, scale, format)
- **Mouse control** — move, left/right click, double-click, triple-click, drag, scroll
- **Keyboard input** — full key support with modifier remapping (Ctrl on Windows/Linux maps to Cmd on macOS)
- **Virtual Trackpad** — drag-to-move cursor with click, double-click, drag-hold, and scroll
- **Virtual Keyboard** — sticky modifiers, developer shortcuts, function keys, text input
- **Native Keyboard (NKB)** — use your device's soft keyboard with sticky modifier support
- **File Transfer** — upload files from client, browse and download files from host
- **Clipboard Sync** — automatic bidirectional clipboard synchronization between host and client
- **Zoom & Pan** — pinch-to-zoom on mobile, Ctrl+scroll on desktop, two-finger pan
- **Audio Streaming** — stream system audio from host to client browser
- **Sleep Prevention** — keeps host awake while the server runs
- **Cross-platform** — auto-detects macOS, Windows, or Linux and uses native APIs
- **Mobile-friendly** — responsive dark UI with Direct and Trackpad interaction modes

---

## Architecture

The system has two deployment modes. Both share the same `server.py` on the host machine.

### LAN Mode (direct access)

```
 +-----------------+          HTTP + WebSocket           +------------------+
 | Client Browser  | <=================================> | server.py (5050) |
 | (phone/laptop)  |         same local network          | Host Machine     |
 +-----------------+                                     +------------------+
                                                                 |
                                                          +------+------+
                                                          | Host OS     |
                                                          | - Screen    |
                                                          | - Mouse     |
                                                          | - Keyboard  |
                                                          | - Clipboard |
                                                          +-------------+
```

### Tunnel Mode (internet access via GCP relay)

```
 +-----------------+     HTTPS/WSS      +-------+    HTTP     +------------------+
 | Client Browser  | =================> | Nginx | ==========> | server.js (9000) |
 | (anywhere)      |     (TLS)          | (GCP) |  proxy pass | Tunnel Relay     |
 +-----------------+                    +-------+             +--------+---------+
                                                                       |
                                                              Persistent WSS
                                                              tunnel connection
                                                                       |
                                                            +----------+----------+
                                                            | create_tunnel.py    |
                                                            | (Host Machine)      |
                                                            +----------+----------+
                                                                       |
                                                              Local HTTP + WS
                                                                       |
                                                            +----------+----------+
                                                            | server.py (5050)    |
                                                            | (Host Machine)      |
                                                            +----------+----------+
                                                                       |
                                                                +------+------+
                                                                | Host OS     |
                                                                | - Screen    |
                                                                | - Mouse     |
                                                                | - Keyboard  |
                                                                | - Clipboard |
                                                                +-------------+
```

**Four components in tunnel mode:**

| Component | Runs on | Role |
|---|---|---|
| `server.py` | Host machine | Flask + Socket.IO — captures screen, handles input, manages clipboard |
| `create_tunnel.py` | Host machine | Connects to GCP via WSS, proxies HTTP and WebSocket traffic to `server.py` |
| `server.js` | GCP (public VM) | Node.js relay — multiplexes HTTP and WebSocket traffic over a single tunnel |
| Nginx | GCP (public VM) | TLS termination, reverse proxies to `server.js` on port 9000 |

### Data Flow — How a Frame Gets to the Browser

```
 1. server.py captures screen using mss
 2. Composites cursor overlay onto the frame
 3. Encodes to JPEG/WebP/PNG, base64-encodes
 4. Emits via Socket.IO WebSocket to local client (create_tunnel.py)

                         +-- TUNNEL PATH (internet) --+
                         |                             |
 5. create_tunnel.py receives the frame
 6. Applies frame-drop backpressure (keeps only latest frame, drops stale ones)
 7. Wraps in JSON envelope {"type":"ws_frame","wsId":"...","data":"..."}
 8. Sends to server.js via persistent WSS tunnel

 9. server.js unwraps the JSON and forwards raw data to the browser's WebSocket

 10. Browser decodes base64 image, draws on canvas
```

### Data Flow — How Input Reaches the Host

```
 1. Browser captures mouse/keyboard/touch event
 2. Emits via Socket.IO (e.g. "click", "keydown", "hotkey")
 3. server.js forwards to create_tunnel.py via tunnel WebSocket
 4. create_tunnel.py forwards to server.py via local WebSocket
 5. server.py translates to OS calls (Quartz/pyautogui/ctypes)
```

### Clipboard Sync Flow

```
 Host clipboard changes
       |
       v
 server.py polls clipboard every 5s
       |
       v (clipboard_changed event)
 create_tunnel.py --> server.js --> Browser
       |
       v
 Browser writes to navigator.clipboard

 ---

 User copies on client browser (Ctrl+C / Cmd+C)
       |
       v
 Browser sends hotkey --> server.py executes Cmd+C on host
       |
       v (250ms later)
 Browser requests clipboard_get --> server.py reads clipboard --> sends back
       |
       v
 Browser writes to navigator.clipboard

 ---

 User pastes on client browser (Ctrl+V / Cmd+V)
       |
       v
 Browser reads own clipboard via navigator.clipboard.readText()
       |
       v
 Sends clipboard_set to server.py (sets host clipboard)
       |
       v (50ms later)
 Sends hotkey Cmd+V --> server.py executes paste on host
```

---

## Platform Support

| Feature | macOS | Windows | Linux (Ubuntu) |
|---|---|---|---|
| Mouse control | Quartz CoreGraphics | ctypes (user32) | pyautogui |
| Keyboard | pyautogui | pyautogui | pyautogui |
| Ctrl remapping | Ctrl on client = Cmd on host | Native Ctrl | Native Ctrl |
| Screen capture | mss | mss | mss |
| Cursor capture | Quartz (position) + PIL (drawn) | Win32 GetCursorInfo + render | XFixes (X11) or PIL fallback |
| Clipboard read | `pbpaste` | `Get-Clipboard` (PowerShell) | `xclip -o` |
| Clipboard write | `pbcopy` | `clip` | `xclip` |
| Sleep prevention | `caffeinate` | `SetThreadExecutionState` | `systemd-inhibit` |
| Accessibility | `AXIsProcessTrusted` | Not needed | Not needed |

---

## Prerequisites

- **Python 3.10+** on the host machine
- **Node.js 18+** on the GCP server (tunnel mode only)

### macOS

- **Accessibility permissions**: System Settings > Privacy & Security > Accessibility — add your terminal app
- **Screen Recording permissions**: System Settings > Privacy & Security > Screen Recording — add your terminal app

### Windows

- No special permissions required

### Linux (Ubuntu / Debian)

```bash
sudo apt install xclip scrot python3-tk python3-dev
```

> The server uses X11 via `mss` and `pyautogui`. Wayland sessions need XWayland enabled.

---

## Quick Start — LAN Mode

```bash
cd macbook-remote

python3 -m venv .venv

# macOS / Linux:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate

pip install -r requirements.txt

python server.py
```

Output:

```
======================================================
  Remote Desktop Server (macOS)
======================================================
  Local:   http://localhost:5050
  Network: http://192.168.x.x:5050
======================================================
  Open the Network URL on your phone / tablet
  (device must be on the same Wi-Fi network)
======================================================
```

Open the **Network URL** on your phone or another device on the same Wi-Fi.

---

## Tunnel Mode — Internet Access

Use tunnel mode to access the remote desktop from anywhere over the internet.

### Step 1: Set up the GCP server

On a public VM (GCP, AWS, DigitalOcean, etc.) with a domain and TLS:

```bash
# Create project directory
mkdir -p /opt/tunnel && cd /opt/tunnel

# Initialize and install dependency
npm init -y
npm install ws

# Copy server.js into /opt/tunnel/
```

### Step 2: Configure Nginx (TLS termination)

Example Nginx config:

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location /tunnel/ {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }
}
```

### Step 3: Start the relay server

```bash
node server.js
```

Expected output:

```
Tunnel server running on port 9000
Rate limits: 120 HTTP/min, 10 concurrent WS per IP
```

For production, use `pm2` or `systemd` to keep it running:

```bash
# Using pm2
npm install -g pm2
pm2 start server.js --name tunnel-relay
pm2 save
pm2 startup

# Using systemd
# Create /etc/systemd/system/tunnel-relay.service:
#   [Unit]
#   Description=Tunnel Relay
#   After=network.target
#   [Service]
#   ExecStart=/usr/bin/node /opt/tunnel/server.js
#   Restart=always
#   User=your-user
#   [Install]
#   WantedBy=multi-user.target
#
# Then: systemctl enable tunnel-relay && systemctl start tunnel-relay
```

### Step 4: Configure and start the host-side tunnel client

Edit `create_tunnel.py` and set your server URL:

```python
SERVER = "wss://your-domain.com/tunnel/register?id=xyz"
```

Then start all host-side services:

```bash
# Terminal 1 — start the remote desktop server
python server.py

# Terminal 2 — start the tunnel client
python create_tunnel.py
```

You should see:

```
Tunnel connected to wss://your-domain.com/tunnel/register?id=xyz
```

### Step 5: Access from browser

Open `https://your-domain.com/tunnel/xyz` from any browser, anywhere.

### Startup order summary

| Order | Command | Where |
|---|---|---|
| 1 | `node server.js` | GCP server |
| 2 | `python server.py` | Host machine |
| 3 | `python create_tunnel.py` | Host machine |
| 4 | Open browser URL | Client device |

### Rate limiting

`server.js` includes built-in rate limiting:

- **120 HTTP requests** per minute per IP
- **10 concurrent WebSocket connections** per IP
- Stale entries are cleaned up every 60 seconds

### Performance tuning for tunnel mode

Tunnel adds network latency. To minimize perceived lag:

| Setting | Recommended for tunnel |
|---|---|
| Format | JPEG (fastest encode, ~3x faster than WebP) |
| FPS | 10-15 (reduces bandwidth pressure) |
| Quality | 40-60 (good balance) |
| Scale | 0.50-0.75 (smaller frames transmit faster) |

---

## Interaction Modes

### Desktop (mouse + keyboard)

| Action | How |
|---|---|
| Move cursor | Move mouse over the canvas |
| Left click | Click |
| Right click | Right-click (or Ctrl+click) |
| Double click | Double-click |
| Triple click | Triple-click (selects entire line) |
| Drag | Click and hold, then move |
| Scroll | Mouse wheel |
| Zoom | Ctrl + mouse wheel |
| Keyboard | Type with canvas focused |
| Ctrl+C / Ctrl+A etc. | Automatically remapped to Cmd on macOS host |

### Mobile / Tablet

Toggle between modes using the **Direct / Trackpad** button:

- **Direct mode** — touch position maps directly to cursor position
- **Trackpad mode** — drag to move the cursor relatively (like a laptop trackpad)

| Action | How |
|---|---|
| Move cursor | Drag on screen |
| Left click | Tap |
| Right click | Use trackpad panel button |
| Double click | Double-tap, or trackpad panel button |
| Drag | Tap-tap-hold, or toggle the Drag button |
| Scroll | Two-finger swipe, or Scroll buttons |
| Zoom | Pinch with two fingers |
| Pan (zoomed) | Two-finger drag |

---

## Toolbar Reference

| Button | Function |
|---|---|
| **Direct / Trackpad** | Toggle touch interaction mode |
| **Pad** | Virtual trackpad panel |
| **Keys** | Virtual keyboard panel |
| **Clip** | Clipboard sync panel |
| **Files** | File transfer panel |
| **NKB** | Toggle native soft keyboard |
| **Audio** | Toggle system audio streaming |
| **Screenshot** | Save current frame as PNG |
| **Fullscreen** | Toggle fullscreen mode |
| **Settings** | Stream settings (FPS, quality, scale, format) |
| **-/+/Fit** | Zoom controls |

---

## Virtual Keyboard

- **Sticky modifiers** — tap Ctrl, Alt, Cmd, or Shift to hold them. Auto-release after the next key press.
- **Developer shortcuts** — common combos: Cmd+S, Cmd+C, Cmd+V, Cmd+Z, Cmd+Shift+P, Cmd+F, etc.
- **Navigation keys** — Esc, Tab, Backspace, Delete, Enter, Space, Home, End, PgUp, PgDn
- **Arrow keys** — Left, Down, Up, Right
- **Function keys** — F1 through F12
- **Text input** — type a string and send it at once

**NKB + Sticky Modifiers**: Activate Cmd in the Keys panel, then open NKB and press `a` — sends Cmd+A (select all).

---

## Virtual Trackpad

- **Touch area** — drag to move cursor relatively, tap to click
- **Left Click / Right Click / Double Click** — explicit click buttons
- **Drag** — toggle drag mode (mousedown on touch, drag on move, mouseup on release)
- **Tap-tap-hold** — natural gesture: tap once, then quickly tap and hold to start dragging
- **Scroll Up/Down** — scroll buttons

---

## Clipboard Sync

Clipboard synchronization works automatically in both directions:

**Host to client**: When you copy something on the host machine, the server detects the clipboard change (polls every 5 seconds) and pushes it to the client browser, which writes it to the browser's clipboard.

**Client to host**: When you press Ctrl+V / Cmd+V in the remote session, the browser first reads your local clipboard, sends the text to the host, sets the host clipboard, then triggers paste.

**Manual clipboard panel**:

| Button | What it does |
|---|---|
| **Read Phone Clipboard** | Reads from your device clipboard into the text area |
| **Get PC Clipboard** | Fetches current host clipboard content |
| **Set PC Clipboard** | Sets host clipboard to text area content |
| **Send to PC & Paste** | Sets host clipboard AND triggers paste |

> **Note**: `navigator.clipboard.readText()` requires HTTPS or localhost. On HTTP, use the manual paste method.

---

## File Transfer

- **Upload to PC** — select files and upload to `~/Desktop/RemoteUploads/` (max 500 MB per file)
- **Browse files** — navigate the host's home directory
- **Download** — tap the download arrow next to any file

Files are restricted to the user's home directory for security.

---

## Stream Settings

Click the gear icon to adjust:

| Setting | Range | Default | Description |
|---|---|---|---|
| FPS | 1–60 | 15 | Frames per second |
| Quality | 10–100 | 70 | JPEG/WebP quality |
| Scale | 0.10–2.00 | 0.75 | Resolution multiplier |
| Format | JPEG/WebP/PNG | WebP | Image format |
| Cursor Sensitivity | 0.1–5.0 | 2.0 | Trackpad cursor speed |

Settings are saved to localStorage and restored on reconnect.

---

## Sleep Prevention

The server prevents the host from sleeping while running:

| Platform | Method |
|---|---|
| macOS | `caffeinate -dis` |
| Windows | `SetThreadExecutionState` with `ES_CONTINUOUS + ES_SYSTEM_REQUIRED + ES_DISPLAY_REQUIRED` |
| Linux | `systemd-inhibit --what=idle:sleep` |

Sleep is re-enabled automatically when the server stops.

---

## Security Considerations

- **No built-in authentication** — anyone with the URL can control the machine
- **LAN mode**: only accessible from the same network (acceptable for home use)
- **Tunnel mode**: exposed to the internet — you **must** add authentication
  - Use Nginx basic auth, OAuth proxy, or a VPN (Tailscale, WireGuard, ZeroTier)
- **Rate limiting**: `server.js` limits HTTP and WebSocket connections per IP
- **File access**: restricted to the user's home directory
- **Clipboard**: only text is synced, no binary/image clipboard data

---

## Troubleshooting

| Issue | Fix |
|---|---|
| Cursor won't move (macOS) | Grant Accessibility permissions to your terminal app |
| Black screen (macOS) | Grant Screen Recording permissions to your terminal app |
| Cursor won't move (Linux) | Ensure X11 is running (Wayland needs XWayland) |
| Clipboard not working (Linux) | Install `xclip`: `sudo apt install xclip` |
| High latency via tunnel | Use JPEG format, lower FPS/quality/scale |
| Can't connect from phone | Ensure same Wi-Fi/LAN, no firewall blocking port 5050 |
| Port 5050 in use | `lsof -ti:5050 \| xargs kill -9` (macOS/Linux) |
| Phone clipboard read fails | Requires HTTPS or localhost. Use manual paste instead. |
| Ctrl+C doesn't copy (tunnel to Mac) | Ctrl is auto-remapped to Cmd. This is working correctly. |
| `create_tunnel.py` gets HTTP 502 | Ensure `SERVER` URL matches `server.js` endpoint. Check server.js is running. |
| Bus error on macOS | Restart `server.py`. If persists, check Python/Quartz version compatibility. |
| Frozen stream via tunnel | Restart `create_tunnel.py`. Check frame-drop backpressure is active. |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Host backend | Python 3, Flask, Flask-SocketIO |
| Screen capture | [mss](https://github.com/BoboTiG/python-mss) (cross-platform) |
| Mouse control | Quartz CoreGraphics (macOS), ctypes user32 (Windows), pyautogui (Linux) |
| Keyboard control | pyautogui (all platforms) |
| Image processing | Pillow (PIL) |
| Tunnel relay | Node.js, [ws](https://github.com/websockets/ws) |
| Tunnel client | Python asyncio, [websockets](https://github.com/python-websockets/websockets) |
| TLS termination | Nginx with Let's Encrypt |
| Frontend | HTML5 Canvas, vanilla JavaScript, Socket.IO 4.x client |
| Audio | sounddevice (optional, host-side capture) |

---

## Project Structure

```
macbook-remote/
  server.py            Host remote desktop server (Flask + Socket.IO)
  create_tunnel.py     Tunnel client (connects host to GCP relay)
  server.js            Tunnel relay server (runs on GCP)
  requirements.txt     Python dependencies
  templates/
    index.html         Browser client UI (HTML + CSS + JS)
  README.md            This file
```
