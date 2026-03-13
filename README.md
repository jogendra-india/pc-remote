# MacBook Remote Desktop

Control your MacBook from any browser on the same Wi-Fi network — phone, tablet, or another computer.

## Features

- **Live screen streaming** via WebSocket (adjustable FPS, quality, resolution)
- **Mouse control** — move cursor, left/right click, double-click, drag, scroll
- **Keyboard input** — full key support including modifier combos (Cmd+C, etc.)
- **Mobile-friendly** — touch support with Direct and Trackpad interaction modes
- **Settings panel** — tune FPS, JPEG quality, and resolution scale on the fly

## Prerequisites

- Python 3.10+
- macOS (tested on Ventura / Sonoma / Sequoia)
- **Accessibility permissions**: the terminal app running the server must have Accessibility access
  - Go to **System Settings → Privacy & Security → Accessibility**
  - Add and enable your terminal app (Terminal.app, iTerm2, VS Code, etc.)

## Quick Start

```bash
cd macbook-remote

# Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the server
python server.py
```

The server prints two URLs:

```
======================================================
  MacBook Remote Desktop
======================================================
  Local:   http://localhost:5050
  Network: http://192.168.x.x:5050
======================================================
```

Open the **Network** URL on your phone or another device on the same Wi-Fi.

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
| Keyboard | Just type (canvas must be focused) |

### Mobile / Tablet

Two touch modes (toggle via the **Direct / Trackpad** button):

**Direct mode** — touch position maps directly to cursor position on screen.

**Trackpad mode** — drag to move the cursor relatively (like a laptop trackpad). Better precision on small screens.

| Action | How |
|---|---|
| Move cursor | Drag on screen |
| Left click | Tap |
| Right click | Use the "Right Click" button in the bottom bar |
| Scroll | Two-finger swipe, or use scroll buttons |
| Keyboard | Tap the **KB** button to open the on-screen keyboard |

## Settings

Click the gear icon to adjust:

- **FPS** (1–30) — frames per second streamed to the browser
- **JPEG Quality** (10–100) — image quality vs bandwidth trade-off
- **Resolution Scale** (0.25–1.0) — scales the captured image down

Lower values = less bandwidth, faster on slow networks.
Higher values = sharper image, more data.

## Troubleshooting

| Issue | Fix |
|---|---|
| Cursor won't move | Grant Accessibility permissions to your terminal app |
| Black screen | Grant Screen Recording permissions (System Settings → Privacy & Security → Screen Recording) |
| High latency | Lower FPS, quality, or scale in settings |
| Can't connect from phone | Ensure both devices are on the same Wi-Fi network and no firewall is blocking port 5050 |
