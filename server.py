"""
Remote Desktop Server — cross-platform (macOS, Windows, Linux)

Streams the screen over WebSocket and accepts mouse/keyboard input
from any browser on the same network.

macOS:   Quartz CoreGraphics for mouse, caffeinate for sleep prevention
Windows: pyautogui for mouse, SetThreadExecutionState for sleep prevention
Linux:   pyautogui for mouse, systemd-inhibit for sleep prevention
"""

import atexit
import base64
import ctypes
import ctypes.util
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from io import BytesIO
from pathlib import Path

import mss
import pyautogui
from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO
from PIL import Image
from werkzeug.utils import secure_filename

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

if IS_MACOS:
    import Quartz

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

LOGGER = logging.getLogger(__name__)

HOME_DIR = str(Path.home())
UPLOAD_DIR = os.path.join(HOME_DIR, "Desktop", "RemoteUploads")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    max_http_buffer_size=10 * 1024 * 1024,
)

connected_clients: set[str] = set()
clients_lock = threading.Lock()

logical_width, logical_height = pyautogui.size()

settings = {
    "fps": 15,
    "quality": 50,
    "scale": 0.5,
}

KEY_MAP = {
    "Enter": "return",
    "Return": "return",
    "Backspace": "backspace",
    "Tab": "tab",
    "Escape": "escape",
    "ArrowUp": "up",
    "ArrowDown": "down",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "Delete": "delete",
    "Home": "home",
    "End": "end",
    "PageUp": "pageup",
    "PageDown": "pagedown",
    " ": "space",
    "CapsLock": "capslock",
    "F1": "f1", "F2": "f2", "F3": "f3", "F4": "f4",
    "F5": "f5", "F6": "f6", "F7": "f7", "F8": "f8",
    "F9": "f9", "F10": "f10", "F11": "f11", "F12": "f12",
}

MODIFIER_KEYS = {"Shift", "Control", "Alt", "Meta"}


# ---------------------------------------------------------------------------
# Mouse controller — Quartz on macOS, pyautogui on Windows/Linux
# ---------------------------------------------------------------------------

if IS_MACOS:

    class MouseController:
        """Direct CoreGraphics mouse control via Quartz."""

        @staticmethod
        def move(x, y):
            point = (float(x), float(y))
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

        @staticmethod
        def click(x, y, button="left"):
            point = (float(x), float(y))
            MouseController.move(x, y)
            time.sleep(0.01)

            if button == "right":
                btn = Quartz.kCGMouseButtonRight
                down_type = Quartz.kCGEventRightMouseDown
                up_type = Quartz.kCGEventRightMouseUp
            else:
                btn = Quartz.kCGMouseButtonLeft
                down_type = Quartz.kCGEventLeftMouseDown
                up_type = Quartz.kCGEventLeftMouseUp

            down = Quartz.CGEventCreateMouseEvent(None, down_type, point, btn)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            time.sleep(0.01)
            up = Quartz.CGEventCreateMouseEvent(None, up_type, point, btn)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

        @staticmethod
        def double_click(x, y):
            point = (float(x), float(y))
            btn = Quartz.kCGMouseButtonLeft
            MouseController.move(x, y)
            time.sleep(0.01)
            for click_count in (1, 2):
                down = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventLeftMouseDown, point, btn,
                )
                Quartz.CGEventSetIntegerValueField(
                    down, Quartz.kCGMouseEventClickState, click_count,
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
                up = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventLeftMouseUp, point, btn,
                )
                Quartz.CGEventSetIntegerValueField(
                    up, Quartz.kCGMouseEventClickState, click_count,
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
                time.sleep(0.01)

        @staticmethod
        def scroll(dy):
            event = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitLine, 1, int(dy),
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

        @staticmethod
        def drag(x, y):
            point = (float(x), float(y))
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseDragged, point, Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

        @staticmethod
        def mouse_down(x, y, button="left"):
            point = (float(x), float(y))
            MouseController.move(x, y)
            if button == "right":
                btn = Quartz.kCGMouseButtonRight
                event_type = Quartz.kCGEventRightMouseDown
            else:
                btn = Quartz.kCGMouseButtonLeft
                event_type = Quartz.kCGEventLeftMouseDown
            event = Quartz.CGEventCreateMouseEvent(None, event_type, point, btn)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

        @staticmethod
        def mouse_up(x, y, button="left"):
            point = (float(x), float(y))
            if button == "right":
                btn = Quartz.kCGMouseButtonRight
                event_type = Quartz.kCGEventRightMouseUp
            else:
                btn = Quartz.kCGMouseButtonLeft
                event_type = Quartz.kCGEventLeftMouseUp
            event = Quartz.CGEventCreateMouseEvent(None, event_type, point, btn)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

else:

    class MouseController:
        """Cross-platform mouse control via pyautogui (Windows/Linux)."""

        @staticmethod
        def move(x, y):
            pyautogui.moveTo(int(x), int(y), _pause=False)

        @staticmethod
        def click(x, y, button="left"):
            pyautogui.click(int(x), int(y), button=button, _pause=False)

        @staticmethod
        def double_click(x, y):
            pyautogui.doubleClick(int(x), int(y), _pause=False)

        @staticmethod
        def scroll(dy):
            pyautogui.scroll(int(dy), _pause=False)

        @staticmethod
        def drag(x, y):
            pyautogui.moveTo(int(x), int(y), _pause=False)

        @staticmethod
        def mouse_down(x, y, button="left"):
            pyautogui.moveTo(int(x), int(y), _pause=False)
            pyautogui.mouseDown(button=button, _pause=False)

        @staticmethod
        def mouse_up(x, y, button="left"):
            pyautogui.moveTo(int(x), int(y), _pause=False)
            pyautogui.mouseUp(button=button, _pause=False)


mouse = MouseController()


# ---------------------------------------------------------------------------
# Permission check (macOS-specific; other platforms return True)
# ---------------------------------------------------------------------------

def check_accessibility():
    if not IS_MACOS:
        return True
    try:
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return lib.AXIsProcessTrusted()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Clipboard helpers — platform-aware
# ---------------------------------------------------------------------------

def get_clipboard():
    try:
        if IS_MACOS:
            result = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=5,
            )
            return result.stdout
        elif IS_WINDOWS:
            result = subprocess.run(
                ["powershell", "-command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.rstrip("\r\n")
        else:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout
    except Exception:
        LOGGER.exception("get_clipboard failed")
        return ""


def set_clipboard(text):
    try:
        if IS_MACOS:
            subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
        elif IS_WINDOWS:
            subprocess.run(["clip"], input=text, text=True, timeout=5)
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text, text=True, timeout=5,
            )
    except Exception:
        LOGGER.exception("set_clipboard failed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def human_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def capture_and_stream():
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        while True:
            with clients_lock:
                has_clients = bool(connected_clients)
            if not has_clients:
                time.sleep(0.5)
                continue
            try:
                img = sct.grab(monitor)
                pil_img = Image.frombytes("RGB", img.size, img.rgb)
                scale = settings["scale"]
                new_size = (int(pil_img.width * scale), int(pil_img.height * scale))
                pil_img = pil_img.resize(new_size, Image.LANCZOS)
                buf = BytesIO()
                pil_img.save(buf, format="JPEG", quality=settings["quality"])
                frame_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                socketio.emit("frame", {
                    "img": frame_b64,
                    "w": logical_width,
                    "h": logical_height,
                })
            except Exception:
                LOGGER.exception("Screen capture error")
            time.sleep(1.0 / settings["fps"])


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400

    filename = secure_filename(f.filename)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest = os.path.join(UPLOAD_DIR, filename)

    counter = 1
    base, ext = os.path.splitext(filename)
    while os.path.exists(dest):
        dest = os.path.join(UPLOAD_DIR, f"{base}_{counter}{ext}")
        counter += 1

    f.save(dest)
    LOGGER.info("File uploaded: %s", dest)
    return jsonify({"ok": True, "path": dest, "name": os.path.basename(dest)})


@app.route("/download")
def download_file():
    filepath = request.args.get("path", "")
    if not filepath:
        return jsonify({"error": "No path given"}), 400

    abs_path = os.path.abspath(filepath)
    if not abs_path.startswith(HOME_DIR):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.isfile(abs_path):
        return jsonify({"error": "Not found"}), 404

    return send_file(abs_path, as_attachment=True)


@app.route("/api/files")
def list_files():
    path = request.args.get("path", HOME_DIR)
    abs_path = os.path.abspath(path)

    if not abs_path.startswith(HOME_DIR):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.isdir(abs_path):
        return jsonify({"error": "Not a directory"}), 400

    items = []
    try:
        for entry in sorted(
            os.scandir(abs_path), key=lambda e: (not e.is_dir(), e.name.lower())
        ):
            if entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
                items.append({
                    "name": entry.name,
                    "path": entry.path,
                    "is_dir": entry.is_dir(),
                    "size": human_size(stat.st_size) if entry.is_file() else "",
                })
            except PermissionError:
                continue
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403

    parent = os.path.dirname(abs_path) if abs_path != HOME_DIR else None
    return jsonify({"path": abs_path, "parent": parent, "items": items})


# ---------------------------------------------------------------------------
# Socket.IO events — connection
# ---------------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    with clients_lock:
        connected_clients.add(request.sid)
    socketio.emit(
        "screen_info",
        {"width": logical_width, "height": logical_height},
        to=request.sid,
    )
    LOGGER.info("Client connected: %s (total: %d)", request.sid, len(connected_clients))


@socketio.on("disconnect")
def on_disconnect():
    with clients_lock:
        connected_clients.discard(request.sid)
    LOGGER.info("Client disconnected: %s (total: %d)", request.sid, len(connected_clients))


# ---------------------------------------------------------------------------
# Socket.IO events — mouse
# ---------------------------------------------------------------------------

@socketio.on("move")
def on_move(data):
    try:
        mouse.move(data["x"] * logical_width, data["y"] * logical_height)
    except Exception:
        LOGGER.exception("mouse.move failed")


@socketio.on("click")
def on_click(data):
    try:
        mouse.click(
            data["x"] * logical_width,
            data["y"] * logical_height,
            button=data.get("btn", "left"),
        )
    except Exception:
        LOGGER.exception("mouse.click failed")


@socketio.on("dblclick")
def on_dblclick(data):
    try:
        mouse.double_click(data["x"] * logical_width, data["y"] * logical_height)
    except Exception:
        LOGGER.exception("mouse.double_click failed")


@socketio.on("mousedown")
def on_mousedown(data):
    try:
        mouse.mouse_down(
            data["x"] * logical_width,
            data["y"] * logical_height,
            button=data.get("btn", "left"),
        )
    except Exception:
        LOGGER.exception("mouse.mouse_down failed")


@socketio.on("mouseup")
def on_mouseup(data):
    try:
        mouse.mouse_up(
            data["x"] * logical_width,
            data["y"] * logical_height,
            button=data.get("btn", "left"),
        )
    except Exception:
        LOGGER.exception("mouse.mouse_up failed")


@socketio.on("drag")
def on_drag(data):
    try:
        mouse.drag(data["x"] * logical_width, data["y"] * logical_height)
    except Exception:
        LOGGER.exception("mouse.drag failed")


@socketio.on("scroll")
def on_scroll(data):
    try:
        mouse.scroll(int(data.get("dy", 0)))
    except Exception:
        LOGGER.exception("mouse.scroll failed")


# ---------------------------------------------------------------------------
# Socket.IO events — keyboard
# ---------------------------------------------------------------------------

# On macOS Cmd maps to pyautogui "command"; on Windows/Linux Cmd shortcuts
# typically correspond to Ctrl (e.g. Cmd+C → Ctrl+C).  The "win" key is
# mapped separately so the user can still press it explicitly.
_CMD_PYAUTOGUI = "command" if IS_MACOS else "ctrl"

MOD_MAP = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt",
    "cmd": _CMD_PYAUTOGUI, "meta": _CMD_PYAUTOGUI, "command": _CMD_PYAUTOGUI,
    "shift": "shift",
    "win": "win" if IS_WINDOWS else "command",
}

def _send_key_combo(modifiers, mapped_key):
    """Send key with modifiers using keyDown/press/keyUp for reliability."""
    if not modifiers:
        pyautogui.press(mapped_key, _pause=False)
        return

    # Alt+Enter has shown issues on some systems when sent directly with hotkey(),
    # so use explicit press sequence.
    for m in modifiers:
        pyautogui.keyDown(m, _pause=False)
    try:
        if mapped_key in ("enter", "return"):
            pyautogui.keyDown(mapped_key, _pause=False)
            pyautogui.keyUp(mapped_key, _pause=False)
        else:
            pyautogui.press(mapped_key, _pause=False)
    finally:
        for m in reversed(modifiers):
            pyautogui.keyUp(m, _pause=False)

def _map_modifier_flag(data):
    """Build modifier list from boolean flags (keydown event)."""
    modifiers = []
    if data.get("meta"):
        modifiers.append(_CMD_PYAUTOGUI)
    if data.get("ctrl"):
        modifiers.append("ctrl")
    if data.get("alt"):
        modifiers.append("alt")
    if data.get("shift"):
        modifiers.append("shift")
    return modifiers


@socketio.on("keydown")
def on_keydown(data):
    key = data.get("key", "")
    if key in MODIFIER_KEYS:
        return

    modifiers = _map_modifier_flag(data)

    mapped = KEY_MAP.get(key)
    if mapped is None:
        mapped = key.lower() if len(key) == 1 else None
    if not mapped:
        return

    try:
        _send_key_combo(modifiers, mapped)
    except Exception:
        LOGGER.exception("keydown error key=%s mods=%s", key, modifiers)


@socketio.on("hotkey")
def on_hotkey(data):
    """Explicit hotkey: { modifiers: ["ctrl","shift"], key: "c" }"""
    modifiers = data.get("modifiers", [])
    key = data.get("key", "")

    mapped_mods = [MOD_MAP[m] for m in modifiers if m in MOD_MAP]

    mapped_key = KEY_MAP.get(key)
    if mapped_key is None:
        mapped_key = key.lower() if len(key) == 1 else None
    if not mapped_key:
        return

    try:
        _send_key_combo(mapped_mods, mapped_key)
    except Exception:
        LOGGER.exception("hotkey error mods=%s key=%s", modifiers, key)


@socketio.on("type_text")
def on_type_text(data):
    """Type a string of text, one character at a time."""
    text = data.get("text", "")
    if not text:
        return
    for ch in text:
        if ch == "\n":
            pyautogui.press("enter", _pause=False)
            time.sleep(0.02)
            continue
        mapped = KEY_MAP.get(ch)
        if mapped is None and len(ch) == 1:
            mapped = ch.lower()
        if mapped:
            try:
                if ch.isupper() or ch in '~!@#$%^&*()_+{}|:"<>?':
                    pyautogui.hotkey("shift", mapped, _pause=False)
                else:
                    pyautogui.press(mapped, _pause=False)
            except Exception:
                pass
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Socket.IO events — clipboard
# ---------------------------------------------------------------------------

@socketio.on("clipboard_get")
def on_clipboard_get(_data=None):
    text = get_clipboard()
    socketio.emit("clipboard_content", {"text": text}, to=request.sid)


@socketio.on("clipboard_set")
def on_clipboard_set(data):
    text = data.get("text", "")
    set_clipboard(text)
    socketio.emit("clipboard_content", {"text": text}, to=request.sid)


# ---------------------------------------------------------------------------
# Socket.IO events — settings
# ---------------------------------------------------------------------------

@socketio.on("update_settings")
def on_update_settings(data):
    if "fps" in data:
        settings["fps"] = max(1, min(30, int(data["fps"])))
    if "quality" in data:
        settings["quality"] = max(10, min(100, int(data["quality"])))
    if "scale" in data:
        settings["scale"] = max(0.25, min(1.0, float(data["scale"])))
    LOGGER.info("Settings updated: %s", settings)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class _WebSocketUpgradeFilter(logging.Filter):
    """Suppress the harmless werkzeug assertion during WebSocket upgrades."""
    def filter(self, record):
        return "write() before start_response" not in record.getMessage()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    logging.getLogger("werkzeug").addFilter(_WebSocketUpgradeFilter())

    accessible = check_accessibility()
    if accessible is False:
        print("\n" + "!" * 54)
        print("  WARNING: Accessibility permissions NOT granted!")
        print("  Mouse control will NOT work until you allow it.")
        print("")
        print("  Fix: System Settings > Privacy & Security")
        print("       > Accessibility > enable your terminal app")
        print("!" * 54 + "\n")
    elif accessible is True:
        LOGGER.info("Accessibility permissions: OK")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    LOGGER.info("Platform: %s", sys.platform)
    LOGGER.info("Logical screen: %dx%d", logical_width, logical_height)
    LOGGER.info("Upload directory: %s", UPLOAD_DIR)

    # --- Prevent display/idle/system sleep while the server is running ---
    _sleep_cleanup = None

    if IS_MACOS:
        caffeinate_proc = None
        try:
            caffeinate_proc = subprocess.Popen(
                ["caffeinate", "-dis"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            LOGGER.info("Caffeinate active (pid %d) — sleep is disabled", caffeinate_proc.pid)
        except FileNotFoundError:
            LOGGER.warning("caffeinate not found — sleep prevention unavailable")

        def _sleep_cleanup_mac():
            if caffeinate_proc and caffeinate_proc.poll() is None:
                caffeinate_proc.terminate()
                LOGGER.info("Caffeinate stopped — sleep re-enabled")

        _sleep_cleanup = _sleep_cleanup_mac

    elif IS_WINDOWS:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
            LOGGER.info("SetThreadExecutionState active — sleep is disabled")
        except Exception:
            LOGGER.warning("SetThreadExecutionState failed — sleep prevention unavailable")

        def _sleep_cleanup_win():
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                LOGGER.info("SetThreadExecutionState cleared — sleep re-enabled")
            except Exception:
                pass

        _sleep_cleanup = _sleep_cleanup_win

    else:
        inhibit_proc = None
        try:
            inhibit_proc = subprocess.Popen(
                [
                    "systemd-inhibit", "--what=idle:sleep",
                    "--who=RemoteDesktop", "--reason=Server active",
                    "sleep", "infinity",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            LOGGER.info("systemd-inhibit active (pid %d) — sleep is disabled", inhibit_proc.pid)
        except FileNotFoundError:
            LOGGER.warning("systemd-inhibit not found — sleep prevention unavailable")

        def _sleep_cleanup_linux():
            if inhibit_proc and inhibit_proc.poll() is None:
                inhibit_proc.terminate()
                LOGGER.info("systemd-inhibit stopped — sleep re-enabled")

        _sleep_cleanup = _sleep_cleanup_linux

    if _sleep_cleanup:
        atexit.register(_sleep_cleanup)
        signal.signal(signal.SIGTERM, lambda *_: (_sleep_cleanup(), os._exit(0)))

    stream_thread = threading.Thread(target=capture_and_stream, daemon=True)
    stream_thread.start()

    local_ip = get_local_ip()
    port = 5050

    platform_name = "macOS" if IS_MACOS else "Windows" if IS_WINDOWS else "Linux"
    print(f"\n{'=' * 54}")
    print(f"  Remote Desktop Server ({platform_name})")
    print(f"{'=' * 54}")
    print(f"  Local:   http://localhost:{port}")
    print(f"  Network: http://{local_ip}:{port}")
    print(f"{'=' * 54}")
    print("  Open the Network URL on your phone / tablet")
    print("  (device must be on the same Wi-Fi network)")
    print(f"{'=' * 54}\n")

    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
