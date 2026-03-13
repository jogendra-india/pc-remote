# Remote Desktop Server

Control your computer from any browser on the same network — phone, tablet, or another machine. Works on **macOS**, **Windows**, and **Linux (Ubuntu)**.

## Features

- **Live screen streaming** via WebSocket (adjustable FPS, quality, resolution)
- **Mouse control** — move cursor, left/right click, double-click, drag, scroll
- **Keyboard input** — full key support including modifier combos, function keys, arrows
- **Virtual Trackpad** — drag-to-move cursor with click, double-click, and drag-hold support
- **Virtual Keyboard** — sticky modifiers (Ctrl, Alt, Cmd/Win, Shift), developer shortcuts, function keys, text input
- **Native Keyboard (NKB)** — use your phone's soft keyboard with sticky modifier support
- **File Transfer** — upload files from phone to desktop, browse & download files from desktop
- **Clipboard Sync** — read/write clipboard on both the remote machine and the client device
- **Zoom & Pan** — pinch-to-zoom on mobile, Ctrl+scroll on desktop, two-finger pan when zoomed
- **Sleep Prevention** — keeps the host machine awake while the server is running (like Caffeine)
- **Cross-platform** — auto-detects macOS, Windows, or Linux and uses the right APIs
- **Mobile-friendly** — responsive dark UI with Direct and Trackpad interaction modes

## Platform Support

| Feature | macOS | Windows | Linux (Ubuntu) |
|---|---|---|---|
| Mouse control | Quartz CoreGraphics | pyautogui | pyautogui |
| Keyboard | pyautogui | pyautogui | pyautogui |
| Cmd key mapping | `Cmd` (native) | `Ctrl` (Cmd→Ctrl) | `Ctrl` (Cmd→Ctrl) |
| Screen capture | mss | mss | mss |
| Clipboard read | `pbpaste` | `Get-Clipboard` (PowerShell) | `xclip -o` |
| Clipboard write | `pbcopy` | `clip` | `xclip` |
| Sleep prevention | `caffeinate` | `SetThreadExecutionState` | `systemd-inhibit` |
| Accessibility check | `AXIsProcessTrusted` | Not needed | Not needed |

## Prerequisites

- **Python 3.10+**
- Platform-specific requirements below

### macOS

- **Accessibility permissions**: the terminal app running the server must have Accessibility access
  - Go to **System Settings → Privacy & Security → Accessibility**
  - Add and enable your terminal app (Terminal.app, iTerm2, VS Code, Cursor, etc.)
- **Screen Recording permissions**: required for screen capture
  - Go to **System Settings → Privacy & Security → Screen Recording**
  - Add and enable your terminal app

### Windows

- No special permissions required
- pyautogui works out of the box on Windows

### Linux (Ubuntu / Debian)

Install required system packages:

```bash
sudo apt install xclip scrot python3-tk python3-dev
```

- `xclip` — clipboard access
- `scrot` — screenshot support for pyautogui
- `python3-tk` — required by pyautogui
- `python3-dev` — build headers for Python packages

> **Note**: The server uses X11 via `mss` and `pyautogui`. Wayland-only sessions may need `XWayland` enabled.

## Quick Start

```bash
cd macbook-remote

# Create a virtual environment
python3 -m venv .venv

# Activate it
# macOS / Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# macOS only — install Quartz bindings (usually already present)
# pip install pyobjc-framework-Quartz

# Run the server
python server.py
```

The server auto-detects the platform and prints:

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

Open the **Network** URL on your phone or another device on the same network.

## Interaction Modes

### Desktop (mouse + keyboard)

| Action | How |
|---|---|
| Move cursor | Move mouse over the canvas |
| Left click | Click |
| Right click | Right-click (or Ctrl+click) |
| Double click | Double-click |
| Drag | Click and drag |
| Scroll | Mouse wheel |
| Zoom | Ctrl + mouse wheel |
| Keyboard | Type with canvas focused |

### Mobile / Tablet

Toggle between modes using the **Direct / Trackpad** button:

**Direct mode** — touch position maps directly to cursor position on screen.

**Trackpad mode** — drag to move the cursor relatively (like a laptop trackpad). Better precision on small screens.

| Action | How |
|---|---|
| Move cursor | Drag on screen |
| Left click | Tap |
| Right click | Use the trackpad panel button |
| Double click | Double-tap, or use the trackpad panel button |
| Drag (scrollbar, etc.) | Tap-tap-hold (quick tap, then tap and hold), or toggle the **Drag** button in the trackpad panel |
| Scroll | Two-finger swipe, or use Scroll ↑/↓ buttons |
| Zoom | Pinch with two fingers |
| Pan (when zoomed) | Two-finger drag |

## Toolbar Buttons

| Button | Function |
|---|---|
| **Direct / Trackpad** | Toggle touch interaction mode |
| **Pad** | Open the virtual trackpad panel with buttons for click, right-click, double-click, drag, and scroll |
| **Keys** | Open the virtual keyboard panel — sticky modifiers, developer shortcuts, navigation keys, function keys, text input |
| **Clip** | Open the clipboard panel — sync clipboard between your phone and the remote machine |
| **Files** | Open the file transfer panel — upload from phone, browse & download files from the machine |
| **NKB** | Toggle native keyboard — use your device's soft keyboard with sticky modifier support |
| **⚙** | Stream settings (FPS, quality, scale) |
| **−/+/Fit** | Zoom controls |

## Virtual Keyboard

The keyboard panel includes:

- **Sticky modifiers** — tap Ctrl, Alt, Cmd, or Shift to hold them. They stay active (highlighted) until the next key press, then auto-release. Tap again to cancel.
- **Developer shortcuts** — common shortcuts like ⌘S, ⌘C, ⌘V, ⌘Z, ⌘⇧P, ⌘F, ⌘/, etc.
- **Navigation keys** — Esc, Tab, Backspace, Delete, Enter, Space, Home, End, PgUp, PgDn
- **Arrow keys** — ←↓↑→
- **Function keys** — F1–F12
- **Text input** — type a string and send it all at once

### NKB + Sticky Modifiers

You can combine them: activate Cmd in the **Keys** panel, then open **NKB** and press `a` on your phone's keyboard — it sends Cmd+A (select all).

## Virtual Trackpad

The trackpad panel provides a touch-sensitive area and control buttons:

- **Touch area** — drag to move the cursor relatively, tap to click
- **Left Click / Right Click / Double Click** — explicit click buttons
- **Drag** — toggle button that enables drag mode (mousedown on touch, drag on move, mouseup on release). Use this to grab scrollbars, resize windows, select text, etc.
- **Tap-tap-hold** — alternative natural gesture: tap once, then quickly tap again and hold — this starts a drag automatically without the Drag button
- **Scroll ↑/↓** — scroll buttons

## Clipboard Sync

The clipboard panel lets you transfer text between your phone and the remote machine:

| Button | What it does |
|---|---|
| **Read Phone Clipboard** | Reads text from your phone's clipboard into the text area (requires HTTPS or localhost) |
| **Get Mac Clipboard** | Fetches the current clipboard content from the remote machine |
| **Set Mac Clipboard** | Sends the text area content to the remote machine's clipboard |
| **Send to Mac & Paste** | Sets the remote clipboard AND sends Cmd+V / Ctrl+V to paste it |

> **Tip**: If "Read Phone Clipboard" doesn't work (non-HTTPS), long-press the text area and paste manually.

## File Transfer

The file panel provides:

- **Upload to Mac** — select files from your phone and upload them to `~/Desktop/RemoteUploads/` on the remote machine (max 500 MB per file)
- **Browse files** — navigate the remote machine's home directory
- **Download** — tap the download arrow next to any file to save it to your phone

Files are restricted to the user's home directory for security.

## Settings

Click the ⚙ gear icon to adjust streaming parameters:

| Setting | Range | Default | Description |
|---|---|---|---|
| FPS | 1–30 | 15 | Frames per second streamed to the browser |
| Quality | 10–100 | 50 | JPEG quality (higher = sharper, more bandwidth) |
| Scale | 0.25–1.0 | 0.50 | Resolution scale (lower = less bandwidth) |

Lower values = less bandwidth, faster on slow networks. Higher values = sharper image, more data.

## Sleep Prevention

The server automatically prevents the host machine from sleeping while running:

- **macOS** — runs `caffeinate -dis` (prevents display dimming, idle sleep, and system sleep)
- **Windows** — calls `SetThreadExecutionState` with `ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED`
- **Linux** — runs `systemd-inhibit --what=idle:sleep`

Sleep is automatically re-enabled when the server stops (Ctrl+C, SIGTERM, or process exit).

## Troubleshooting

| Issue | Fix |
|---|---|
| **Cursor won't move** (macOS) | Grant Accessibility permissions to your terminal app (System Settings → Privacy & Security → Accessibility) |
| **Black screen** (macOS) | Grant Screen Recording permissions (System Settings → Privacy & Security → Screen Recording) |
| **Cursor won't move** (Linux) | Ensure X11 is running (Wayland needs XWayland) |
| **Clipboard not working** (Linux) | Install `xclip`: `sudo apt install xclip` |
| **High latency** | Lower FPS, quality, or scale in settings |
| **Can't connect from phone** | Ensure both devices are on the same Wi-Fi/LAN and no firewall is blocking port 5050 |
| **Port already in use** | Kill the existing process: `lsof -ti:5050 \| xargs kill -9` (macOS/Linux) or `netstat -ano \| findstr :5050` then `taskkill /PID <pid> /F` (Windows) |
| **Phone clipboard "Read" fails** | The `navigator.clipboard.readText()` API requires HTTPS or localhost. Use the manual paste method instead. |
| **Cmd shortcuts don't work** (Windows) | Cmd is auto-mapped to Ctrl on Windows (Cmd+C → Ctrl+C). This is intentional. |

## Network Access Beyond LAN

To access the server from outside your local network:

1. **Port forwarding** — forward port 5050 on your router to the host machine's local IP
2. **VPN** — connect both devices to the same VPN (e.g., Tailscale, WireGuard, ZeroTier)
3. **Reverse proxy** — use ngrok, Cloudflare Tunnel, or similar to expose the local port

> **Security warning**: The server has no authentication. Do not expose it to the public internet without adding authentication (e.g., nginx basic auth, or a VPN).

## Tech Stack

- **Backend**: Python, Flask, Flask-SocketIO
- **Screen capture**: [mss](https://github.com/BoboTiG/python-mss) (cross-platform, fast)
- **Mouse control**: Quartz CoreGraphics (macOS) / pyautogui (Windows/Linux)
- **Keyboard control**: pyautogui (all platforms)
- **Frontend**: HTML5 Canvas, vanilla JavaScript, Socket.IO client
- **Clipboard**: pbcopy/pbpaste (macOS), clip/PowerShell (Windows), xclip (Linux)
