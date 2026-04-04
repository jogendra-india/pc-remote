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
import json
import ctypes.util
import logging
import os
import signal
import socket
import subprocess
import sys
import queue
import threading
import time
import textwrap
from io import BytesIO
from pathlib import Path

import mss
import pyautogui
from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

try:
    import sounddevice as sd
    HAS_AUDIO = True
except ImportError:
    HAS_AUDIO = False

try:
    import asyncio
    import numpy as np
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import VideoStreamTrack
    from av import VideoFrame
    HAS_WEBRTC = True
except ImportError:
    HAS_WEBRTC = False

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

if IS_MACOS:
    import Quartz
elif IS_WINDOWS:
    from ctypes import wintypes
    # Must be called before ANY Win32 coordinate API (pyautogui.size, mss,
    # GetCursorInfo, etc.) so that logical/physical pixel values are consistent
    # across all calls.  Without this, display scaling (125 %, 150 %, …) causes
    # mss screenshots vs GetCursorInfo coordinates to disagree, and the
    # composited cursor appears at wrong position or is off-screen entirely.
    try:
        _shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        _shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        # shcore unavailable (Windows 7) → fall back to user32
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

LOGGER = logging.getLogger(__name__)


if IS_WINDOWS:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    UOI_NAME = 2
    DESKTOP_READOBJECTS = 0x0001
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.argtypes = []
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = ctypes.c_void_p
    _user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    _user32.SetClipboardData.restype = ctypes.c_void_p
    _user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _user32.OpenInputDesktop.restype = wintypes.HANDLE
    _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _user32.CloseDesktop.restype = wintypes.BOOL
    _user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.GetUserObjectInformationW.restype = wintypes.BOOL
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalFree.restype = ctypes.c_void_p


    def _open_clipboard_with_retry(retries=5, delay=0.05):
        for _ in range(retries):
            if _user32.OpenClipboard(None):
                return True
            time.sleep(delay)
        return False


    def _get_windows_clipboard_text():
        if not _open_clipboard_with_retry():
            return ""
        handle = None
        locked = None
        try:
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            locked = _kernel32.GlobalLock(handle)
            if not locked:
                return ""
            return ctypes.wstring_at(locked)
        finally:
            if locked:
                _kernel32.GlobalUnlock(handle)
            _user32.CloseClipboard()


    def _set_windows_clipboard_text(text):
        data = (text or "") + "\0"
        data_size = len(data) * ctypes.sizeof(ctypes.c_wchar)
        h_global = _kernel32.GlobalAlloc(GMEM_MOVEABLE, data_size)
        if not h_global:
            raise ctypes.WinError(ctypes.get_last_error())

        locked = None
        should_free = True
        try:
            locked = _kernel32.GlobalLock(h_global)
            if not locked:
                raise ctypes.WinError(ctypes.get_last_error())

            ctypes.memmove(locked, ctypes.create_unicode_buffer(data), data_size)
            _kernel32.GlobalUnlock(h_global)
            locked = None

            if not _open_clipboard_with_retry():
                raise RuntimeError("OpenClipboard failed")

            try:
                if not _user32.EmptyClipboard():
                    raise ctypes.WinError(ctypes.get_last_error())
                if not _user32.SetClipboardData(CF_UNICODETEXT, h_global):
                    raise ctypes.WinError(ctypes.get_last_error())
                should_free = False
            finally:
                _user32.CloseClipboard()
        finally:
            if locked:
                _kernel32.GlobalUnlock(h_global)
            if should_free:
                _kernel32.GlobalFree(h_global)


def resource_path(relative_path):
    """Resolve bundled resources for normal runs and PyInstaller one-file builds."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


HOME_DIR = str(Path.home())
UPLOAD_DIR = os.path.join(HOME_DIR, "Desktop", "RemoteUploads")

app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
    static_url_path="/static",
)
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
    "fps": 30,
    "quality": 70,
    "scale": 0.75,
    "format": "webp",
    "monitor": 1,
}


_active_monitor = {"left": 0, "top": 0, "width": logical_width, "height": logical_height}


def _mouse_xy(data):
    """Convert browser 0-1 ratios to absolute screen coordinates for the active monitor."""
    m = _active_monitor
    return m["left"] + data["x"] * m["width"], m["top"] + data["y"] * m["height"]


def _sync_active_monitor():
    """Refresh _active_monitor from the current settings['monitor']."""
    global _active_monitor
    try:
        with mss.mss() as sct:
            idx = settings.get("monitor", 1)
            num = len(sct.monitors)
            if num == 0:
                return
            if idx < 0 or idx >= num:
                idx = min(1, num - 1)
                settings["monitor"] = idx
            m = sct.monitors[idx]
            _active_monitor = {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
    except Exception:
        LOGGER.exception("Failed to sync active monitor")


_sync_active_monitor()


def _get_monitor_list():
    """Return a list of monitors with index, resolution, and position."""
    with mss.mss() as sct:
        monitors = []
        for i, m in enumerate(sct.monitors):
            label = "All Screens" if i == 0 else f"Screen {i}"
            monitors.append({
                "index": i,
                "label": label,
                "width": m["width"],
                "height": m["height"],
                "left": m["left"],
                "top": m["top"],
            })
        return monitors

# Audio streaming state
audio_active = False
audio_thread = None
audio_lock = threading.Lock()
AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2
AUDIO_CHUNK = 4096
audio_device_index = None
audio_loopback_extra = None  # WASAPI loopback settings (Windows only)


def _find_loopback_device():
    """Auto-detect the best device for capturing system audio output."""
    if not HAS_AUDIO:
        return None, None

    try:
        devices = sd.query_devices()
    except Exception:
        return None, None

    # Log all host APIs and devices for diagnostics
    try:
        apis = sd.query_hostapis()
        for i, api in enumerate(apis):
            LOGGER.info("Host API %d: %s (devices: %d)", i, api["name"], api["device_count"])
        for i, d in enumerate(devices):
            LOGGER.info(
                "  Device %d: %s [hostapi=%d, in=%d, out=%d, rate=%.0f]",
                i, d["name"], d["hostapi"], d["max_input_channels"], d["max_output_channels"], d["default_samplerate"],
            )
    except Exception:
        pass

    if IS_WINDOWS:
        try:
            wasapi_settings = sd.WasapiSettings(loopback=True)
            wasapi_api_idx = None
            for i, api in enumerate(sd.query_hostapis()):
                if "WASAPI" in api["name"]:
                    wasapi_api_idx = i
                    break

            if wasapi_api_idx is not None:
                default_out = None
                for i, d in enumerate(devices):
                    if d["hostapi"] == wasapi_api_idx and d["max_output_channels"] > 0:
                        default_out = i
                        break
                if default_out is not None:
                    LOGGER.info(
                        "WASAPI loopback selected: device %d (%s, hostapi=%d)",
                        default_out, devices[default_out]["name"], wasapi_api_idx,
                    )
                    return default_out, wasapi_settings
                else:
                    LOGGER.warning("WASAPI host API found but no output devices in it")
            else:
                LOGGER.warning("No WASAPI host API found on this system")
        except (AttributeError, TypeError):
            LOGGER.warning("sd.WasapiSettings not available in this sounddevice build")
        except Exception:
            LOGGER.exception("WASAPI loopback detection failed")

    loopback_keywords = ["blackhole", "soundflower", "loopback", "monitor", "stereo mix", "what u hear"]
    for i, d in enumerate(devices):
        name_lower = d["name"].lower()
        if d["max_input_channels"] > 0 and any(kw in name_lower for kw in loopback_keywords):
            LOGGER.info("Loopback device found: %d (%s, hostapi=%d)", i, d["name"], d["hostapi"])
            return i, None

    LOGGER.warning("No loopback audio device found — will capture from default mic")
    return None, None


_default_loopback_device, _default_loopback_extra = _find_loopback_device()

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
    "space": "space",
    "Space": "space",
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
        def triple_click(x, y):
            point = (float(x), float(y))
            btn = Quartz.kCGMouseButtonLeft
            MouseController.move(x, y)
            time.sleep(0.01)
            for click_count in (1, 2, 3):
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
            if IS_WINDOWS:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
            else:
                pyautogui.moveTo(int(x), int(y), _pause=False)

        @staticmethod
        def click(x, y, button="left"):
            if IS_WINDOWS:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
                if button == "right":
                    ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0) # RIGHTDOWN
                    ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0) # RIGHTUP
                else:
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0) # LEFTDOWN
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0) # LEFTUP
            else:
                pyautogui.click(int(x), int(y), button=button, _pause=False)

        @staticmethod
        def double_click(x, y):
            if IS_WINDOWS:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
                ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
            else:
                pyautogui.doubleClick(int(x), int(y), _pause=False)

        @staticmethod
        def triple_click(x, y):
            if IS_WINDOWS:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
                for _ in range(3):
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
            else:
                pyautogui.click(int(x), int(y), clicks=3, _pause=False)

        @staticmethod
        def scroll(dy):
            pyautogui.scroll(int(dy), _pause=False)

        @staticmethod
        def drag(x, y):
            if IS_WINDOWS:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
            else:
                pyautogui.moveTo(int(x), int(y), _pause=False)

        @staticmethod
        def mouse_down(x, y, button="left"):
            if IS_WINDOWS:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
                if button == "right":
                    ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0)
                else:
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
            else:
                pyautogui.moveTo(int(x), int(y), _pause=False)
                pyautogui.mouseDown(button=button, _pause=False)

        @staticmethod
        def mouse_up(x, y, button="left"):
            if IS_WINDOWS:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
                if button == "right":
                    ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)
                else:
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
            else:
                pyautogui.moveTo(int(x), int(y), _pause=False)
                pyautogui.mouseUp(button=button, _pause=False)


mouse = MouseController()


# ---------------------------------------------------------------------------
# Cursor capture — composites real system cursor into screenshots
# ---------------------------------------------------------------------------

_cursor_cache = {}

def _make_arrow_cursor(scale=2):
    """Draw a macOS-style arrow cursor at the given DPI scale."""
    w, h = int(17 * scale), int(25 * scale)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = scale
    # Outer black border
    d.polygon([
        (0, 0), (0, int(20 * s)), (int(4.5 * s), int(15.5 * s)),
        (int(7.5 * s), int(23 * s)), (int(10 * s), int(21.5 * s)),
        (int(7 * s), int(14 * s)), (int(12 * s), int(14 * s)),
    ], fill=(0, 0, 0, 220))
    # Inner white fill
    d.polygon([
        (int(1.2 * s), int(2 * s)), (int(1.2 * s), int(18 * s)),
        (int(5 * s), int(14.5 * s)), (int(8 * s), int(21.5 * s)),
        (int(9 * s), int(20.5 * s)), (int(6.5 * s), int(13 * s)),
        (int(10.5 * s), int(13 * s)),
    ], fill=(255, 255, 255, 245))
    return img


if IS_MACOS:

    def get_cursor_info(screenshot_width, monitor=None):
        """Use Quartz (thread-safe) for position, draw a static arrow cursor."""
        try:
            event = Quartz.CGEventCreate(None)
            loc = Quartz.CGEventGetLocation(event)
            if monitor:
                dpi = screenshot_width / monitor["width"]
                px = int((loc.x - monitor["left"]) * dpi)
                py = int((loc.y - monitor["top"]) * dpi)
            else:
                dpi = screenshot_width / logical_width
                px = int(loc.x * dpi)
                py = int(loc.y * dpi)
            cache_key = f"arrow_{dpi:.2f}"
            if cache_key not in _cursor_cache:
                _cursor_cache[cache_key] = _make_arrow_cursor(scale=dpi)
            cursor_img = _cursor_cache[cache_key]
            return cursor_img, px, py
        except Exception:
            return None

elif IS_WINDOWS:

    class _POINT_CI(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _CURSORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint),
            ("hCursor", ctypes.c_void_p), ("ptScreenPos", _POINT_CI),
        ]

    class _ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", ctypes.c_int),
            ("xHotspot", ctypes.c_uint), ("yHotspot", ctypes.c_uint),
            ("hbmMask", ctypes.c_void_p), ("hbmColor", ctypes.c_void_p),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint),
            ("biWidth", ctypes.c_int), ("biHeight", ctypes.c_int),
            ("biPlanes", ctypes.c_ushort), ("biBitCount", ctypes.c_ushort),
            ("biCompression", ctypes.c_uint), ("biSizeImage", ctypes.c_uint),
            ("biXPelsPerMeter", ctypes.c_int), ("biYPelsPerMeter", ctypes.c_int),
            ("biClrUsed", ctypes.c_uint), ("biClrImportant", ctypes.c_uint),
        ]

    _cur_u32 = ctypes.windll.user32
    _cur_g32 = ctypes.windll.gdi32

    # On 64-bit Windows, GDI/user32 handles are 64-bit pointers.  Without
    # explicit argtypes, ctypes defaults to c_int (32-bit) which overflows
    # when the handle value exceeds 2^31.
    _cur_g32.DeleteObject.argtypes = [ctypes.c_void_p]
    _cur_g32.DeleteObject.restype = ctypes.c_int
    _cur_g32.DeleteDC.argtypes = [ctypes.c_void_p]
    _cur_g32.DeleteDC.restype = ctypes.c_int
    _cur_g32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _cur_g32.CreateCompatibleDC.restype = ctypes.c_void_p
    _cur_g32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _cur_g32.SelectObject.restype = ctypes.c_void_p
    _cur_g32.CreateDIBSection.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint,
    ]
    _cur_g32.CreateDIBSection.restype = ctypes.c_void_p
    _cur_u32.GetDC.argtypes = [ctypes.c_void_p]
    _cur_u32.GetDC.restype = ctypes.c_void_p
    _cur_u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _cur_u32.ReleaseDC.restype = ctypes.c_int
    _cur_u32.DrawIconEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
    ]
    _cur_u32.DrawIconEx.restype = ctypes.c_int
    _cur_u32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _cur_u32.GetIconInfo.restype = ctypes.c_int
    _cur_u32.GetCursorInfo.argtypes = [ctypes.c_void_p]
    _cur_u32.GetCursorInfo.restype = ctypes.c_int
    _cur_u32.GetCursorPos.argtypes = [ctypes.c_void_p]
    _cur_u32.GetCursorPos.restype = ctypes.c_int
    _cur_u32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _cur_u32.GetSystemMetrics.restype = ctypes.c_int

    def _render_win_cursor(h_cursor, cw, ch):
        hdc_s = _cur_u32.GetDC(0)
        hdc_m = _cur_g32.CreateCompatibleDC(hdc_s)
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = cw, -ch
        bmi.biPlanes, bmi.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        hbm = _cur_g32.CreateDIBSection(hdc_m, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not hbm:
            _cur_g32.DeleteDC(hdc_m)
            _cur_u32.ReleaseDC(0, hdc_s)
            return None
        old = _cur_g32.SelectObject(hdc_m, hbm)
        buf_sz = cw * ch * 4
        ctypes.memset(bits, 0, buf_sz)
        _cur_u32.DrawIconEx(hdc_m, 0, 0, h_cursor, cw, ch, 0, None, 3)
        raw = (ctypes.c_ubyte * buf_sz)()
        ctypes.memmove(raw, bits, buf_sz)
        _cur_g32.SelectObject(hdc_m, old)
        _cur_g32.DeleteObject(hbm)
        _cur_g32.DeleteDC(hdc_m)
        _cur_u32.ReleaseDC(0, hdc_s)
        return Image.frombuffer("RGBA", (cw, ch), bytes(raw), "raw", "BGRA", 0, 1)

    _win_cursor_bitmap_warned = False

    def _get_win_cursor_pos():
        """Return (x, y) in screen pixels via GetCursorPos — almost never fails."""
        pt = _POINT_CI()
        if _cur_u32.GetCursorPos(ctypes.byref(pt)):
            return pt.x, pt.y
        return None

    def _try_native_cursor_bitmap():
        """Try GetCursorInfo → GetIconInfo → DrawIconEx.  Returns (img, hx, hy) or None."""
        ci = _CURSORINFO()
        ci.cbSize = ctypes.sizeof(_CURSORINFO)
        if not _cur_u32.GetCursorInfo(ctypes.byref(ci)):
            return None
        ck = ci.hCursor
        if not ck:
            return None
        if ck in _cursor_cache:
            return _cursor_cache[ck]
        ii = _ICONINFO()
        if not _cur_u32.GetIconInfo(ck, ctypes.byref(ii)):
            return None
        hx, hy = ii.xHotspot, ii.yHotspot
        cw = _cur_u32.GetSystemMetrics(13) or 32
        ch = _cur_u32.GetSystemMetrics(14) or 32
        img = _render_win_cursor(ck, cw, ch)
        if ii.hbmMask:
            _cur_g32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            _cur_g32.DeleteObject(ii.hbmColor)
        if img is None:
            return None
        _cursor_cache[ck] = (img, hx, hy)
        return img, hx, hy

    def get_cursor_info(screenshot_width, monitor=None):
        global _win_cursor_bitmap_warned
        try:
            pos = _get_win_cursor_pos()
            if pos is None:
                return None
            scr_x, scr_y = pos

            # Best-effort native cursor bitmap; fall back to static arrow
            bitmap_result = None
            try:
                bitmap_result = _try_native_cursor_bitmap()
            except Exception:
                if not _win_cursor_bitmap_warned:
                    LOGGER.warning("Native cursor bitmap extraction failed, using fallback arrow", exc_info=True)
                    _win_cursor_bitmap_warned = True

            if bitmap_result:
                cursor_img, hx, hy = bitmap_result
            else:
                dpi = screenshot_width / (monitor["width"] if monitor else logical_width)
                cache_key = f"fallback_arrow_{dpi:.2f}"
                if cache_key not in _cursor_cache:
                    _cursor_cache[cache_key] = (_make_arrow_cursor(scale=max(1, dpi)), 0, 0)
                cursor_img, hx, hy = _cursor_cache[cache_key]

            if monitor:
                dpi = screenshot_width / monitor["width"]
                px = int((scr_x - monitor["left"]) * dpi - hx)
                py = int((scr_y - monitor["top"]) * dpi - hy)
            else:
                dpi = screenshot_width / logical_width
                px = int(scr_x * dpi - hx)
                py = int(scr_y * dpi - hy)
            return cursor_img, px, py
        except Exception:
            LOGGER.exception("get_cursor_info failed entirely")
            return None

else:  # Linux

    _x11_display = None
    _xfixes_lib = None

    try:
        _x11_lib = ctypes.cdll.LoadLibrary("libX11.so.6")
        _xfixes_lib = ctypes.cdll.LoadLibrary("libXfixes.so.3")

        class _XFixesCursorImage(ctypes.Structure):
            _fields_ = [
                ("x", ctypes.c_short), ("y", ctypes.c_short),
                ("width", ctypes.c_ushort), ("height", ctypes.c_ushort),
                ("xhot", ctypes.c_ushort), ("yhot", ctypes.c_ushort),
                ("cursor_serial", ctypes.c_ulong),
                ("pixels", ctypes.POINTER(ctypes.c_ulong)),
                ("atom", ctypes.c_ulong), ("name", ctypes.c_char_p),
            ]

        _x11_lib.XOpenDisplay.restype = ctypes.c_void_p
        _x11_lib.XFree.argtypes = [ctypes.c_void_p]
        _xfixes_lib.XFixesGetCursorImage.restype = ctypes.POINTER(_XFixesCursorImage)
        _x11_display = _x11_lib.XOpenDisplay(None)
    except OSError:
        pass

    def get_cursor_info(screenshot_width, monitor=None):
        try:
            if _x11_display and _xfixes_lib:
                cp = _xfixes_lib.XFixesGetCursorImage(_x11_display)
                if cp:
                    ci = cp.contents
                    w, h = ci.width, ci.height
                    serial = ci.cursor_serial
                    if serial not in _cursor_cache:
                        rgba = bytearray(w * h * 4)
                        for i in range(w * h):
                            p = ci.pixels[i] & 0xFFFFFFFF
                            rgba[i * 4] = (p >> 16) & 0xFF
                            rgba[i * 4 + 1] = (p >> 8) & 0xFF
                            rgba[i * 4 + 2] = p & 0xFF
                            rgba[i * 4 + 3] = (p >> 24) & 0xFF
                        _cursor_cache[serial] = (
                            Image.frombytes("RGBA", (w, h), bytes(rgba)),
                            ci.xhot, ci.yhot,
                        )
                    cursor_img, hx, hy = _cursor_cache[serial]
                    if monitor:
                        px = ci.x - monitor["left"] - hx
                        py = ci.y - monitor["top"] - hy
                    else:
                        px, py = ci.x - hx, ci.y - hy
                    _x11_lib.XFree(cp)
                    return cursor_img, px, py
            pos = pyautogui.position()
            if monitor:
                dpi = screenshot_width / monitor["width"]
                px = int((pos[0] - monitor["left"]) * dpi)
                py = int((pos[1] - monitor["top"]) * dpi)
            else:
                dpi = screenshot_width / logical_width
                px = int(pos[0] * dpi)
                py = int(pos[1] * dpi)
            cache_key = f"fb_{dpi:.2f}"
            if cache_key not in _cursor_cache:
                _cursor_cache[cache_key] = (_make_arrow_cursor(scale=dpi), 0, 0)
            cursor_img, hx, hy = _cursor_cache[cache_key]
            return cursor_img, px - hx, py - hy
        except Exception:
            return None


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
            return _get_windows_clipboard_text().rstrip("\r\n")
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
            _set_windows_clipboard_text(text)
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text, text=True, timeout=5,
            )
    except Exception:
        LOGGER.exception("set_clipboard failed")


# ---------------------------------------------------------------------------
# Clipboard auto-sync removed — was polling every 5s causing CPU/GIL overhead


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


_status_frame_cache = {}


def _render_status_frame(size, title, message, accent=(210, 153, 34)):
    """Render a simple placeholder frame for capture states that cannot be streamed."""
    cache_key = (size, title, message, accent)
    if cache_key in _status_frame_cache:
        return _status_frame_cache[cache_key].copy()

    width, height = size
    img = Image.new("RGB", (width, height), (13, 17, 23))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    margin = max(24, min(width, height) // 18)
    panel = (margin, margin, width - margin, height - margin)
    draw.rounded_rectangle(panel, radius=18, fill=(22, 27, 34), outline=(48, 54, 61), width=2)

    badge_h = 36
    badge = (panel[0] + 24, panel[1] + 24, panel[0] + 180, panel[1] + 24 + badge_h)
    draw.rounded_rectangle(badge, radius=10, fill=accent)
    draw.text((badge[0] + 14, badge[1] + 11), "Windows notice", fill=(0, 0, 0), font=font)

    title_y = badge[3] + 18
    draw.text((panel[0] + 24, title_y), title, fill=(230, 237, 243), font=font)

    approx_chars = max(32, min(100, (panel[2] - panel[0] - 48) // 7))
    wrapped = textwrap.fill(message, width=approx_chars)
    body_y = title_y + 28
    draw.multiline_text(
        (panel[0] + 24, body_y),
        wrapped,
        fill=(139, 148, 158),
        font=font,
        spacing=8,
    )

    _status_frame_cache[cache_key] = img
    return img.copy()


def _get_selected_monitor(sct):
    mon_idx = settings.get("monitor", 1)
    num = len(sct.monitors)
    if num == 0:
        return {"top": 0, "left": 0, "width": logical_width, "height": logical_height}
    if mon_idx < 0 or mon_idx >= num:
        mon_idx = min(1, num - 1)
        settings["monitor"] = mon_idx
        LOGGER.warning("Monitor index reset to %d (available monitors: %d)", mon_idx, num)
    return sct.monitors[mon_idx]


if IS_WINDOWS:

    def _get_input_desktop_state():
        desk = _user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not desk:
            return None, ctypes.get_last_error()

        needed = wintypes.DWORD(0)
        try:
            _user32.GetUserObjectInformationW(desk, UOI_NAME, None, 0, ctypes.byref(needed))
            if not needed.value:
                return None, ctypes.get_last_error()
            buf = ctypes.create_unicode_buffer(max(1, needed.value // ctypes.sizeof(ctypes.c_wchar)))
            if not _user32.GetUserObjectInformationW(
                desk, UOI_NAME, buf, needed.value, ctypes.byref(needed),
            ):
                return None, ctypes.get_last_error()
            return buf.value, 0
        finally:
            _user32.CloseDesktop(desk)


    def _get_windows_capture_notice():
        desktop_name, desktop_error = _get_input_desktop_state()
        if desktop_name and desktop_name.lower() == "default":
            return None
        if not desktop_name and not desktop_error:
            return None

        if desktop_name:
            detail = (
                "Windows moved the session to the secure desktop "
                f"({desktop_name}). UAC and credential prompts are isolated there, "
                "so this server cannot capture the username/password dialog from the normal desktop."
            )
        elif desktop_error == 5:
            detail = (
                "Windows denied access to the current input desktop while an elevation prompt is active. "
                "That usually means the UAC dialog is on the secure desktop, so this server cannot capture "
                "the username/password prompt."
            )
        else:
            detail = (
                "Windows switched away from the normal input desktop while an elevation prompt is active. "
                "This server cannot capture the administrator credential dialog in that state."
            )
        return {
            "title": "UAC prompt is on the secure desktop",
            "message": detail,
        }

else:

    def _get_windows_capture_notice():
        return None


def _capture_monitor_frame(sct):
    """Capture the selected monitor, or a placeholder if capture is blocked."""
    monitor = _get_selected_monitor(sct)

    notice = _get_windows_capture_notice()
    if notice:
        frame = _render_status_frame(
            (monitor["width"], monitor["height"]),
            notice["title"],
            notice["message"],
        )
        return frame, frame.tobytes(), None

    img = sct.grab(monitor)
    raw = img.rgb
    pil_img = Image.frombytes("RGB", img.size, raw)
    scr_w = img.size[0]
    cur_pos = None

    try:
        cursor_data = get_cursor_info(scr_w, monitor)
        if cursor_data:
            cur_img, px, py = cursor_data
            cur_pos = (px, py)
            pil_img.paste(cur_img, (px, py), cur_img)
    except Exception:
        pass

    return pil_img, raw, cur_pos


# ---------------------------------------------------------------------------
# WebRTC — ScreenShareTrack + event loop + peer connection management
# ---------------------------------------------------------------------------

_peer_connections: dict[str, "RTCPeerConnection"] = {}
_webrtc_loop = None
_webrtc_clients: set[str] = set()
_webrtc_clients_lock = threading.Lock()


class _FrameBuffer:
    """Thread-safe latest-frame buffer shared between WebRTC track and Socket.IO encoder.
    Single screen capture feeds both transports — eliminates double-capture CPU cost."""

    def __init__(self):
        self._cv = threading.Condition()
        self.pil_img = None
        self.raw = None
        self.cur_pos = None
        self.seq = 0

    def put(self, pil_img, raw, cur_pos):
        with self._cv:
            self.pil_img = pil_img
            self.raw = raw
            self.cur_pos = cur_pos
            self.seq += 1
            self._cv.notify_all()

    def wait_next(self, after_seq, timeout=0.1):
        """Block until a new frame arrives (seq > after_seq) or timeout elapses."""
        with self._cv:
            self._cv.wait_for(lambda: self.seq > after_seq, timeout=timeout)
            return self.pil_img, self.raw, self.cur_pos, self.seq


_frame_buffer = _FrameBuffer()

if HAS_WEBRTC:

    class ScreenShareTrack(VideoStreamTrack):
        """Reads frames from the shared _FrameBuffer — no independent screen capture."""
        kind = "video"

        def __init__(self):
            super().__init__()
            self._last_seq = 0

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            loop = asyncio.get_event_loop()
            pil_img, _raw, _cur_pos, seq = await loop.run_in_executor(
                None,
                lambda: _frame_buffer.wait_next(self._last_seq, timeout=0.5),
            )
            self._last_seq = seq
            if pil_img is None:
                frame_np = np.zeros((logical_height, logical_width, 3), dtype=np.uint8)
            else:
                frame_np = np.array(pil_img)
            frame = VideoFrame.from_ndarray(frame_np, format="rgb24")
            frame.pts = pts
            frame.time_base = time_base
            return frame

    def _run_webrtc_loop():
        global _webrtc_loop
        _webrtc_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_webrtc_loop)
        _webrtc_loop.run_forever()

    LOGGER.info("WebRTC support: enabled (aiortc)")
else:
    LOGGER.info("WebRTC support: disabled (aiortc not installed, using Socket.IO fallback)")


def capture_and_stream():
    prev_sample = None
    prev_cur_pos = None
    last_sent = 0.0
    last_error_log = 0.0
    MIN_SEND_INTERVAL = 0.2  # force at least 5 fps even when idle

    sct = mss.mss()
    while True:
        frame_start = time.monotonic()

        with clients_lock:
            has_clients = bool(connected_clients)
        if not has_clients:
            time.sleep(0.5)
            continue

        try:
            pil_img, raw, cur_pos = _capture_monitor_frame(sct)
            frame_w, frame_h = pil_img.size

            buf_len = len(raw)
            chunk = 1024
            sample = (
                raw[:chunk]
                + raw[buf_len // 3 : buf_len // 3 + chunk]
                + raw[2 * buf_len // 3 : 2 * buf_len // 3 + chunk]
                + raw[-chunk:]
            )
            time_since_last = frame_start - last_sent
            cursor_changed = cur_pos != prev_cur_pos
            if (
                sample == prev_sample
                and not cursor_changed
                and time_since_last < MIN_SEND_INTERVAL
            ):
                elapsed = time.monotonic() - frame_start
                time.sleep(max(0, (1.0 / settings["fps"]) - elapsed))
                continue
            prev_sample = sample
            prev_cur_pos = cur_pos

            scale = settings["scale"]
            if abs(scale - 1.0) > 0.01:
                new_size = (int(pil_img.width * scale), int(pil_img.height * scale))
                pil_img = pil_img.resize(new_size, Image.BILINEAR)

            # Push to shared buffer — WebRTC track reads from here (no separate capture)
            _frame_buffer.put(pil_img, raw, cur_pos)

            # Only encode + emit via Socket.IO for clients not yet on WebRTC
            with clients_lock:
                all_clients = set(connected_clients)
            with _webrtc_clients_lock:
                socketio_clients = all_clients - _webrtc_clients
            if socketio_clients:
                buf = BytesIO()
                fmt = settings.get("format", "webp")
                if fmt == "png":
                    pil_img.save(buf, format="PNG", optimize=False)
                    mime = "image/png"
                elif fmt == "webp":
                    pil_img.save(buf, format="WEBP", quality=settings["quality"], method=0)
                    mime = "image/webp"
                else:
                    pil_img.save(buf, format="JPEG", quality=settings["quality"], optimize=False)
                    mime = "image/jpeg"
                frame_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                frame_payload = {
                    "img": frame_b64,
                    "w": frame_w,
                    "h": frame_h,
                    "fmt": mime,
                }
                if cur_pos is not None:
                    frame_payload["cx"] = cur_pos[0] / (frame_w or 1)
                    frame_payload["cy"] = cur_pos[1] / (frame_h or 1)
                for sid in socketio_clients:
                    socketio.emit("frame", frame_payload, to=sid)
            last_sent = time.monotonic()
        except (IndexError, KeyError):
            # Monitor config changed — recreate mss context and reset monitor
            now = time.monotonic()
            if now - last_error_log > 5.0:
                LOGGER.warning("Monitor config changed, refreshing mss context")
                last_error_log = now
            try:
                sct.close()
            except Exception:
                pass
            sct = mss.mss()
            settings["monitor"] = min(1, len(sct.monitors) - 1) if sct.monitors else 0
            _sync_active_monitor()
            time.sleep(0.5)
            continue
        except Exception:
            now = time.monotonic()
            if now - last_error_log > 5.0:
                LOGGER.exception("Screen capture error")
                last_error_log = now

        elapsed = time.monotonic() - frame_start
        time.sleep(max(0, (1.0 / settings["fps"]) - elapsed))


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


@app.route("/api/screenshot")
def api_screenshot():
    """Full-resolution screenshot of the active monitor as PNG."""
    with mss.mss() as sct:
        mon_idx = settings.get("monitor", 1)
        if mon_idx < 0 or mon_idx >= len(sct.monitors):
            mon_idx = 1
        monitor = sct.monitors[mon_idx]
        img = sct.grab(monitor)
        pil_img = Image.frombytes("RGB", img.size, img.rgb)

        try:
            cursor_data = get_cursor_info(img.size[0], monitor)
            if cursor_data:
                cur_img, cx, cy = cursor_data
                pil_img.paste(cur_img, (cx, cy), cur_img)
        except Exception:
            pass

        buf = BytesIO()
        pil_img.save(buf, format="PNG", optimize=False)
        buf.seek(0)
        ts = int(time.time())
        return send_file(buf, mimetype="image/png", download_name=f"screenshot_{ts}.png", as_attachment=True)


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
    try:
        monitors = _get_monitor_list()
    except Exception:
        LOGGER.exception("Failed to enumerate monitors on connect")
        monitors = [{"index": 1, "label": "Screen 1", "width": logical_width, "height": logical_height, "left": 0, "top": 0}]
    socketio.emit(
        "screen_info",
        {
            "width": logical_width,
            "height": logical_height,
            "webrtc": HAS_WEBRTC,
            "audio": HAS_AUDIO,
            "monitors": monitors,
            "active_monitor": settings["monitor"],
            "os": "macos" if IS_MACOS else "windows" if IS_WINDOWS else "linux",
        },
        to=request.sid,
    )
    LOGGER.info("Client connected: %s (total: %d)", request.sid, len(connected_clients))


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with clients_lock:
        connected_clients.discard(sid)
    if HAS_WEBRTC:
        pc = _peer_connections.pop(sid, None)
        if pc:
            try:
                if _webrtc_loop and _webrtc_loop.is_running():
                    asyncio.run_coroutine_threadsafe(pc.close(), _webrtc_loop)
            except Exception:
                LOGGER.exception("Error closing WebRTC peer connection for %s", sid)
        with _webrtc_clients_lock:
            _webrtc_clients.discard(sid)
    LOGGER.info("Client disconnected: %s (total: %d)", sid, len(connected_clients))


# ---------------------------------------------------------------------------
# Socket.IO events — mouse
# ---------------------------------------------------------------------------

@socketio.on("move")
def on_move(data):
    try:
        x, y = _mouse_xy(data)
        mouse.move(x, y)
    except Exception:
        LOGGER.exception("mouse.move failed")


@socketio.on("click")
def on_click(data):
    try:
        x, y = _mouse_xy(data)
        mouse.click(x, y, button=data.get("btn", "left"))
    except Exception:
        LOGGER.exception("mouse.click failed")


@socketio.on("dblclick")
def on_dblclick(data):
    try:
        x, y = _mouse_xy(data)
        mouse.double_click(x, y)
    except Exception:
        LOGGER.exception("mouse.double_click failed")


@socketio.on("tripleclick")
def on_tripleclick(data):
    try:
        x, y = _mouse_xy(data)
        mouse.triple_click(x, y)
    except Exception:
        LOGGER.exception("mouse.triple_click failed")


@socketio.on("mousedown")
def on_mousedown(data):
    try:
        x, y = _mouse_xy(data)
        mouse.mouse_down(x, y, button=data.get("btn", "left"))
    except Exception:
        LOGGER.exception("mouse.mouse_down failed")


@socketio.on("mouseup")
def on_mouseup(data):
    try:
        x, y = _mouse_xy(data)
        mouse.mouse_up(x, y, button=data.get("btn", "left"))
    except Exception:
        LOGGER.exception("mouse.mouse_up failed")


@socketio.on("drag")
def on_drag(data):
    try:
        x, y = _mouse_xy(data)
        mouse.drag(x, y)
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

    # On macOS, if the browser already resolved the character (e.g. "@" from Shift+2),
    # use Quartz to type it directly — avoids double-shift bugs with pyautogui.
    if IS_MACOS and modifiers == ["shift"] and len(key) == 1 and not key.isalpha():
        try:
            _type_char_quartz(key)
            return
        except Exception:
            pass

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


if IS_MACOS:
    def _type_char_quartz(ch):
        """Type a single character using Quartz CGEvents — bypasses pyautogui key mapping."""
        ev_down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(ev_down, len(ch), ch)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_down)
        ev_up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
        Quartz.CGEventKeyboardSetUnicodeString(ev_up, len(ch), ch)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)


@socketio.on("type_text")
def on_type_text(data):
    """Type a string of text, one character at a time."""
    text = data.get("text", "")
    if not text:
        return
    for ch in text:
        if ch == "\n":
            pyautogui.press("enter", _pause=False)
            time.sleep(0.03)
            continue
        if IS_MACOS:
            try:
                _type_char_quartz(ch)
            except Exception:
                LOGGER.debug("Quartz type_char failed for %r, falling back", ch)
                pyautogui.press(ch.lower() if len(ch) == 1 else ch, _pause=False)
        else:
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
        time.sleep(0.03)


def _dispatch_input_event(event_type, data):
    """Shared handler for mouse/keyboard events from Socket.IO and WebRTC DataChannel."""
    try:
        if event_type == "move":
            x, y = _mouse_xy(data)
            mouse.move(x, y)
        elif event_type == "click":
            x, y = _mouse_xy(data)
            mouse.click(x, y, button=data.get("btn", "left"))
        elif event_type == "dblclick":
            x, y = _mouse_xy(data)
            mouse.double_click(x, y)
        elif event_type == "tripleclick":
            x, y = _mouse_xy(data)
            mouse.triple_click(x, y)
        elif event_type == "mousedown":
            x, y = _mouse_xy(data)
            mouse.mouse_down(x, y, button=data.get("btn", "left"))
        elif event_type == "mouseup":
            x, y = _mouse_xy(data)
            mouse.mouse_up(x, y, button=data.get("btn", "left"))
        elif event_type == "drag":
            x, y = _mouse_xy(data)
            mouse.drag(x, y)
        elif event_type == "scroll":
            mouse.scroll(int(data.get("dy", 0)))
        elif event_type == "keydown":
            key = data.get("key", "")
            if key in MODIFIER_KEYS:
                return
            modifiers = _map_modifier_flag(data)
            mapped = KEY_MAP.get(key)
            if mapped is None:
                mapped = key.lower() if len(key) == 1 else None
            if mapped:
                _send_key_combo(modifiers, mapped)
        elif event_type == "hotkey":
            modifiers = data.get("modifiers", [])
            key = data.get("key", "")
            if IS_MACOS and modifiers == ["shift"] and len(key) == 1 and not key.isalpha():
                try:
                    _type_char_quartz(key)
                    return
                except Exception:
                    pass
            mapped_mods = [MOD_MAP[m] for m in modifiers if m in MOD_MAP]
            mapped_key = KEY_MAP.get(key)
            if mapped_key is None:
                mapped_key = key.lower() if len(key) == 1 else None
            if mapped_key:
                _send_key_combo(mapped_mods, mapped_key)
        elif event_type == "type_text":
            text = data.get("text", "")
            for ch in text:
                if ch == "\n":
                    pyautogui.press("enter", _pause=False)
                    time.sleep(0.03)
                    continue
                if IS_MACOS:
                    try:
                        _type_char_quartz(ch)
                    except Exception:
                        pyautogui.press(ch.lower() if len(ch) == 1 else ch, _pause=False)
                else:
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
                time.sleep(0.03)
    except Exception:
        LOGGER.exception("Input dispatch error: type=%s", event_type)


# ---------------------------------------------------------------------------
# Socket.IO events — clipboard
# ---------------------------------------------------------------------------

@socketio.on("clipboard_get")
def on_clipboard_get(_data=None):
    text = get_clipboard()
    socketio.emit("clipboard_content", {"text": text}, to=request.sid)


@socketio.on("clipboard_set")
def on_clipboard_set(data):
    global _last_clipboard_content
    text = data.get("text", "")
    set_clipboard(text)
    _last_clipboard_content = text
    socketio.emit("clipboard_content", {"text": text}, to=request.sid)


# ---------------------------------------------------------------------------
# Socket.IO events — settings
# ---------------------------------------------------------------------------

@socketio.on("update_settings")
def on_update_settings(data):
    if "fps" in data:
        settings["fps"] = max(1, min(60, int(data["fps"])))
    if "quality" in data:
        settings["quality"] = max(10, min(100, int(data["quality"])))
    if "scale" in data:
        settings["scale"] = max(0.1, min(2.0, float(data["scale"])))
    if "format" in data and data["format"] in ("jpeg", "webp", "png"):
        settings["format"] = data["format"]
    LOGGER.info("Settings updated: %s", settings)


@socketio.on("select_monitor")
def on_select_monitor(data):
    idx = int(data.get("index", 1))
    with mss.mss() as sct:
        if idx < 0 or idx >= len(sct.monitors):
            idx = 1
    settings["monitor"] = idx
    _sync_active_monitor()
    mon = _get_monitor_list()
    selected = next((m for m in mon if m["index"] == idx), mon[1] if len(mon) > 1 else mon[0])
    LOGGER.info("Monitor switched to %d (%s, %dx%d)", idx, selected["label"], selected["width"], selected["height"])
    socketio.emit("monitor_changed", {
        "index": idx,
        "width": selected["width"],
        "height": selected["height"],
        "label": selected["label"],
    })


# ---------------------------------------------------------------------------
# Socket.IO events — latency
# ---------------------------------------------------------------------------

@socketio.on("ping_check")
def on_ping_check(data):
    socketio.emit("pong_check", data, to=request.sid)


# ---------------------------------------------------------------------------
# Socket.IO events — WebRTC signaling
# ---------------------------------------------------------------------------

if HAS_WEBRTC:

    @socketio.on("webrtc_offer")
    def on_webrtc_offer(data):
        sid = request.sid

        async def _handle_offer():
            try:
                if sid in _peer_connections:
                    old_pc = _peer_connections.pop(sid)
                    await old_pc.close()

                pc = RTCPeerConnection()
                _peer_connections[sid] = pc

                @pc.on("connectionstatechange")
                async def on_state_change():
                    LOGGER.info("WebRTC state [%s]: %s", sid, pc.connectionState)
                    if pc.connectionState == "connected":
                        with _webrtc_clients_lock:
                            _webrtc_clients.add(sid)
                    elif pc.connectionState in ("failed", "closed", "disconnected"):
                        _peer_connections.pop(sid, None)
                        with _webrtc_clients_lock:
                            _webrtc_clients.discard(sid)

                @pc.on("datachannel")
                def on_datachannel(channel):
                    LOGGER.info("DataChannel [%s] ready for %s", channel.label, sid)

                    @channel.on("message")
                    def on_message(message):
                        try:
                            data = json.loads(message)
                            event_type = data.pop("t", None)
                            if not event_type:
                                return
                            # type_text has per-char sleeps — run in a thread
                            if event_type == "type_text":
                                threading.Thread(
                                    target=_dispatch_input_event,
                                    args=(event_type, data),
                                    daemon=True,
                                ).start()
                            else:
                                _dispatch_input_event(event_type, data)
                        except Exception:
                            LOGGER.exception("DataChannel message error for %s", sid)

                pc.addTrack(ScreenShareTrack())

                offer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                socketio.emit("webrtc_answer", {
                    "sdp": pc.localDescription.sdp,
                    "type": pc.localDescription.type,
                }, to=sid)
            except Exception:
                LOGGER.exception("WebRTC offer handler failed for %s", sid)
                _peer_connections.pop(sid, None)

        if not _webrtc_loop or not _webrtc_loop.is_running():
            LOGGER.warning("WebRTC event loop not running, ignoring offer from %s", sid)
            return
        asyncio.run_coroutine_threadsafe(_handle_offer(), _webrtc_loop)

    @socketio.on("webrtc_ice")
    def on_webrtc_ice(data):
        sid = request.sid
        pc = _peer_connections.get(sid)
        if not pc:
            return

        async def _add_ice():
            from aiortc import RTCIceCandidate
            try:
                candidate_str = data.get("candidate", "")
                sdp_mid = data.get("sdpMid")
                sdp_mline_index = data.get("sdpMLineIndex")
                if not candidate_str:
                    return
                # Strip "candidate:" prefix browsers include
                raw = candidate_str[len("candidate:"):] if candidate_str.startswith("candidate:") else candidate_str
                parts = raw.split()
                # SDP candidate format:
                # foundation component protocol priority ip port typ type [raddr ip] [rport port] ...
                foundation = parts[0]
                component = int(parts[1])
                protocol = parts[2].lower()
                priority = int(parts[3])
                ip = parts[4]
                port = int(parts[5])
                candidate_type = parts[7]  # parts[6] == "typ"
                related_address = None
                related_port = None
                for i in range(8, len(parts) - 1, 2):
                    if parts[i] == "raddr":
                        related_address = parts[i + 1]
                    elif parts[i] == "rport":
                        related_port = int(parts[i + 1])
                rtc_candidate = RTCIceCandidate(
                    component=component,
                    foundation=foundation,
                    ip=ip,
                    port=port,
                    priority=priority,
                    protocol=protocol,
                    type=candidate_type,
                    relatedAddress=related_address,
                    relatedPort=related_port,
                    sdpMid=sdp_mid,
                    sdpMLineIndex=sdp_mline_index,
                )
                await pc.addIceCandidate(rtc_candidate)
                LOGGER.debug("Added ICE candidate from client [%s]: %s:%s", sid, ip, port)
            except Exception:
                LOGGER.exception("ICE candidate error")

        asyncio.run_coroutine_threadsafe(_add_ice(), _webrtc_loop)


# ---------------------------------------------------------------------------
# Socket.IO events — audio streaming
# ---------------------------------------------------------------------------

def _audio_stream_worker():
    """Capture system audio (loopback) and stream PCM chunks via callback-based stream."""
    global audio_active
    if not HAS_AUDIO:
        return

    dev = audio_device_index if audio_device_index is not None else _default_loopback_device
    extra = audio_loopback_extra if audio_loopback_extra is not None else _default_loopback_extra

    try:
        dev_info = sd.query_devices(dev) if dev is not None else sd.query_devices(kind="input")
        if extra is not None:
            channels = min(AUDIO_CHANNELS, int(dev_info["max_output_channels"]) or AUDIO_CHANNELS)
        else:
            channels = min(AUDIO_CHANNELS, int(dev_info["max_input_channels"]) or AUDIO_CHANNELS)
        rate = int(dev_info["default_samplerate"]) if dev is not None else AUDIO_SAMPLE_RATE
    except Exception:
        channels = AUDIO_CHANNELS
        rate = AUDIO_SAMPLE_RATE

    audio_q = queue.Queue(maxsize=50)

    def _audio_callback(indata, frames, time_info, status):
        try:
            audio_q.put_nowait(bytes(indata))
        except queue.Full:
            pass

    stream_kwargs = {
        "samplerate": rate,
        "channels": channels,
        "dtype": "int16",
        "blocksize": AUDIO_CHUNK,
        "device": dev,
        "callback": _audio_callback,
    }
    if extra is not None:
        stream_kwargs["extra_settings"] = extra

    dev_name = "default"
    try:
        if dev is not None:
            dev_info_log = sd.query_devices(dev)
            dev_name = dev_info_log["name"]
            LOGGER.info(
                "Opening audio stream: device=%d (%s), hostapi=%d, in_ch=%d, out_ch=%d, rate=%d, channels=%d, wasapi=%s",
                dev, dev_name, dev_info_log["hostapi"], dev_info_log["max_input_channels"],
                dev_info_log["max_output_channels"], rate, channels, extra is not None,
            )
    except Exception:
        pass

    def _pump_stream(kwargs, label):
        import struct
        with sd.RawInputStream(**kwargs) as stream:
            LOGGER.info("Audio streaming started — %s, %dHz %dch", label, kwargs["samplerate"], kwargs["channels"])
            chunk_count = 0
            silent_count = 0
            while audio_active:
                try:
                    raw = audio_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                chunk_count += 1
                # Silence detection — log every 100 chunks (~5s) whether we're getting real audio
                if chunk_count % 100 == 1:
                    n_samples = min(len(raw) // 2, 200)
                    samples = struct.unpack(f"<{n_samples}h", raw[:n_samples * 2])
                    peak = max(abs(s) for s in samples) if samples else 0
                    if peak < 50:
                        silent_count += 1
                        if silent_count <= 3:
                            LOGGER.info("Audio chunk #%d: SILENCE (peak=%d) — Stereo Mix may not be capturing", chunk_count, peak)
                    else:
                        silent_count = 0
                        LOGGER.info("Audio chunk #%d: active (peak=%d)", chunk_count, peak)
                encoded = base64.b64encode(raw).decode("ascii")
                socketio.emit("audio_data", {
                    "pcm": encoded,
                    "rate": kwargs["samplerate"],
                    "channels": kwargs["channels"],
                })

    try:
        mode = "WASAPI loopback" if extra is not None else "direct"
        _pump_stream(stream_kwargs, f"{dev_name} ({mode})")
    except Exception:
        LOGGER.exception("Audio stream failed for device %s, trying default input", dev_name)
        if dev is not None:
            try:
                fallback_kwargs = {
                    "samplerate": AUDIO_SAMPLE_RATE,
                    "channels": 1,
                    "dtype": "int16",
                    "blocksize": AUDIO_CHUNK,
                    "device": None,
                    "callback": _audio_callback,
                }
                _pump_stream(fallback_kwargs, "default mic (fallback)")
            except Exception:
                LOGGER.exception("Fallback audio stream also failed")
    finally:
        audio_active = False
        LOGGER.info("Audio streaming stopped")


@socketio.on("audio_start")
def on_audio_start(data=None):
    global audio_active, audio_thread, audio_device_index, audio_loopback_extra
    if not HAS_AUDIO:
        socketio.emit("audio_status", {"active": False, "error": "Audio unavailable — run: pip install sounddevice"}, to=request.sid)
        return
    if data and "device" in data:
        audio_device_index = data["device"]
        audio_loopback_extra = data.get("wasapi_loopback") and _default_loopback_extra or None
    with audio_lock:
        if audio_active:
            return
        audio_active = True
        audio_thread = threading.Thread(target=_audio_stream_worker, daemon=True)
        audio_thread.start()
    socketio.emit("audio_status", {"active": True})


@socketio.on("audio_stop")
def on_audio_stop(_data=None):
    global audio_active
    with audio_lock:
        audio_active = False
    socketio.emit("audio_status", {"active": False})


@app.route("/api/audio_devices")
def list_audio_devices():
    if not HAS_AUDIO:
        return jsonify({"available": False, "devices": [], "error": "sounddevice not installed"})
    try:
        devices = sd.query_devices()
        result = []
        loopback_keywords = {"blackhole", "soundflower", "loopback", "monitor", "stereo mix", "what u hear"}
        has_wasapi_loopback = False

        if IS_WINDOWS:
            try:
                wasapi_api_idx = None
                for i, api in enumerate(sd.query_hostapis()):
                    if "WASAPI" in api["name"]:
                        wasapi_api_idx = i
                        break
                if wasapi_api_idx is not None:
                    for i, d in enumerate(devices):
                        if d["hostapi"] == wasapi_api_idx and d["max_output_channels"] > 0:
                            result.append({
                                "index": i,
                                "name": d["name"] + " (System Audio — WASAPI Loopback)",
                                "channels": max(d["max_output_channels"], 2),
                                "sample_rate": int(d["default_samplerate"]),
                                "is_loopback": True,
                                "recommended": not has_wasapi_loopback,
                            })
                            has_wasapi_loopback = True
                            break
            except Exception:
                pass

        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0:
                name_lower = d["name"].lower()
                is_loopback = any(kw in name_lower for kw in loopback_keywords)
                result.append({
                    "index": i,
                    "name": d["name"],
                    "channels": d["max_input_channels"],
                    "sample_rate": int(d["default_samplerate"]),
                    "is_loopback": is_loopback,
                    "recommended": is_loopback and not has_wasapi_loopback,
                })

        recommended = _default_loopback_device
        hint = None
        if not any(d.get("is_loopback") or d.get("recommended") for d in result):
            if IS_MACOS:
                hint = "Install BlackHole (brew install blackhole-2ch) to capture system audio"
            elif IS_WINDOWS:
                hint = "Enable Stereo Mix in Sound settings to capture system audio"
            else:
                hint = "Use a PulseAudio monitor source to capture system audio"

        return jsonify({
            "available": True,
            "devices": result,
            "recommended": recommended,
            "hint": hint,
        })
    except Exception as e:
        return jsonify({"available": False, "devices": [], "error": str(e)})


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

    if HAS_WEBRTC:
        webrtc_thread = threading.Thread(target=_run_webrtc_loop, daemon=True)
        webrtc_thread.start()
        LOGGER.info("WebRTC event loop started")

    stream_thread = threading.Thread(target=capture_and_stream, daemon=True)
    stream_thread.start()

    local_ip = get_local_ip()

    # Port: CLI flag > config.json > default 5050
    import argparse, json as _json
    _parser = argparse.ArgumentParser(add_help=False)
    _parser.add_argument("--port", type=int, default=None)
    _args, _ = _parser.parse_known_args()
    if _args.port:
        port = _args.port
    else:
        try:
            _cfg = _json.loads((Path(__file__).parent / "config.json").read_text())
            port = int(_cfg.get("server_port", 5050))
        except Exception:
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
