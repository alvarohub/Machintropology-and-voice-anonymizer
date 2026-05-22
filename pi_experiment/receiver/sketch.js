/**
 * p5.js sketch — Real-time Speech Emotion/Prosody receiver
 *
 * Connects to the Node.js bridge via WebSocket.
 * Displays scrolling strips for prosody features, VAD bar,
 * emotion label + score bars, sample counter, and controls.
 */

// ── Config ───────────────────────────────────────────────────────
const WS_URL = `ws://${location.hostname || 'localhost'}:8765`;
const HISTORY = 200; // data points to keep (scrolling window)
const BAR_H = 18; // height of VAD bar (was 12)
const RECONNECT_MS = 2000;
const CTRL_H = 70; // height reserved for control panel at bottom (was 50)

// ── State ────────────────────────────────────────────────────────
let ws = null;
let connected = false;
let sampleIndex = 0; // increments on every VAD message

// Prosody channels — keyed by OSC address suffix
const channels = {
  'F0semitoneFrom27.5Hz_sma3nz': { label: 'F0 (st)', color: [0, 255, 255], hi: 50, data: [] },
  Loudness_sma3: { label: 'Loudness', color: [0, 200, 80], hi: 2.5, data: [] },
  jitterLocal_sma3nz: { label: 'Jitter', color: [255, 180, 200], hi: 0.35, data: [] },
  shimmerLocaldB_sma3nz: { label: 'Shimmer (dB)', color: [255, 165, 0], hi: 30, data: [] },
  HNRdBACF_sma3nz: { label: 'HNR (dB)', color: [200, 130, 255], hi: 15, data: [] },
};
const channelKeys = Object.keys(channels);

let vadHistory = [];
let emotionLabel = '';
let emotionConf = 0;
let emotionScores = {};
const EMOTION_DIMS = ['angry', 'disgusted', 'fearful', 'happy', 'neutral', 'other', 'sad', 'surprised', 'unknown'];
const EMO_COLORS = {
  angry: [255, 60, 60],
  disgusted: [120, 200, 60],
  fearful: [180, 100, 255],
  happy: [255, 220, 40],
  neutral: [160, 160, 160],
  other: [100, 100, 100],
  sad: [80, 140, 255],
  surprised: [255, 140, 200],
  unknown: [80, 80, 80],
};

// Controls state
let oscRunning = false;
let logRunning = false;

// ── WebSocket ────────────────────────────────────────────────────
function wsConnect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    connected = true;
    document.getElementById('status').textContent = 'connected ✓';
    document.getElementById('status').style.color = '#6f6';
  };

  ws.onclose = () => {
    connected = false;
    document.getElementById('status').textContent = 'disconnected — retrying…';
    document.getElementById('status').style.color = '#f66';
    setTimeout(wsConnect, RECONNECT_MS);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      // OSC data forwarded from bridge
      if (msg.address) {
        handleOSC(msg.address, msg.args);
      }
      // Control state feedback from bridge
      if (msg.type === 'state') {
        if (msg.osc !== undefined) oscRunning = msg.osc;
        if (msg.log !== undefined) logRunning = msg.log;
      }
    } catch (_) {}
  };
}

function sendCmd(cmd) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'cmd', cmd }));
  }
}

function handleOSC(address, args) {
  if (!address) return;

  // /speech/vad → [0.0 or 1.0]
  if (address.endsWith('/vad')) {
    vadHistory.push(args[0] || 0);
    if (vadHistory.length > HISTORY) vadHistory.shift();
    sampleIndex++;
    return;
  }

  // /speech/emo/label → [label, confidence]
  if (address.endsWith('/emo/label')) {
    emotionLabel = args[0] || '';
    emotionConf = args[1] || 0;
    return;
  }

  // /speech/emo/scores → [angry, disgusted, …]
  if (address.endsWith('/emo/scores')) {
    for (let i = 0; i < EMOTION_DIMS.length && i < args.length; i++) {
      emotionScores[EMOTION_DIMS[i]] = args[i];
    }
    return;
  }

  // Prosody features — /speech/<key>
  for (const key of channelKeys) {
    if (address.endsWith('/' + key)) {
      channels[key].data.push(args[0] || 0);
      if (channels[key].data.length > HISTORY) channels[key].data.shift();
      return;
    }
  }
}

// ── Button helper ────────────────────────────────────────────────
function drawBtn(x, y, w, h, label, bg, isHover) {
  let c = isHover ? bg.map((v) => min(255, v + 30)) : bg;
  fill(c[0], c[1], c[2]);
  noStroke();
  rect(x, y, w, h, 5);
  fill(255);
  textAlign(CENTER, CENTER);
  textSize(14);
  text(label, x + w / 2, y + h / 2);
}

function inRect(mx, my, x, y, w, h) {
  return mx >= x && mx <= x + w && my >= y && my <= y + h;
}

// Button layout (computed each frame)
function btnLayout() {
  let y = height - CTRL_H + 16;
  let bw = 130,
    bh = 38,
    gap = 14,
    bx = 135;
  return [
    {
      x: bx,
      y,
      w: bw,
      h: bh,
      label: oscRunning ? '■ STOP OSC' : '● OSC',
      bg: oscRunning ? [180, 120, 0] : [60, 60, 80],
      action: () => {
        sendCmd(oscRunning ? 'osc_stop' : 'osc_start');
        oscRunning = !oscRunning;
      },
    },
    {
      x: bx + bw + gap,
      y,
      w: bw,
      h: bh,
      label: logRunning ? '■ STOP LOG' : '● LOG',
      bg: logRunning ? [180, 50, 50] : [60, 60, 80],
      action: () => {
        sendCmd(logRunning ? 'log_stop' : 'log_start');
        logRunning = !logRunning;
      },
    },
  ];
}

// ── p5.js ────────────────────────────────────────────────────────
function setup() {
  createCanvas(1080, 780);
  textFont('monospace');
  wsConnect();
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}

function mousePressed() {
  for (let b of btnLayout()) {
    if (inRect(mouseX, mouseY, b.x, b.y, b.w, b.h)) {
      b.action();
      return;
    }
  }
}

function draw() {
  background(26, 26, 46);

  const nCh = channelKeys.length;
  const topMargin = 195; // extra room for emotion bars (was 130)
  const botMargin = CTRL_H + 15;
  const stripH = (height - topMargin - botMargin - BAR_H - 15) / nCh;
  const lm = 135; // left margin for labels (was 90)
  const rm = 30; // right margin (was 20)
  const plotW = width - lm - rm;

  // ── Sample counter (top-left) ────────────────────────────────
  fill(100);
  noStroke();
  textAlign(LEFT, TOP);
  textSize(13);
  text(`sample: ${sampleIndex}`, 12, 12);

  // ── Emotion label (top center) ───────────────────────────────
  if (emotionLabel) {
    let ec = EMO_COLORS[emotionLabel] || [200, 200, 200];
    fill(ec[0], ec[1], ec[2]);
    noStroke();
    textAlign(CENTER, CENTER);
    textSize(min(42, width / 12));
    text(emotionLabel.toUpperCase() + '  ' + nf(emotionConf * 100, 0, 0) + '%', width / 2, 36);

    // Bar chart spanning full width
    let emoGap = 6;
    let totalGaps = (EMOTION_DIMS.length - 1) * emoGap;
    let barW = (plotW - totalGaps) / EMOTION_DIMS.length;
    let barMaxH = 68;
    let bx = lm;
    let by = 70;
    textSize(11);
    textAlign(CENTER, TOP);
    for (let i = 0; i < EMOTION_DIMS.length; i++) {
      let d = EMOTION_DIMS[i];
      let v = emotionScores[d] || 0;
      let c = EMO_COLORS[d] || [150, 150, 150];
      let h = v * barMaxH;
      fill(c[0], c[1], c[2], 180);
      noStroke();
      rect(bx, by + barMaxH - h, barW, h, 3);
      fill(150);
      text(d.substr(0, 4), bx + barW / 2, by + barMaxH + 3);
      bx += barW + emoGap;
    }
  }

  // ── VAD bar (tri-state: -1 = VAD off, 0 = silent, 1 = speech) ──
  let vadY = topMargin - BAR_H - 6;
  noStroke();

  // Always draw a background so the strip is visible even with no data
  fill(40, 40, 60);
  rect(lm, vadY, plotW, BAR_H, 3);

  // Draw VAD history on top
  for (let i = 0; i < vadHistory.length; i++) {
    let x = lm + map(i, 0, HISTORY, 0, plotW);
    let w = plotW / HISTORY + 1;
    let v = vadHistory[i];
    if (v > 0.5) {
      fill(100, 255, 100, 180); // speech detected — green
    } else if (v < -0.5) {
      fill(80, 200, 200, 120); // VAD OFF (gate open) — muted cyan
    } else {
      fill(60, 60, 80, 120); // silence — dark
    }
    rect(x, vadY, w, BAR_H);
  }

  // VAD label — always visible, shows current state
  let lastVad = vadHistory.length > 0 ? vadHistory[vadHistory.length - 1] : 0;
  let vadStateText, vadLabelColor;
  if (vadHistory.length === 0) {
    vadStateText = 'VAD';
    vadLabelColor = [120, 120, 120]; // grey — no data yet
  } else if (lastVad < -0.5) {
    vadStateText = 'VAD OFF';
    vadLabelColor = [80, 200, 200]; // cyan — gate always open
  } else if (lastVad > 0.5) {
    vadStateText = 'VAD ON';
    vadLabelColor = [100, 255, 100]; // green — speech
  } else {
    vadStateText = 'VAD ON';
    vadLabelColor = [140, 140, 160]; // dim — silence (VAD active but quiet)
  }
  fill(vadLabelColor[0], vadLabelColor[1], vadLabelColor[2]);
  textSize(14);
  textAlign(RIGHT, CENTER);
  text(vadStateText, lm - 8, vadY + BAR_H / 2);

  // ── Prosody strips ──────────────────────────────────────────
  for (let ci = 0; ci < nCh; ci++) {
    let key = channelKeys[ci];
    let ch = channels[key];
    let y0 = topMargin + ci * stripH;
    let y1 = y0 + stripH - 4;

    // Background
    fill(30, 30, 50);
    noStroke();
    rect(lm, y0, plotW, stripH - 4, 3);

    // Label
    fill(ch.color[0], ch.color[1], ch.color[2]);
    textSize(14);
    textAlign(RIGHT, CENTER);
    text(ch.label, lm - 8, y0 + (stripH - 4) / 2);

    // Determine if this is F0 (scatter/segment style for unvoiced gaps)
    let isF0 = key === 'F0semitoneFrom27.5Hz_sma3nz';

    // Line plot
    if (ch.data.length > 1) {
      if (isF0) {
        // F0: draw only segments where value > 0 (skip unvoiced)
        stroke(ch.color[0], ch.color[1], ch.color[2]);
        strokeWeight(2);
        noFill();
        let inSegment = false;
        for (let i = 0; i < ch.data.length; i++) {
          let x = lm + map(i, 0, HISTORY, 0, plotW);
          let raw = ch.data[i];
          let v = constrain(raw / ch.hi, 0, 1);
          let y = map(v, 0, 1, y1, y0);
          if (raw > 0.5) {
            if (!inSegment) {
              beginShape();
              inSegment = true;
            }
            vertex(x, y);
          } else {
            if (inSegment) {
              endShape();
              inSegment = false;
            }
          }
        }
        if (inSegment) endShape();
      } else {
        // Other features: continuous line + fill
        stroke(ch.color[0], ch.color[1], ch.color[2]);
        strokeWeight(2);
        noFill();
        beginShape();
        for (let i = 0; i < ch.data.length; i++) {
          let x = lm + map(i, 0, HISTORY, 0, plotW);
          let v = constrain(ch.data[i] / ch.hi, 0, 1);
          let y = map(v, 0, 1, y1, y0);
          vertex(x, y);
        }
        endShape();

        // Filled area
        fill(ch.color[0], ch.color[1], ch.color[2], 40);
        noStroke();
        beginShape();
        vertex(lm + map(0, 0, HISTORY, 0, plotW), y1);
        for (let i = 0; i < ch.data.length; i++) {
          let x = lm + map(i, 0, HISTORY, 0, plotW);
          let v = constrain(ch.data[i] / ch.hi, 0, 1);
          let y = map(v, 0, 1, y1, y0);
          vertex(x, y);
        }
        vertex(lm + map(ch.data.length - 1, 0, HISTORY, 0, plotW), y1);
        endShape(CLOSE);
      }
    }
  }

  // ── Control panel ────────────────────────────────────────────
  stroke(50);
  strokeWeight(1);
  line(0, height - CTRL_H, width, height - CTRL_H);
  noStroke();

  let btns = btnLayout();
  for (let b of btns) {
    let hover = inRect(mouseX, mouseY, b.x, b.y, b.w, b.h);
    drawBtn(b.x, b.y, b.w, b.h, b.label, b.bg, hover);
  }

  // ── Connection indicator (labeled) ──────────────────────────
  let connColor = connected ? [100, 255, 100] : [255, 80, 80];
  let connText = connected ? 'connected' : 'disconnected';
  fill(connColor[0], connColor[1], connColor[2]);
  noStroke();
  circle(width - 16, height - CTRL_H / 2, 12);
  textSize(13);
  textAlign(RIGHT, CENTER);
  text(connText, width - 28, height - CTRL_H / 2);
}
