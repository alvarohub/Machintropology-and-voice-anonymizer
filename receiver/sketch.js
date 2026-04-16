/**
 * p5.js sketch — Real-time Speech Emotion/Prosody receiver
 *
 * Connects to the Node.js bridge via WebSocket.
 * Displays scrolling strips for prosody features, VAD bar,
 * and a large emotion label.
 */

// ── Config ───────────────────────────────────────────────────────
const WS_URL = `ws://${location.hostname || 'localhost'}:8765`;
const HISTORY = 200; // data points to keep (scrolling window)
const BAR_H = 12; // height of VAD bar
const RECONNECT_MS = 2000;

// ── State ────────────────────────────────────────────────────────
let ws = null;
let connected = false;

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
      handleOSC(msg.address, msg.args);
    } catch (_) {}
  };
}

function handleOSC(address, args) {
  if (!address) return;

  // /speech/vad → [0.0 or 1.0]
  if (address.endsWith('/vad')) {
    vadHistory.push(args[0] || 0);
    if (vadHistory.length > HISTORY) vadHistory.shift();
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

// ── p5.js ────────────────────────────────────────────────────────
function setup() {
  createCanvas(windowWidth, windowHeight);
  textFont('monospace');
  wsConnect();
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}

function draw() {
  background(26, 26, 46);

  const nCh = channelKeys.length;
  const topMargin = 100; // space for emotion label
  const botMargin = 30;
  const stripH = (height - topMargin - botMargin - BAR_H - 10) / nCh;
  const lm = 90; // left margin for labels
  const rm = 20; // right margin
  const plotW = width - lm - rm;

  // ── Emotion label (top center) ───────────────────────────────
  if (emotionLabel) {
    let ec = EMO_COLORS[emotionLabel] || [200, 200, 200];
    fill(ec[0], ec[1], ec[2]);
    noStroke();
    textAlign(CENTER, CENTER);
    textSize(min(40, width / 10));
    text(emotionLabel.toUpperCase() + '  ' + nf(emotionConf * 100, 0, 0) + '%', width / 2, 35);

    // Mini bar chart of all scores
    let barW = min(30, plotW / EMOTION_DIMS.length - 4);
    let barMaxH = 30;
    let bx = width / 2 - (EMOTION_DIMS.length * (barW + 4)) / 2;
    let by = 60;
    textSize(7);
    textAlign(CENTER, TOP);
    for (let i = 0; i < EMOTION_DIMS.length; i++) {
      let d = EMOTION_DIMS[i];
      let v = emotionScores[d] || 0;
      let c = EMO_COLORS[d] || [150, 150, 150];
      let h = v * barMaxH;
      fill(c[0], c[1], c[2], 180);
      noStroke();
      rect(bx, by + barMaxH - h, barW, h, 2);
      fill(150);
      text(d.substr(0, 3), bx + barW / 2, by + barMaxH + 2);
      bx += barW + 4;
    }
  }

  // ── VAD bar ──────────────────────────────────────────────────
  let vadY = topMargin - BAR_H - 6;
  noStroke();
  for (let i = 0; i < vadHistory.length; i++) {
    let x = lm + map(i, 0, HISTORY, 0, plotW);
    let w = plotW / HISTORY + 1;
    if (vadHistory[i] > 0.5) {
      fill(100, 255, 100, 160);
    } else {
      fill(60, 60, 80, 100);
    }
    rect(x, vadY, w, BAR_H);
  }
  fill(100, 255, 100);
  textSize(9);
  textAlign(RIGHT, CENTER);
  text('VAD', lm - 6, vadY + BAR_H / 2);

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
    textSize(10);
    textAlign(RIGHT, CENTER);
    text(ch.label, lm - 6, y0 + (stripH - 4) / 2);

    // Line plot
    if (ch.data.length > 1) {
      stroke(ch.color[0], ch.color[1], ch.color[2]);
      strokeWeight(1.5);
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

  // ── Connection indicator ─────────────────────────────────────
  fill(connected ? color(100, 255, 100) : color(255, 80, 80));
  noStroke();
  circle(width - 12, height - 15, 8);
}
