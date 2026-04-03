const http = require("http");
const WebSocket = require("ws");

// ---------------------------------------------------------------------------
// Rate limiting
// ---------------------------------------------------------------------------
const ipRequests = {};      // ip -> { count, resetAt }
const ipWsConns = {};       // ip -> number of active WebSocket connections
const RATE_WINDOW_MS = 60000;   // 1 minute
const RATE_MAX_HTTP = 120;      // max HTTP requests per window per IP
const RATE_MAX_WS = 10;         // max concurrent WebSocket connections per IP

function getIP(req) {
    return (req.headers["x-forwarded-for"] || "").split(",")[0].trim()
        || req.headers["x-real-ip"]
        || req.socket.remoteAddress;
}

function checkHttpRate(ip) {
    const now = Date.now();
    if (!ipRequests[ip] || now > ipRequests[ip].resetAt) {
        ipRequests[ip] = { count: 1, resetAt: now + RATE_WINDOW_MS };
        return true;
    }
    ipRequests[ip].count++;
    return ipRequests[ip].count <= RATE_MAX_HTTP;
}

function trackWsOpen(ip) {
    ipWsConns[ip] = (ipWsConns[ip] || 0) + 1;
}

function trackWsClose(ip) {
    if (ipWsConns[ip]) ipWsConns[ip]--;
}

// Periodically clean up stale rate-limit entries
setInterval(() => {
    const now = Date.now();
    for (const ip in ipRequests) {
        if (now > ipRequests[ip].resetAt) delete ipRequests[ip];
    }
    for (const ip in ipWsConns) {
        if (ipWsConns[ip] <= 0) delete ipWsConns[ip];
    }
}, 60000);

// ---------------------------------------------------------------------------
// Tunnel state
// ---------------------------------------------------------------------------
const clients = {};         // clientId -> tunnelWs
const pendingHttp = {};     // requestId -> { res, timer }
const browserWsSockets = {}; // wsId -> browserWs

const server = http.createServer();
const wss = new WebSocket.Server({ noServer: true });

let requestCounter = 0;
let wsCounter = 0;

// ---------------------------------------------------------------------------
// WebSocket upgrade handler
// ---------------------------------------------------------------------------
server.on("upgrade", (req, socket, head) => {
    const url = new URL(req.url, "http://localhost");
    const ip = getIP(req);

    // --- Tunnel client registration ---
    if (url.pathname === "/tunnel/register") {
        const clientId = url.searchParams.get("id");
        if (!clientId) { socket.destroy(); return; }

        wss.handleUpgrade(req, socket, head, (ws) => {
            console.log(`[tunnel] client registered: ${clientId} from ${ip}`);
            clients[clientId] = ws;
            ws._alive = true;
            ws.on("pong", () => { ws._alive = true; });

            ws.on("message", (raw) => {
                const str = raw.toString();

                // Fast path: lightweight frame protocol "WF:wsId:payload"
                if (str.charCodeAt(0) === 87 && str.charCodeAt(1) === 70 && str.charCodeAt(2) === 58) {
                    const sep = str.indexOf(":", 3);
                    if (sep > 0) {
                        const wsId = str.substring(3, sep);
                        if (browserWsSockets[wsId]) {
                            browserWsSockets[wsId].send(str.substring(sep + 1));
                        }
                    }
                    return;
                }

                // Standard JSON path for http_response, ws_close, legacy ws_frame
                try {
                    const msg = JSON.parse(str);

                    if (msg.type === "http_response" && pendingHttp[msg.id]) {
                        const { res, timer } = pendingHttp[msg.id];
                        clearTimeout(timer);
                        delete pendingHttp[msg.id];
                        const status = msg.status || 200;
                        const headers = msg.headers || {};
                        res.writeHead(status, headers);
                        res.end(msg.body || "");
                    }
                    else if (msg.type === "ws_frame" && browserWsSockets[msg.wsId]) {
                        browserWsSockets[msg.wsId].send(msg.data);
                    }
                    else if (msg.type === "ws_close" && browserWsSockets[msg.wsId]) {
                        browserWsSockets[msg.wsId].close();
                        delete browserWsSockets[msg.wsId];
                    }
                } catch (e) {
                    console.error("[tunnel] parse error:", e.message);
                }
            });

            ws.on("close", () => {
                console.log(`[tunnel] client disconnected: ${clientId}`);
                delete clients[clientId];
            });
        });
        return;
    }

    // --- Browser WebSocket (e.g. Socket.IO) ---
    const match = url.pathname.match(/^\/tunnel\/([^/]+)\/(.*)/);
    if (!match) { socket.destroy(); return; }

    const clientId = match[1];
    const tunnelWs = clients[clientId];
    if (!tunnelWs || tunnelWs.readyState !== WebSocket.OPEN) {
        socket.destroy();
        return;
    }

    // Rate-limit WebSocket connections per IP
    if ((ipWsConns[ip] || 0) >= RATE_MAX_WS) {
        console.log(`[rate-limit] WS denied for ${ip} (${ipWsConns[ip]} active)`);
        socket.destroy();
        return;
    }

    wss.handleUpgrade(req, socket, head, (browserWs) => {
        const wsId = "ws_" + (++wsCounter);
        browserWsSockets[wsId] = browserWs;
        trackWsOpen(ip);

        const subPath = "/" + match[2] + (url.search || "");

        tunnelWs.send(JSON.stringify({
            type: "ws_open", wsId, path: subPath,
        }));

        browserWs.on("message", (data) => {
            if (tunnelWs.readyState === WebSocket.OPEN) {
                tunnelWs.send(JSON.stringify({
                    type: "ws_frame", wsId, data: data.toString(),
                }));
            }
        });

        browserWs.on("close", () => {
            delete browserWsSockets[wsId];
            trackWsClose(ip);
            if (tunnelWs.readyState === WebSocket.OPEN) {
                tunnelWs.send(JSON.stringify({ type: "ws_close", wsId }));
            }
        });
    });
});

// ---------------------------------------------------------------------------
// HTTP request handler (tunneled through WebSocket)
// ---------------------------------------------------------------------------
server.on("request", (req, res) => {
    const ip = getIP(req);

    // Rate-limit HTTP requests per IP
    if (!checkHttpRate(ip)) {
        res.writeHead(429, { "Content-Type": "text/plain" });
        res.end("Too Many Requests");
        return;
    }

    const url = new URL(req.url, "http://localhost");

    // Redirect /tunnel/<clientId> → /tunnel/<clientId>/ so relative asset
    // paths (e.g. static/app.js) resolve correctly in the browser.
    const trailingSlashMatch = url.pathname.match(/^\/tunnel\/([^/]+)$/);
    if (trailingSlashMatch) {
        res.writeHead(301, { Location: url.pathname + "/" + (url.search || "") });
        res.end();
        return;
    }

    const match = url.pathname.match(/^\/tunnel\/([^/]+)(\/.*)?/);
    if (!match) {
        res.writeHead(404);
        res.end("Not found");
        return;
    }

    const clientId = match[1];
    const tunnelWs = clients[clientId];
    if (!tunnelWs || tunnelWs.readyState !== WebSocket.OPEN) {
        res.writeHead(502);
        res.end("Tunnel offline");
        return;
    }

    const subPath = (match[2] || "/") + (url.search || "");
    const reqId = "r_" + (++requestCounter);

    const timer = setTimeout(() => {
        if (pendingHttp[reqId]) {
            delete pendingHttp[reqId];
            res.writeHead(504);
            res.end("Gateway Timeout");
        }
    }, 30000);

    pendingHttp[reqId] = { res, timer };

    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
        tunnelWs.send(JSON.stringify({
            type: "http_request",
            id: reqId,
            method: req.method,
            path: subPath,
            headers: req.headers,
            body: body,
        }));
    });
});

// ---------------------------------------------------------------------------
// Heartbeat — detect dead tunnel connections
// ---------------------------------------------------------------------------
const HEARTBEAT_MS = 30000;

setInterval(() => {
    for (const [clientId, ws] of Object.entries(clients)) {
        if (ws._alive === false) {
            console.log(`[tunnel] heartbeat timeout, terminating: ${clientId}`);
            ws.terminate();
            delete clients[clientId];
            continue;
        }
        ws._alive = false;
        ws.ping();
    }
}, HEARTBEAT_MS);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
const PORT = 9000;
server.listen(PORT, () => {
    console.log(`Tunnel server running on port ${PORT}`);
    console.log(`Rate limits: ${RATE_MAX_HTTP} HTTP/min, ${RATE_MAX_WS} concurrent WS per IP`);
});