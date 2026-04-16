/**
 * OSC → WebSocket bridge
 *
 * Receives UDP OSC messages from the Python app and forwards them
 * as JSON over WebSocket to the p5.js browser client.
 *
 * Usage:
 *   cd receiver && npm install && npm start
 *
 * Env vars:
 *   OSC_PORT    – UDP port to listen on (default 9000)
 *   WS_PORT     – WebSocket port for browser (default 8765)
 *   HTTP_PORT   – HTTP port for serving index.html (default 3000)
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const osc = require('osc');
const WebSocket = require('ws');

const OSC_PORT = parseInt(process.env.OSC_PORT || '9000', 10);
const WS_PORT = parseInt(process.env.WS_PORT || '8765', 10);
const HTTP_PORT = parseInt(process.env.HTTP_PORT || '3000', 10);

// ── HTTP server (serves index.html + sketch.js) ──────────────────
const MIME = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
};

const httpServer = http.createServer((req, res) => {
  let filePath = req.url === '/' ? '/index.html' : req.url;
  filePath = path.join(__dirname, filePath);
  const ext = path.extname(filePath);
  const contentType = MIME[ext] || 'application/octet-stream';

  // Prevent path traversal
  const resolved = path.resolve(filePath);
  if (!resolved.startsWith(path.resolve(__dirname))) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  fs.readFile(resolved, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end('Not found');
      return;
    }
    res.writeHead(200, { 'Content-Type': contentType });
    res.end(data);
  });
});

httpServer.listen(HTTP_PORT, () => {
  console.log(`[HTTP] http://localhost:${HTTP_PORT}`);
});

// ── WebSocket server ─────────────────────────────────────────────
const wss = new WebSocket.Server({ port: WS_PORT });
const clients = new Set();

wss.on('connection', (ws) => {
  clients.add(ws);
  console.log(`[WS] client connected (${clients.size} total)`);
  ws.on('close', () => {
    clients.delete(ws);
    console.log(`[WS] client disconnected (${clients.size} total)`);
  });
});

function broadcast(obj) {
  const msg = JSON.stringify(obj);
  for (const ws of clients) {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(msg);
    }
  }
}

console.log(`[WS] ws://localhost:${WS_PORT}`);

// ── OSC UDP receiver ─────────────────────────────────────────────
const udpPort = new osc.UDPPort({
  localAddress: '0.0.0.0',
  localPort: OSC_PORT,
  metadata: true,
});

udpPort.on('message', (oscMsg) => {
  const addr = oscMsg.address;
  const args = (oscMsg.args || []).map((a) => a.value);

  // Forward as { address, args }
  broadcast({ address: addr, args });
});

udpPort.on('error', (err) => {
  console.error('[OSC error]', err);
});

udpPort.open();
console.log(`[OSC] listening on UDP :${OSC_PORT}`);
console.log('Ready. Open http://localhost:3000 in your browser.');
