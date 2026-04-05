(function() {
"use strict";

const $ = (s) => document.getElementById(s);
const canvas = $("screen"), ctx = canvas.getContext("2d");
const canvasWrap = $("canvas-wrap"), canvasCenter = $("canvas-center");
const screenContainer = $("screen-container");
const screenVideo = $("screen-video");
const bottomPanel = $("bottom-panel");
const toast = $("toast");

let viewOnly = false;
let useWebRTC = false;
let rtcPC = null;
let inputDC = null;

// ===== localStorage helpers =====
const STORAGE_KEY = "pc_remote_settings";
function loadSavedSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}
function saveSettings(s) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); } catch {}
}

// ===== State =====
let screenW = 1, screenH = 1;
let cursorX = 0.5, cursorY = 0.5;
let remoteCursorX = -1, remoteCursorY = -1;
let isConnected = false;
let frameCount = 0, lastFrameBytes = 0;
let interactionMode = "direct";
let cursorSens = 2.0;

// Zoom
const ZOOM_MIN = 0.25, ZOOM_MAX = 5.0, ZOOM_STEP = 0.25;
let zoomLevel = 1, baseFitW = 100, baseFitH = 100;

// Sticky modifiers
const mods = { ctrl: false, alt: false, cmd: false, shift: false };
const isMacBrowser = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
let isRemoteMac = true; // default; will be updated from screen_info

function adaptShortcutLabels(remoteOS) {
  isRemoteMac = (remoteOS === "macos");
  if (!isRemoteMac) {
    document.querySelectorAll(".vk-key.shortcut, .sc-label").forEach(el => {
      el.innerHTML = el.innerHTML.replace(/\u2318/g, "Ctrl").replace(/\u2963/g, "Ctrl").replace(/\u2325/g, "Alt");
    });
    const cmdModBtn = document.querySelector('.vk-key.mod[data-mod="cmd"]');
    if (cmdModBtn) cmdModBtn.innerHTML = "Ctrl/Win";
  }
}

// Active panel
let activePanel = null;
const PANEL_HEIGHT = 300;

// Audio
let audioActive = false;
let audioCtx = null, audioProcessor = null, audioQueue = [];

// Latency
let latencyMs = 0;

// ===== Socket =====
const tunnelMatch = window.location.pathname.match(/^(\/tunnel\/[^\/]+)/);
const BASE = tunnelMatch ? tunnelMatch[1] : "";
const socket = io({ path: BASE + "/socket.io/", transports: ["websocket"] });

socket.on("connect", () => {
  isConnected = true;
  $("status-dot").classList.add("connected");
  $("status-text").textContent = "Connected";
  setTimeout(() => $("connect-banner").classList.add("hidden"), 400);

  // Send saved settings to server on connect
  const saved = loadSavedSettings();
  if (saved) {
    socket.emit("update_settings", {
      fps: saved.fps || 30,
      quality: saved.quality || 70,
      scale: (saved.scale || 75) / 100,
      format: saved.format || "webp",
    });
    // Restore UI
    $("sl-fps").value = saved.fps || 30;
    $("sl-q").value = saved.quality || 70;
    $("sl-s").value = saved.scale || 75;
    $("sel-fmt").value = saved.format || "webp";
    if (saved.sens) { cursorSens = saved.sens; $("sl-sens").value = saved.sens * 10; $("val-sens").textContent = saved.sens.toFixed(1); }
    $("val-fps").textContent = saved.fps || 30;
    $("val-q").textContent = saved.quality || 70;
    $("val-s").textContent = ((saved.scale || 75) / 100).toFixed(2);
  }
});
socket.on("disconnect", () => {
  isConnected = false;
  $("status-dot").classList.remove("connected");
  $("status-text").textContent = "Disconnected";
  $("connect-banner").classList.remove("hidden");
  _cleanupWebRTC();
});
socket.on("screen_info", (d) => {
  screenW = d.width; screenH = d.height;

  if (d.os) adaptShortcutLabels(d.os);

  // Populate monitor selector
  if (d.monitors && d.monitors.length > 0) {
    const sel = $("monitor-sel");
    sel.innerHTML = "";
    d.monitors.forEach(m => {
      const opt = document.createElement("option");
      opt.value = m.index;
      opt.textContent = m.label + " (" + m.width + "x" + m.height + ")";
      sel.appendChild(opt);
    });
    if (d.active_monitor !== undefined) sel.value = d.active_monitor;
    sel.style.display = d.monitors.length > 2 ? "" : "none";
  }

  if (d.audio === false) {
    $("btn-audio").disabled = true;
    $("btn-audio").title = "Audio unavailable (sounddevice not installed on remote)";
    $("btn-audio").style.opacity = "0.35";
  }

  if (d.webrtc && !rtcPC) startWebRTC();
});

socket.on("monitor_changed", (d) => {
  showToast("Switched to " + d.label + " (" + d.width + "x" + d.height + ")");
});

let _monitorSwitching = false;
$("monitor-sel").onchange = () => {
  const idx = parseInt($("monitor-sel").value);
  socket.emit("select_monitor", { index: idx });
  if (useWebRTC || rtcPC) {
    _monitorSwitching = true;
    _cleanupWebRTC();
    showToast("Switching monitor…");
    setTimeout(() => { _monitorSwitching = false; startWebRTC(); }, 600);
  }
};

// ===== Frames with requestAnimationFrame =====
const frameImg = new Image();
let framePending = false, firstFrame = true, newFrameReady = false;

frameImg.onload = () => {
  if (canvas.width !== frameImg.width || canvas.height !== frameImg.height) {
    canvas.width = frameImg.width;
    canvas.height = frameImg.height;
    if (firstFrame) { firstFrame = false; recalcFit(); applyZoom(); }
  }
  newFrameReady = true;
  framePending = false;
  frameCount++;
};

function renderLoop() {
  if (newFrameReady) {
    ctx.drawImage(frameImg, 0, 0);
    newFrameReady = false;
    if (remoteCursorX >= 0 && remoteCursorY >= 0) drawRemoteCursor();
  }
  requestAnimationFrame(renderLoop);
}
requestAnimationFrame(renderLoop);

socket.on("frame", (d) => {
  if (useWebRTC) return;  // WebRTC video element handles display; ignore Socket.IO frames
  if (framePending) return;
  framePending = true;
  screenW = d.w; screenH = d.h;
  if (d.cx != null && d.cy != null) { remoteCursorX = d.cx; remoteCursorY = d.cy; }
  const mime = d.fmt || "image/jpeg";
  lastFrameBytes = d.img.length * 0.75; // approximate decoded size
  frameImg.src = "data:" + mime + ";base64," + d.img;
});

setInterval(() => {
  $("fps-display").textContent = frameCount + " FPS";
  if (useWebRTC) {
    $("frame-info").textContent = "WebRTC";
  } else {
    const kb = Math.round(lastFrameBytes / 1024);
    $("frame-info").textContent = kb > 0 ? "~" + kb + "KB/f" : "";
  }
  frameCount = 0;
}, 1000);

// ===== WebRTC =====
const WEBRTC_CONNECT_TIMEOUT_MS = 10000;
let _rtcTimeout = null;

function _cleanupWebRTC() {
  clearTimeout(_rtcTimeout);
  _rtcTimeout = null;
  if (inputDC) { try { inputDC.close(); } catch {} inputDC = null; }
  if (rtcPC) {
    rtcPC.onconnectionstatechange = null;
    rtcPC.ontrack = null;
    rtcPC.onicecandidate = null;
    rtcPC.close();
    rtcPC = null;
  }
  useWebRTC = false;
  screenVideo.classList.add("hidden");
  canvas.classList.remove("overlay");
}

function startWebRTC() {
  _cleanupWebRTC();
  rtcPC = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });

  _rtcTimeout = setTimeout(() => {
    if (rtcPC && rtcPC.connectionState !== "connected") {
      LOGGER.log("WebRTC connect timeout after " + WEBRTC_CONNECT_TIMEOUT_MS + "ms — falling back to Socket.IO");
      _cleanupWebRTC();
      showToast("WebRTC timed out — using Socket.IO");
    }
  }, WEBRTC_CONNECT_TIMEOUT_MS);

  // Input DataChannel: unordered + no retransmits = UDP semantics, lowest latency
  inputDC = rtcPC.createDataChannel("input", { ordered: false, maxRetransmits: 0 });
  inputDC.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.t === "dc_pong") {
        latencyMs = performance.now() - msg.ts;
        $("latency-display").textContent = Math.round(latencyMs) + "ms";
        $("latency-display").style.color = latencyMs < 50 ? "var(--green)" : latencyMs < 150 ? "var(--orange)" : "var(--red)";
      }
    } catch {}
  };

  rtcPC.addTransceiver("video", { direction: "recvonly" });

  rtcPC.ontrack = (ev) => {
    // Wire up the stream now so video plays the instant ICE connects.
    // Do NOT switch display or set useWebRTC here — wait for "connected".
    LOGGER.log("WebRTC track received — awaiting ICE");
    screenVideo.srcObject = ev.streams[0];
    if ("requestVideoFrameCallback" in HTMLVideoElement.prototype) {
      function countFrame() { frameCount++; screenVideo.requestVideoFrameCallback(countFrame); }
      screenVideo.requestVideoFrameCallback(countFrame);
    }
  };

  rtcPC.onicecandidate = (ev) => {
    if (ev.candidate) {
      socket.emit("webrtc_ice", {
        candidate: ev.candidate.candidate,
        sdpMid: ev.candidate.sdpMid,
        sdpMLineIndex: ev.candidate.sdpMLineIndex,
      });
    }
  };

  rtcPC.onconnectionstatechange = () => {
    if (_monitorSwitching) return;
    LOGGER.log("WebRTC state:", rtcPC ? rtcPC.connectionState : "null");
    if (rtcPC && rtcPC.connectionState === "connected") {
      clearTimeout(_rtcTimeout);
      _rtcTimeout = null;
      screenVideo.classList.remove("hidden");
      canvas.classList.add("overlay");
      useWebRTC = true;
      LOGGER.log("WebRTC connected");
      if (screenVideo.videoWidth) {
        canvas.width = screenVideo.videoWidth;
        canvas.height = screenVideo.videoHeight;
        recalcFit(); applyZoom();
      } else {
        screenVideo.onloadedmetadata = () => {
          canvas.width = screenVideo.videoWidth;
          canvas.height = screenVideo.videoHeight;
          recalcFit(); applyZoom();
        };
      }
    }
    if (rtcPC && (rtcPC.connectionState === "failed" || rtcPC.connectionState === "disconnected")) {
      _cleanupWebRTC();
      showToast("WebRTC unavailable — using Socket.IO");
    }
  };

  rtcPC.createOffer().then(offer => {
    return rtcPC.setLocalDescription(offer);
  }).then(() => {
    socket.emit("webrtc_offer", {
      sdp: rtcPC.localDescription.sdp,
      type: rtcPC.localDescription.type,
    });
  }).catch(err => {
    LOGGER.error("WebRTC offer failed:", err);
    _cleanupWebRTC();
    showToast("WebRTC setup failed — using Socket.IO fallback");
  });
}

socket.on("webrtc_answer", (d) => {
  if (!rtcPC) return;
  rtcPC.setRemoteDescription(new RTCSessionDescription(d)).catch(err => {
    LOGGER.error("WebRTC answer error:", err);
  });
});

socket.on("webrtc_ice_candidate", (d) => {
  if (!rtcPC) return;
  rtcPC.addIceCandidate(new RTCIceCandidate(d)).catch(() => {});
});

const LOGGER = { log: console.log.bind(console), error: console.error.bind(console) };

// Route input events over WebRTC DataChannel (UDP, unordered) when available, else Socket.IO
function emitInput(type, data) {
  if (inputDC && inputDC.readyState === "open") {
    inputDC.send(JSON.stringify({ t: type, ...data }));
  } else {
    socket.emit(type, data);
  }
}

// ===== Cursor =====
function drawRemoteCursor() {
  const cx = remoteCursorX * canvas.width, cy = remoteCursorY * canvas.height;
  const s = Math.max(1, canvas.width / 1920) * 1.2;
  ctx.save();
  ctx.translate(cx, cy);
  ctx.scale(s, s);
  // Arrow pointer — black outline + white fill (matches OS cursors)
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(0, 20);
  ctx.lineTo(4.5, 15.5);
  ctx.lineTo(7.5, 23);
  ctx.lineTo(10, 21.5);
  ctx.lineTo(7, 14);
  ctx.lineTo(12, 14);
  ctx.closePath();
  ctx.fillStyle = "rgba(0,0,0,0.85)";
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(1.2, 2);
  ctx.lineTo(1.2, 18);
  ctx.lineTo(5, 14.5);
  ctx.lineTo(8, 21.5);
  ctx.lineTo(9, 20.5);
  ctx.lineTo(6.5, 13);
  ctx.lineTo(10.5, 13);
  ctx.closePath();
  ctx.fillStyle = "rgba(255,255,255,0.95)";
  ctx.fill();
  ctx.restore();
}

function drawCursor() {
  const cx = cursorX * canvas.width, cy = cursorY * canvas.height;
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, 4, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(88,166,255,0.5)";
  ctx.fill();
  ctx.strokeStyle = "rgba(255,255,255,0.7)";
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.restore();
}

// ===== Toast =====
let toastTimer = null;
function showToast(text) {
  toast.textContent = text; toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2000);
}

// ===== Zoom =====
function recalcFit() {
  const srcW = useWebRTC ? (screenVideo.videoWidth || canvas.width) : canvas.width;
  const srcH = useWebRTC ? (screenVideo.videoHeight || canvas.height) : canvas.height;
  if (!srcW) return;
  const w = canvasWrap.clientWidth, h = canvasWrap.clientHeight;
  const a = srcW / srcH;
  baseFitW = w; baseFitH = baseFitW / a;
  if (baseFitH > h) { baseFitH = h; baseFitW = baseFitH * a; }
}
function applyZoom() {
  const w = Math.round(baseFitW * zoomLevel), h = Math.round(baseFitH * zoomLevel);
  screenContainer.style.width = w + "px"; screenContainer.style.height = h + "px";
  if (!useWebRTC) { canvas.style.width = w + "px"; canvas.style.height = h + "px"; }
  canvasCenter.style.minWidth = zoomLevel <= 1.01 ? "100%" : w + "px";
  canvasCenter.style.minHeight = zoomLevel <= 1.01 ? "100%" : h + "px";
  $("zoom-label").textContent = zoomLevel === 1 ? "Fit" : Math.round(zoomLevel * 100) + "%";
  if (typeof updatePanButtons === "function") updatePanButtons();
}
function setZoom(z) { zoomLevel = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(z * 4) / 4)); applyZoom(); }
window.addEventListener("resize", () => { recalcFit(); applyZoom(); });
$("btn-zi").onclick = () => setZoom(zoomLevel + ZOOM_STEP);
$("btn-zo").onclick = () => setZoom(zoomLevel - ZOOM_STEP);
$("btn-zf").onclick = () => { setZoom(1); canvasWrap.scrollTo(0, 0); };

// ===== Panel management =====
function openPanel(id) {
  if (activePanel === id) { closePanel(); return; }
  document.querySelectorAll(".panel-page").forEach(p => p.classList.remove("visible"));
  $(id).classList.add("visible");
  document.documentElement.style.setProperty("--panel-h", PANEL_HEIGHT + "px");
  bottomPanel.classList.add("open");
  activePanel = id;
  ["btn-p-kb","btn-p-tp","btn-p-clip","btn-p-files"].forEach(b => $(b).classList.remove("active"));
  const btnMap = {"panel-kb":"btn-p-kb","panel-tp":"btn-p-tp","panel-clip":"btn-p-clip","panel-files":"btn-p-files"};
  if (btnMap[id]) $(btnMap[id]).classList.add("active");
  setTimeout(() => { recalcFit(); applyZoom(); }, 220);
}
function closePanel() {
  bottomPanel.classList.remove("open");
  document.documentElement.style.setProperty("--panel-h", "0px");
  activePanel = null;
  ["btn-p-kb","btn-p-tp","btn-p-clip","btn-p-files"].forEach(b => $(b).classList.remove("active"));
  setTimeout(() => { recalcFit(); applyZoom(); }, 220);
}

$("btn-p-kb").onclick = () => openPanel("panel-kb");
$("btn-p-tp").onclick = () => openPanel("panel-tp");
$("btn-p-clip").onclick = () => { openPanel("panel-clip"); socket.emit("clipboard_get"); };
$("btn-p-files").onclick = () => { openPanel("panel-files"); loadFiles(); };

// ===== Coordinate helpers =====
function canvasCoords(cx, cy) {
  const r = canvas.getBoundingClientRect();
  return { x: Math.max(0, Math.min(1, (cx - r.left) / r.width)), y: Math.max(0, Math.min(1, (cy - r.top) / r.height)) };
}
function throttle(fn, ms) { let l = 0; return (...a) => { const n = Date.now(); if (n - l >= ms) { l = n; fn(...a); } }; }
const emitMove = throttle((c) => emitInput("move", c), 8);

// ===== Sticky modifiers =====
function getActiveMods() {
  const m = [];
  if (mods.ctrl) m.push("ctrl");
  if (mods.alt) m.push("alt");
  if (mods.cmd) m.push("cmd");
  if (mods.shift) m.push("shift");
  return m;
}
let modsTimer = null;
function releaseMods() {
  mods.ctrl = mods.alt = mods.cmd = mods.shift = false;
  document.querySelectorAll(".vk-key.mod").forEach(b => b.classList.remove("held"));
  clearTimeout(modsTimer);
}
function scheduleModsClear() {
  clearTimeout(modsTimer);
  if (mods.ctrl || mods.alt || mods.cmd || mods.shift) {
    modsTimer = setTimeout(() => { releaseMods(); showToast("Sticky mods auto-cleared"); }, 8000);
  }
}
function sendKeyWithMods(key) {
  if (viewOnly) return;
  const m = getActiveMods();
  if (m.length > 0) {
    emitInput("hotkey", { modifiers: m, key });
    releaseMods();
  } else {
    emitInput("keydown", { key, ctrl: false, alt: false, shift: false, meta: false });
  }
}

// Modifier toggle buttons
document.querySelectorAll(".vk-key.mod").forEach(btn => {
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    const mod = btn.dataset.mod;
    mods[mod] = !mods[mod];
    btn.classList.toggle("held", mods[mod]);
    scheduleModsClear();
  });
  btn.addEventListener("touchstart", (e) => e.stopPropagation(), { passive: true });
});

// ===== Virtual Keyboard — key buttons =====
document.querySelectorAll(".vk-key[data-key]").forEach(btn => {
  function fire(e) {
    e.preventDefault(); e.stopPropagation();
    sendKeyWithMods(btn.dataset.key);
  }
  btn.addEventListener("click", fire);
  btn.addEventListener("touchend", (e) => { e.preventDefault(); e.stopPropagation(); fire(e); });
});

// Shortcut buttons
document.querySelectorAll(".vk-key[data-hk]").forEach(btn => {
  function fire(e) {
    e.preventDefault(); e.stopPropagation();
    if (viewOnly) return;
    const parts = btn.dataset.hk.split("+");
    const key = parts.pop();
    emitInput("hotkey", { modifiers: parts, key });
  }
  btn.addEventListener("click", fire);
  btn.addEventListener("touchend", (e) => { e.preventDefault(); e.stopPropagation(); fire(e); });
});

// Text input send
$("vk-text-send").onclick = () => {
  const t = $("vk-text-input").value;
  if (t) { emitInput("type_text", { text: t }); $("vk-text-input").value = ""; showToast("Text sent"); }
};
$("vk-text-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("vk-text-send").click(); }
  e.stopPropagation();
});

// ===== Virtual Trackpad =====
const tpArea = $("trackpad-area");
let tpLast = null, tpStart = null, tpStartTime = 0;
let tpDragMode = false;
let tpDragging = false;
let tpLastTapTime = 0;
const TAP_TAP_WINDOW = 350;

function tpMoveCursor(t) {
  if (!tpLast) return;
  const r = tpArea.getBoundingClientRect();
  const dx = (t.clientX - tpLast.x) / r.width * cursorSens;
  const dy = (t.clientY - tpLast.y) / r.height * cursorSens;
  cursorX = Math.max(0, Math.min(1, cursorX + dx));
  cursorY = Math.max(0, Math.min(1, cursorY + dy));
}

tpArea.addEventListener("touchstart", (e) => {
  e.preventDefault();
  if (viewOnly) return;
  const t = e.touches[0];
  tpLast = { x: t.clientX, y: t.clientY };
  tpStart = { x: t.clientX, y: t.clientY };
  tpStartTime = Date.now();

  if (!tpDragMode && (tpStartTime - tpLastTapTime) < TAP_TAP_WINDOW) {
    tpDragging = true;
    emitInput("mousedown", { x: cursorX, y: cursorY, btn: "left" });
    tpArea.textContent = "Dragging (tap-hold)...";
  }

  if (tpDragMode && !tpDragging) {
    tpDragging = true;
    emitInput("mousedown", { x: cursorX, y: cursorY, btn: "left" });
    tpArea.textContent = "Dragging...";
  }
}, { passive: false });

tpArea.addEventListener("touchmove", (e) => {
  e.preventDefault();
  if (viewOnly) return;
  const t = e.touches[0];
  tpMoveCursor(t);
  tpLast = { x: t.clientX, y: t.clientY };
  if (tpDragging) {
    emitInput("drag", { x: cursorX, y: cursorY });
  } else {
    emitMove({ x: cursorX, y: cursorY });
  }
}, { passive: false });

tpArea.addEventListener("touchend", (e) => {
  e.preventDefault();
  if (viewOnly) return;
  const t = e.changedTouches[0];
  const elapsed = Date.now() - tpStartTime;
  const dist = tpStart ? Math.hypot(t.clientX - tpStart.x, t.clientY - tpStart.y) : 999;

  if (tpDragging) {
    emitInput("mouseup", { x: cursorX, y: cursorY, btn: "left" });
    tpDragging = false;
    tpArea.textContent = tpDragMode
      ? "Drag mode ON \u2014 touch to grab & drag"
      : "Drag to move cursor \u00b7 Tap to click";
    if (elapsed < 250 && dist < 12 && !tpDragMode) emitInput("dblclick", {x: cursorX, y: cursorY});
  } else if (elapsed < 250 && dist < 12) {
    tpLastTapTime = Date.now();
    emitInput("click", { x: cursorX, y: cursorY, btn: "left" });
  }
  tpLast = null;
}, { passive: false });

// Mouse support on trackpad area
let tpMouseDown = false, tpMouseLast = null;
tpArea.addEventListener("mousedown", (e) => {
  if (viewOnly) return;
  tpMouseDown = true;
  tpMouseLast = { x: e.clientX, y: e.clientY };
  if (tpDragMode && !tpDragging) {
    tpDragging = true;
    emitInput("mousedown", { x: cursorX, y: cursorY, btn: "left" });
  }
});
tpArea.addEventListener("mousemove", (e) => {
  if (!tpMouseDown || !tpMouseLast) return;
  const r = tpArea.getBoundingClientRect();
  cursorX = Math.max(0, Math.min(1, cursorX + (e.clientX - tpMouseLast.x) / r.width * cursorSens));
  cursorY = Math.max(0, Math.min(1, cursorY + (e.clientY - tpMouseLast.y) / r.height * cursorSens));
  tpMouseLast = { x: e.clientX, y: e.clientY };
  if (tpDragging) {
    emitInput("drag", { x: cursorX, y: cursorY });
  } else {
    emitMove({ x: cursorX, y: cursorY });
  }
});
tpArea.addEventListener("mouseup", () => {
  tpMouseDown = false; tpMouseLast = null;
  if (tpDragging) {
    emitInput("mouseup", { x: cursorX, y: cursorY, btn: "left" });
    tpDragging = false;
  }
});

$("tp-left").onclick = () => { if (viewOnly) return; emitInput("click", { x: cursorX, y: cursorY, btn: "left" }); };
$("tp-right").onclick = () => { if (viewOnly) return; emitInput("click", { x: cursorX, y: cursorY, btn: "right" }); };
$("tp-dbl").onclick = () => { if (viewOnly) return; emitInput("dblclick", { x: cursorX, y: cursorY }); };
$("tp-drag").onclick = () => {
  if (viewOnly) return;
  tpDragMode = !tpDragMode;
  $("tp-drag").classList.toggle("active", tpDragMode);
  tpArea.textContent = tpDragMode
    ? "Drag mode ON \u2014 touch to grab & drag"
    : "Drag to move cursor \u00b7 Tap to click";
  showToast(tpDragMode ? "Drag mode ON: touch pad to grab & drag" : "Drag mode OFF");
};
$("tp-sup").onclick = () => { if (viewOnly) return; emitInput("scroll", { dy: 5 }); };
$("tp-sdn").onclick = () => { if (viewOnly) return; emitInput("scroll", { dy: -5 }); };

["tp-left","tp-right","tp-dbl","tp-drag","tp-sup","tp-sdn"].forEach(id => {
  $(id).addEventListener("touchstart", (e) => { e.preventDefault(); $(id).click(); }, { passive: false });
});

// ===== Bottom Action Bar =====
$("bb-lclick").onclick  = () => { if (viewOnly) return; emitInput("click", { x: cursorX, y: cursorY, btn: "left" }); };
$("bb-rclick").onclick  = () => { if (viewOnly) return; emitInput("click", { x: cursorX, y: cursorY, btn: "right" }); };
$("bb-dblclick").onclick= () => { if (viewOnly) return; emitInput("dblclick", { x: cursorX, y: cursorY }); };
$("bb-sup").onclick     = () => { if (viewOnly) return; emitInput("scroll", { dy: 5 }); };
$("bb-sdn").onclick     = () => { if (viewOnly) return; emitInput("scroll", { dy: -5 }); };
$("bb-left").onclick    = () => { if (viewOnly) return; emitInput("keydown", { key: "ArrowLeft", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-down").onclick    = () => { if (viewOnly) return; emitInput("keydown", { key: "ArrowDown", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-up").onclick      = () => { if (viewOnly) return; emitInput("keydown", { key: "ArrowUp", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-right").onclick   = () => { if (viewOnly) return; emitInput("keydown", { key: "ArrowRight", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-esc").onclick     = () => { if (viewOnly) return; emitInput("keydown", { key: "Escape", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-tab").onclick     = () => { if (viewOnly) return; emitInput("keydown", { key: "Tab", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-enter").onclick   = () => { if (viewOnly) return; emitInput("keydown", { key: "Enter", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-bksp").onclick    = () => { if (viewOnly) return; emitInput("keydown", { key: "Backspace", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-space").onclick   = () => { if (viewOnly) return; emitInput("keydown", { key: " ", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-del").onclick     = () => { if (viewOnly) return; emitInput("keydown", { key: "Delete", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-home").onclick    = () => { if (viewOnly) return; emitInput("keydown", { key: "Home", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-end").onclick     = () => { if (viewOnly) return; emitInput("keydown", { key: "End", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-pgup").onclick    = () => { if (viewOnly) return; emitInput("keydown", { key: "PageUp", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-pgdn").onclick    = () => { if (viewOnly) return; emitInput("keydown", { key: "PageDown", ctrl: false, shift: false, alt: false, meta: false }); };
$("bb-mclick").onclick  = () => { if (viewOnly) return; emitInput("click", { x: cursorX, y: cursorY, btn: "middle" }); };

// Text push — type text at remote cursor position
$("bb-text-push").onclick = () => {
  if (viewOnly) return;
  const inp = $("bb-text-input");
  const txt = inp.value;
  if (!txt) return;
  emitInput("type_text", { text: txt });
  inp.value = "";
  showToast("Text pushed");
  setTimeout(() => canvas.focus(), 50);
};
$("bb-text-input").addEventListener("keydown", (e) => {
  e.stopPropagation();
  if (e.key === "Enter") { e.preventDefault(); $("bb-text-push").click(); }
});

// Pan overlay arrows (visible only when zoomed in, hold-to-repeat)
const PAN_STEP = 8;
let panInterval = null;
function startPan(dx, dy) {
  canvasWrap.scrollBy(dx, dy);
  clearInterval(panInterval);
  panInterval = setInterval(() => canvasWrap.scrollBy(dx, dy), 30);
}
function stopPan() { clearInterval(panInterval); panInterval = null; }

["pan-left","pan-right","pan-up","pan-down"].forEach(id => {
  const dirs = { "pan-left":[-PAN_STEP,0], "pan-right":[PAN_STEP,0], "pan-up":[0,-PAN_STEP], "pan-down":[0,PAN_STEP] };
  const [dx,dy] = dirs[id];
  $(id).addEventListener("mousedown", (e) => { e.preventDefault(); startPan(dx, dy); });
  $(id).addEventListener("touchstart", (e) => { e.preventDefault(); startPan(dx, dy); }, { passive: false });
});
window.addEventListener("mouseup", stopPan);
window.addEventListener("touchend", stopPan);

function updatePanButtons() {
  $("pan-overlay").style.display = zoomLevel > 1.05 ? "" : "none";
}

// Touch support + refocus canvas after bottom bar clicks
document.querySelectorAll("#bottombar .bb:not(.sep)").forEach(btn => {
  btn.addEventListener("mouseup", () => setTimeout(() => canvas.focus(), 50));
});

// Convert vertical mouse wheel to horizontal scroll on the bottom bar
$("bottombar").addEventListener("wheel", (e) => {
  if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
    e.preventDefault();
    $("bottombar").scrollLeft += e.deltaY;
  }
}, { passive: false });

// ===== Bottom Bar Toggle =====
function setBBVisible(visible) {
  document.body.classList.toggle("bb-hidden", !visible);
  try { localStorage.setItem("bbHidden", visible ? "0" : "1"); } catch {}
}
$("bb-hide").addEventListener("click", () => setBBVisible(false));
$("bb-show-tab").addEventListener("click", () => setBBVisible(true));
// Restore preference
if (localStorage.getItem("bbHidden") === "1") setBBVisible(false);

// ===== Clipboard =====
socket.on("clipboard_content", async (d) => {
  $("clip-text").value = d.text || "";
  if (d.text) { try { await navigator.clipboard.writeText(d.text); } catch {} }
});
socket.on("clipboard_changed", async (d) => {
  if (d.text) { try { await navigator.clipboard.writeText(d.text); } catch {} }
});
async function syncClipboardAndPaste() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      socket.emit("clipboard_set", { text });
      await new Promise(r => setTimeout(r, 50));
    }
  } catch {}
  emitInput("hotkey", { modifiers: ["cmd"], key: "v" });
}
$("clip-get").onclick = () => { socket.emit("clipboard_get"); showToast("Fetching PC clipboard..."); };
$("clip-set").onclick = () => { socket.emit("clipboard_set", { text: $("clip-text").value }); showToast("PC clipboard set"); };
$("clip-paste").onclick = () => {
  const text = $("clip-text").value;
  if (!text) { showToast("Text area is empty — paste your phone text first"); return; }
  socket.emit("clipboard_set", { text });
  setTimeout(() => emitInput("hotkey", { modifiers: ["cmd"], key: "v" }), 150);
  showToast("Pasting on PC...");
};
$("clip-read-phone").onclick = async () => {
  try {
    const text = await navigator.clipboard.readText();
    $("clip-text").value = text;
    showToast("Phone clipboard loaded (" + text.length + " chars)");
  } catch (e) {
    showToast("Can't read clipboard — long-press the text area and paste manually");
  }
};

// ===== File Transfer =====
let currentFilePath = null;

function loadFiles(path) {
  const url = BASE + (path ? "/api/files?path=" + encodeURIComponent(path) : "/api/files");
  fetch(url).then(r => r.json()).then(data => {
    if (data.error) { showToast("Error: " + data.error); return; }
    currentFilePath = data.path;
    $("file-path").textContent = data.path.replace(/^\/Users\/[^/]+/, "~");
    $("file-up").style.display = data.parent ? "inline-block" : "none";
    $("file-up").onclick = () => loadFiles(data.parent);
    const list = $("file-list");
    list.innerHTML = "";
    data.items.forEach(item => {
      const row = document.createElement("div");
      row.className = "file-item";
      if (item.is_dir) {
        row.innerHTML = '<span class="file-icon">&#128193;</span><span class="file-name">' + esc(item.name) + '</span>';
        row.onclick = () => loadFiles(item.path);
      } else {
        row.innerHTML = '<span class="file-icon">&#128196;</span><span class="file-name">' + esc(item.name) +
          '</span><span class="file-size">' + item.size + '</span>' +
          '<a class="file-dl" href="' + BASE + '/download?path=' + encodeURIComponent(item.path) + '" target="_blank">&#8681;</a>';
      }
      list.appendChild(row);
    });
  }).catch(() => showToast("Failed to load files"));
}

$("file-upload-btn").onclick = () => {
  const files = $("file-input").files;
  if (!files.length) { showToast("Select a file first"); return; }
  let done = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    fetch(BASE + "/upload", { method: "POST", body: fd })
      .then(r => r.json())
      .then(() => { done++; if (done === files.length) { showToast(done + " file(s) uploaded"); loadFiles(currentFilePath); } })
      .catch(() => showToast("Upload failed"));
  }
};

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

// ===== Desktop mouse events =====
let isDragging = false;

function canvasCoordsFromVideo(cx, cy) {
  const el = useWebRTC ? screenContainer : canvas;
  const r = el.getBoundingClientRect();
  return { x: Math.max(0, Math.min(1, (cx - r.left) / r.width)), y: Math.max(0, Math.min(1, (cy - r.top) / r.height)) };
}

canvas.addEventListener("mousemove", (e) => {
  if (viewOnly) return;
  const c = useWebRTC ? canvasCoordsFromVideo(e.clientX, e.clientY) : canvasCoords(e.clientX, e.clientY);
  cursorX = c.x; cursorY = c.y;
  isDragging ? emitInput("drag", c) : emitMove(c);
});
canvas.addEventListener("mousedown", (e) => {
  e.preventDefault(); canvas.focus();
  if (viewOnly) return;
  const coordFn = useWebRTC ? canvasCoordsFromVideo : canvasCoords;
  if (e.detail >= 3) {
    emitInput("tripleclick", coordFn(e.clientX, e.clientY));
    return;
  }
  isDragging = true;
  emitInput("mousedown", { ...coordFn(e.clientX, e.clientY), btn: e.button === 2 ? "right" : "left" });
});
canvas.addEventListener("mouseup", (e) => {
  e.preventDefault();
  if (viewOnly) return;
  if (e.detail >= 3) return;
  isDragging = false;
  const coordFn = useWebRTC ? canvasCoordsFromVideo : canvasCoords;
  emitInput("mouseup", { ...coordFn(e.clientX, e.clientY), btn: e.button === 2 ? "right" : "left" });
});
canvas.addEventListener("click", (e) => e.preventDefault());
canvas.addEventListener("dblclick", (e) => {
  e.preventDefault();
  if (viewOnly) return;
  const coordFn = useWebRTC ? canvasCoordsFromVideo : canvasCoords;
  emitInput("dblclick", coordFn(e.clientX, e.clientY));
});
canvas.addEventListener("contextmenu", (e) => e.preventDefault());

canvasWrap.addEventListener("wheel", (e) => {
  if (e.ctrlKey || e.metaKey) { e.preventDefault(); setZoom(zoomLevel + (e.deltaY > 0 ? -ZOOM_STEP : ZOOM_STEP)); }
  else if (zoomLevel <= 1.01 && !viewOnly) { e.preventDefault(); emitInput("scroll", { dy: e.deltaY > 0 ? -3 : 3 }); }
}, { passive: false });

// ===== Touch on canvas =====
let tStartTime = 0, tStartXY = {x:0,y:0}, tMoved = false, trackpadLP = null;
let pinchActive = false, lastPinchDist = 0, pinchBaseZoom = 1;
let twoFingerMid = null;
let lastCanvasTapTime = 0;
let canvasDragging = false;

function pDist(a,b) { return Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY); }
function midpoint(a,b) { return { x:(a.clientX+b.clientX)/2, y:(a.clientY+b.clientY)/2 }; }

canvas.addEventListener("touchstart", (e) => {
  e.preventDefault();
  if (viewOnly) return;
  if (e.touches.length === 2) {
    pinchActive = true;
    lastPinchDist = pDist(e.touches[0], e.touches[1]);
    pinchBaseZoom = zoomLevel;
    twoFingerMid = midpoint(e.touches[0], e.touches[1]);
    return;
  }
  if (e.touches.length !== 1) return;
  const t = e.touches[0], c = canvasCoords(t.clientX, t.clientY);
  tStartTime = Date.now(); tStartXY = {x:t.clientX,y:t.clientY}; tMoved = false;
  if (interactionMode === "direct") {
    cursorX = c.x; cursorY = c.y;
    if (tStartTime - lastCanvasTapTime < 350) {
      canvasDragging = true;
      emitInput("mousedown", {...c, btn: "left"});
    } else {
      emitMove(c);
    }
  }
  else trackpadLP = {x:t.clientX,y:t.clientY};
}, { passive: false });

canvas.addEventListener("touchmove", (e) => {
  e.preventDefault();
  if (viewOnly) return;
  if (e.touches.length === 2 && pinchActive) {
    const d = pDist(e.touches[0], e.touches[1]);
    if (lastPinchDist > 0) setZoom(pinchBaseZoom * d / lastPinchDist);

    const mid = midpoint(e.touches[0], e.touches[1]);
    if (twoFingerMid) {
      const dx = mid.x - twoFingerMid.x;
      const dy = mid.y - twoFingerMid.y;
      if (zoomLevel > 1.01) {
        canvasWrap.scrollLeft -= dx;
        canvasWrap.scrollTop -= dy;
      } else {
        if (Math.abs(dy) > 3) {
          emitInput("scroll", { dy: dy > 0 ? -1 : 1 });
        }
      }
    }
    twoFingerMid = mid;
    return;
  }
  if (e.touches.length !== 1) return;
  tMoved = true;
  const t = e.touches[0];
  if (interactionMode === "direct") {
    const c = canvasCoords(t.clientX, t.clientY); cursorX = c.x; cursorY = c.y;
    if (canvasDragging) emitInput("drag", c); else emitMove(c);
  }
  else if (trackpadLP) {
    const r = canvas.getBoundingClientRect();
    cursorX = Math.max(0, Math.min(1, cursorX + (t.clientX-trackpadLP.x)/r.width*cursorSens));
    cursorY = Math.max(0, Math.min(1, cursorY + (t.clientY-trackpadLP.y)/r.height*cursorSens));
    emitMove({x:cursorX,y:cursorY});
    trackpadLP = {x:t.clientX,y:t.clientY};
  }
}, { passive: false });

canvas.addEventListener("touchend", (e) => {
  e.preventDefault();
  if (viewOnly) return;
  if (e.touches.length < 2) { pinchActive = false; lastPinchDist = 0; twoFingerMid = null; }
  if (e.touches.length > 0) return;
  const t = e.changedTouches[0], elapsed = Date.now()-tStartTime;
  const dist = Math.hypot(t.clientX-tStartXY.x, t.clientY-tStartXY.y);
  const c = interactionMode === "direct" ? canvasCoords(t.clientX, t.clientY) : {x:cursorX,y:cursorY};

  if (interactionMode === "direct" && canvasDragging) {
    emitInput("mouseup", {...c, btn:"left"});
    canvasDragging = false;
    if (elapsed < 250 && dist < 15) emitInput("dblclick", c);
  } else if (elapsed < 300 && dist < 15) {
    lastCanvasTapTime = Date.now();
    emitInput("click", {...c, btn:"left"});
  }
  trackpadLP = null;
}, { passive: false });

// ===== Keyboard =====
function buildModList(e) {
  const allMods = [];
  if (e.ctrlKey || mods.ctrl) allMods.push(isMacBrowser ? "ctrl" : "cmd");
  if (e.altKey || mods.alt) allMods.push("alt");
  if (e.metaKey || mods.cmd) { if (!allMods.includes("cmd")) allMods.push("cmd"); }
  if (e.shiftKey || mods.shift) allMods.push("shift");
  return allMods;
}

let _lastKeyTs = 0, _lastKeyVal = "";
function handleKeyDown(e) {
  if (!isConnected || viewOnly) return;
  if ($("settings-overlay").classList.contains("open")) return;
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (["Shift","Control","Alt","Meta","Fn","FnLock"].includes(e.key)) return;
  e.preventDefault();

  // Guard against rapid duplicate key events (OS autorepeat or ghost events)
  const now = Date.now();
  if (e.key === _lastKeyVal && now - _lastKeyTs < 80 && !e.repeat) return;
  _lastKeyTs = now; _lastKeyVal = e.key;

  // When zoomed in + Shift held, arrow keys pan the local view
  if (zoomLevel > 1.05 && e.shiftKey && ["ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(e.key)) {
    const s = 100;
    const map = { ArrowLeft: [-s,0], ArrowRight: [s,0], ArrowUp: [0,-s], ArrowDown: [0,s] };
    const [dx, dy] = map[e.key];
    canvasWrap.scrollBy({ left: dx, top: dy, behavior: "smooth" });
    return;
  }

  // Intercept paste — sync browser clipboard to remote first
  if ((e.key === 'v' || e.key === 'V') && (e.ctrlKey || e.metaKey)) {
    syncClipboardAndPaste();
    const hadSticky = mods.ctrl || mods.alt || mods.cmd || mods.shift;
    if (hadSticky) releaseMods();
    return;
  }

  const allMods = buildModList(e);
  const hadSticky = mods.ctrl || mods.alt || mods.cmd || mods.shift;

  if (allMods.length > 0) {
    emitInput("hotkey", { modifiers: allMods, key: e.key });
    // Auto-sync clipboard after copy/cut
    if (allMods.includes("cmd") && ['c','C','x','X'].includes(e.key)) {
      setTimeout(() => socket.emit("clipboard_get"), 250);
    }
    if (hadSticky) releaseMods();
  } else {
    emitInput("keydown", { key: e.key, ctrl: false, shift: false, alt: false, meta: false });
  }
}
canvas.addEventListener("keydown", handleKeyDown);

// Release any physical modifier that might stick on blur/focus change
window.addEventListener("blur", () => { releaseMods(); });
canvas.addEventListener("focus", () => { _lastKeyVal = ""; });

// NKB keydown
$("kb-input").addEventListener("keydown", (e) => {
  if (!isConnected || viewOnly) return;
  e.stopPropagation();
  if (["Shift","Control","Alt","Meta"].includes(e.key)) return;
  if (e.key === "Unidentified" || e.key === "Process") return;

  const hasActionMod = e.ctrlKey || mods.ctrl || e.altKey || mods.alt || e.metaKey || mods.cmd;
  const isSpecialKey = e.key.length > 1;

  if (!hasActionMod && !isSpecialKey) return;

  e.preventDefault();

  const allMods = buildModList(e);
  const hadSticky = mods.ctrl || mods.alt || mods.cmd || mods.shift;

  if (allMods.length > 0) {
    emitInput("hotkey", { modifiers: allMods, key: e.key });
  } else {
    emitInput("keydown", { key: e.key, ctrl: false, shift: false, alt: false, meta: false });
  }
  if (hadSticky) releaseMods();
});

// NKB input
$("kb-input").addEventListener("input", () => {
  if (viewOnly) return;
  const v = $("kb-input").value;
  if (!v) return;
  $("kb-input").value = "";

  const activeMods = getActiveMods();
  if (activeMods.length > 0) {
    const ch = v.charAt(v.length - 1);
    emitInput("hotkey", { modifiers: activeMods, key: ch });
    releaseMods();
    if (v.length > 1) emitInput("type_text", { text: v.slice(0, -1) });
  } else {
    emitInput("type_text", { text: v });
  }
});

// Mode toggle
$("btn-mode").onclick = () => {
  interactionMode = interactionMode === "direct" ? "trackpad" : "direct";
  $("btn-mode").textContent = interactionMode === "direct" ? "Direct" : "Trackpad";
  showToast(interactionMode === "direct" ? "Direct: touch = cursor" : "Trackpad: drag to move");
};

// Native keyboard toggle
let nkbActive = false;
$("btn-nkb").onclick = () => {
  nkbActive = !nkbActive;
  $("btn-nkb").classList.toggle("active", nkbActive);
  nkbActive ? $("kb-input").focus() : $("kb-input").blur();
};
$("kb-input").addEventListener("blur", () => {
  if (nkbActive) setTimeout(() => {
    if (nkbActive && document.activeElement === document.body) $("kb-input").focus();
  }, 200);
});

// ===== Audio =====
let audioBuffer = new Float32Array(0);
let audioRate = 44100, audioChannels = 1;

function initAudioPlayback(rate) {
  if (audioCtx) return;
  audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: rate || 44100 });
  const BUFSIZE = 4096;
  audioProcessor = audioCtx.createScriptProcessor(BUFSIZE, 1, 1);
  audioProcessor.onaudioprocess = (ev) => {
    const output = ev.outputBuffer.getChannelData(0);
    if (audioBuffer.length >= output.length) {
      output.set(audioBuffer.subarray(0, output.length));
      audioBuffer = audioBuffer.slice(output.length);
    } else {
      if (audioBuffer.length > 0) {
        output.set(audioBuffer);
        output.fill(0, audioBuffer.length);
        audioBuffer = new Float32Array(0);
      } else {
        output.fill(0);
      }
    }
  };
  audioProcessor.connect(audioCtx.destination);
}

socket.on("audio_data", (d) => {
  if (!audioActive) return;
  try {
    if (d.rate && d.rate !== audioRate) {
      audioRate = d.rate;
      if (audioCtx) { audioProcessor.disconnect(); audioCtx.close(); audioCtx = null; audioProcessor = null; }
      initAudioPlayback(audioRate);
      if (audioCtx.state === "suspended") audioCtx.resume();
    }
    audioChannels = d.channels || 1;

    const raw = atob(d.pcm);
    const int16 = new Int16Array(raw.length / 2);
    for (let i = 0; i < int16.length; i++) {
      int16[i] = raw.charCodeAt(i * 2) | (raw.charCodeAt(i * 2 + 1) << 8);
    }

    let mono;
    if (audioChannels >= 2) {
      const samplesPerCh = int16.length / audioChannels;
      mono = new Float32Array(samplesPerCh);
      for (let i = 0; i < samplesPerCh; i++) {
        let sum = 0;
        for (let c = 0; c < audioChannels; c++) sum += int16[i * audioChannels + c];
        mono[i] = (sum / audioChannels) / 32768;
      }
    } else {
      mono = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) mono[i] = int16[i] / 32768;
    }

    const newBuffer = new Float32Array(audioBuffer.length + mono.length);
    newBuffer.set(audioBuffer, 0);
    newBuffer.set(mono, audioBuffer.length);
    audioBuffer = newBuffer;

    const maxBuf = (audioRate || 44100) * 2;
    if (audioBuffer.length > maxBuf) {
      audioBuffer = audioBuffer.slice(audioBuffer.length - maxBuf);
    }
  } catch {}
});

socket.on("audio_status", (d) => {
  audioActive = d.active;
  $("btn-audio").classList.toggle("audio-active", d.active);
  if (d.error) showToast("Audio: " + d.error);
  else showToast(d.active ? "Audio streaming ON" : "Audio streaming OFF");
});

$("btn-audio").onclick = () => {
  if (audioActive) {
    socket.emit("audio_stop");
    if (audioCtx) { audioProcessor.disconnect(); audioCtx.close(); audioCtx = null; audioProcessor = null; }
    audioBuffer = new Float32Array(0);
  } else {
    initAudioPlayback(audioRate);
    if (audioCtx.state === "suspended") audioCtx.resume();
    const deviceSel = $("sel-audio-device");
    const deviceIdx = deviceSel.value ? parseInt(deviceSel.value) : null;
    socket.emit("audio_start", deviceIdx !== null ? { device: deviceIdx } : {});
  }
};

// Load audio devices when settings open
function loadAudioDevices() {
  fetch(BASE + "/api/audio_devices").then(r => r.json()).then(data => {
    const sel = $("sel-audio-device");
    sel.innerHTML = "";
    if (!data.available) {
      sel.innerHTML = '<option value="">Not available (install sounddevice)</option>';
      return;
    }
    $("audio-device-row").style.display = "block";
    sel.innerHTML = '<option value="">Default (auto-detect loopback)</option>';
    data.devices.forEach(d => {
      const opt = document.createElement("option");
      opt.value = d.index;
      const label = d.name + " (" + d.channels + "ch, " + d.sample_rate + "Hz)";
      opt.textContent = d.recommended ? "\u2605 " + label : label;
      if (d.is_loopback) opt.style.fontWeight = "bold";
      sel.appendChild(opt);
    });
    if (data.recommended !== null && data.recommended !== undefined) {
      sel.value = String(data.recommended);
    }
    if (data.hint) {
      const hintOpt = document.createElement("option");
      hintOpt.disabled = true;
      hintOpt.textContent = "\u2139\uFE0F " + data.hint;
      sel.appendChild(hintOpt);
    }
  }).catch(() => {});
}

// ===== Latency =====
setInterval(() => {
  if (!isConnected) return;
  if (inputDC && inputDC.readyState === "open") {
    inputDC.send(JSON.stringify({ t: "dc_ping", ts: performance.now() }));
  } else {
    socket.emit("ping_check", { t: Date.now() });
  }
}, 2000);
socket.on("pong_check", (d) => {
  if (inputDC && inputDC.readyState === "open") return;
  latencyMs = Date.now() - d.t;
  $("latency-display").textContent = latencyMs + "ms";
  $("latency-display").style.color = latencyMs < 50 ? "var(--green)" : latencyMs < 150 ? "var(--orange)" : "var(--red)";
});

// ===== Screenshot =====
$("btn-screenshot").onclick = () => {
  showToast("Capturing HD screenshot\u2026");
  const url = BASE + "/api/screenshot";
  fetch(url).then(r => {
    if (!r.ok) throw new Error("Server error");
    return r.blob();
  }).then(blob => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "screenshot_" + new Date().toISOString().replace(/[:.]/g, "-") + ".png";
    a.click();
    URL.revokeObjectURL(a.href);
    showToast("HD screenshot saved");
  }).catch(() => {
    // Fallback to client-side capture
    const link = document.createElement("a");
    link.download = "screenshot_" + new Date().toISOString().replace(/[:.]/g, "-") + ".png";
    if (useWebRTC && screenVideo.videoWidth) {
      const sc = document.createElement("canvas");
      sc.width = screenVideo.videoWidth; sc.height = screenVideo.videoHeight;
      sc.getContext("2d").drawImage(screenVideo, 0, 0);
      link.href = sc.toDataURL("image/png");
    } else {
      link.href = canvas.toDataURL("image/png");
    }
    link.click();
    showToast("Screenshot saved (fallback)");
  });
};

// ===== View-Only Mode =====
$("btn-viewonly").onclick = () => {
  viewOnly = !viewOnly;
  $("btn-viewonly").classList.toggle("active", viewOnly);
  document.body.classList.toggle("view-only", viewOnly);
  if (viewOnly) {
    canvas.style.cursor = "default";
    isDragging = false;
  } else {
    canvas.style.cursor = "none";
  }
  showToast(viewOnly ? "View-only mode — input disabled" : "Interactive mode");
};

// ===== Fullscreen =====
$("btn-fullscreen").onclick = () => {
  if (document.fullscreenElement) {
    document.exitFullscreen();
  } else {
    document.documentElement.requestFullscreen().catch(() => showToast("Fullscreen not supported"));
  }
};
document.addEventListener("fullscreenchange", () => {
  $("btn-fullscreen").classList.toggle("active", !!document.fullscreenElement);
  setTimeout(() => { recalcFit(); applyZoom(); }, 100);
});

// ===== Settings =====
$("sl-fps").oninput = () => $("val-fps").textContent = $("sl-fps").value;
$("sl-q").oninput = () => $("val-q").textContent = $("sl-q").value;
$("sl-s").oninput = () => $("val-s").textContent = ($("sl-s").value/100).toFixed(2);
$("sl-sens").oninput = () => $("val-sens").textContent = ($("sl-sens").value / 10).toFixed(1);
$("btn-settings").onclick = () => { $("settings-overlay").classList.add("open"); loadAudioDevices(); };
$("s-cancel").onclick = () => $("settings-overlay").classList.remove("open");
$("settings-overlay").onclick = (e) => { if (e.target === $("settings-overlay")) $("settings-overlay").classList.remove("open"); };
$("s-apply").onclick = () => {
  const cfg = {
    fps: +$("sl-fps").value,
    quality: +$("sl-q").value,
    scale: +$("sl-s").value / 100,
    format: $("sel-fmt").value,
  };
  socket.emit("update_settings", cfg);
  cursorSens = +$("sl-sens").value / 10;
  // Save to localStorage
  saveSettings({ fps: cfg.fps, quality: cfg.quality, scale: +$("sl-s").value, format: cfg.format, sens: cursorSens });
  $("settings-overlay").classList.remove("open");
  showToast("Settings applied & saved");
};

canvas.focus();
})();
