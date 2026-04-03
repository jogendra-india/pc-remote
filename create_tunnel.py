import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
import websockets

def _load_config():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int, default=None)
    args, _ = parser.parse_known_args()
    try:
        base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
        cfg_path = Path(sys.executable).parent / "config.json" if getattr(sys, 'frozen', False) else base / "config.json"
        cfg = json.loads(cfg_path.read_text())
    except Exception:
        cfg = {}
    port = args.port or int(cfg.get("server_port", 5050))
    server = cfg.get("tunnel_server", "")
    return port, server

LOCAL_PORT, SERVER = _load_config()
LOCAL_BASE = f"http://localhost:{LOCAL_PORT}"
LOCAL_WS_BASE = f"ws://localhost:{LOCAL_PORT}"

# Extract client_id from tunnel URL for <base href> injection
_parsed_server = urlparse(SERVER)
_client_id = parse_qs(_parsed_server.query).get("id", [""])[0]
TUNNEL_BASE_HREF = f"/tunnel/{_client_id}/" if _client_id else ""

local_ws_connections = {}


async def handle_http_request(tunnel_ws, msg):
    path = msg.get("path") or "/"
    url = f"{LOCAL_BASE}{path}"
    request_id = msg["id"]

    def do_request():
        return requests.request(msg["method"], url, data=msg.get("body", ""))

    try:
        resp = await asyncio.to_thread(do_request)
        headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() in (
                "content-type", "cache-control", "location",
                "set-cookie", "x-content-type-options",
            )
        }
        body = resp.text
        # Inject <base href> into HTML so relative paths resolve through tunnel
        ct = (headers.get("Content-Type") or headers.get("content-type") or "")
        if TUNNEL_BASE_HREF and "text/html" in ct and "<head" in body:
            base_tag = f'<base href="{TUNNEL_BASE_HREF}">'
            body = body.replace("<head>", f"<head>\n{base_tag}", 1)
        await tunnel_ws.send(json.dumps({
            "type": "http_response",
            "id": request_id,
            "status": resp.status_code,
            "headers": headers,
            "body": body,
        }))
    except Exception as exc:
        try:
            await tunnel_ws.send(json.dumps({
                "type": "http_response",
                "id": request_id,
                "status": 502,
                "headers": {"Content-Type": "text/plain"},
                "body": f"Tunnel error: {exc}",
            }))
        except Exception:
            pass


async def handle_ws_open(tunnel_ws, msg):
    ws_id = msg["wsId"]
    path = msg.get("path", "/socket.io/")
    local_url = f"{LOCAL_WS_BASE}{path}"

    try:
        local_ws = await websockets.connect(
            local_url, ping_interval=None, max_size=None,
        )

        async def forward_local_to_tunnel():
            frame_holder = [None]
            frame_ready = asyncio.Event()

            async def frame_sender():
                while True:
                    await frame_ready.wait()
                    frame_ready.clear()
                    data = frame_holder[0]
                    if data is None:
                        continue
                    frame_holder[0] = None
                    try:
                        await tunnel_ws.send(json.dumps({
                            "type": "ws_frame",
                            "wsId": ws_id,
                            "data": data,
                        }))
                    except Exception:
                        return

            sender_task = asyncio.create_task(frame_sender())
            try:
                async for message in local_ws:
                    data = message if isinstance(message, str) else message.decode("utf-8", errors="replace")
                    if len(data) > 10000:
                        frame_holder[0] = data
                        frame_ready.set()
                    else:
                        await tunnel_ws.send(json.dumps({
                            "type": "ws_frame",
                            "wsId": ws_id,
                            "data": data,
                        }))
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                sender_task.cancel()
                try:
                    await tunnel_ws.send(json.dumps({
                        "type": "ws_close",
                        "wsId": ws_id,
                    }))
                except Exception:
                    pass
                local_ws_connections.pop(ws_id, None)

        task = asyncio.create_task(forward_local_to_tunnel())
        local_ws_connections[ws_id] = (local_ws, task)
        print(f"WebSocket {ws_id} opened to {local_url}")

    except Exception as exc:
        print(f"Failed to open local WebSocket {ws_id}: {exc}")
        try:
            await tunnel_ws.send(json.dumps({
                "type": "ws_close",
                "wsId": ws_id,
            }))
        except Exception:
            pass


async def handle_ws_message(msg):
    ws_id = msg["wsId"]
    conn = local_ws_connections.get(ws_id)
    if conn:
        local_ws, _ = conn
        try:
            await local_ws.send(msg["data"])
        except Exception:
            pass


async def handle_ws_close(msg):
    ws_id = msg["wsId"]
    conn = local_ws_connections.pop(ws_id, None)
    if conn:
        local_ws, task = conn
        try:
            await local_ws.close()
        except Exception:
            pass
        task.cancel()
        print(f"WebSocket {ws_id} closed")


async def run():
    backoff = 3
    while True:
        try:
            async with websockets.connect(
                SERVER, ping_interval=30, ping_timeout=60, close_timeout=10, max_size=None,
            ) as tunnel_ws:
                print("Tunnel connected to", SERVER)
                backoff = 3  # reset on successful connection

                async for raw in tunnel_ws:
                    try:
                        msg = json.loads(raw)
                        msg_type = msg.get("type", "http_request")

                        if msg_type == "http_request":
                            asyncio.create_task(handle_http_request(tunnel_ws, msg))
                        elif msg_type == "ws_open":
                            asyncio.create_task(handle_ws_open(tunnel_ws, msg))
                        elif msg_type == "ws_frame":
                            asyncio.create_task(handle_ws_message(msg))
                        elif msg_type == "ws_close":
                            asyncio.create_task(handle_ws_close(msg))
                    except Exception as exc:
                        print(f"Error handling message: {exc}")

        except Exception as exc:
            print(f"Tunnel disconnected: {exc}, reconnecting in {backoff}s...")
            for ws_id, (local_ws, task) in list(local_ws_connections.items()):
                try:
                    await local_ws.close()
                except Exception:
                    pass
                task.cancel()
            local_ws_connections.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)  # exponential backoff, cap at 30s


asyncio.run(run())
