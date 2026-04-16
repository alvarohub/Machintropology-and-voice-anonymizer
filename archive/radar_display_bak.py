"""
Combined live display: radar chart (left) + scrolling timeline (right).

Right panel has two grouped sections:
  • EMOTIONS — thin strips for the 9 emotion2vec dimensions
  • PROSODY  — taller strips for key eGeMAPSv02 features (F0, loudness,
    jitter, shimmer, HNR).  All 88 features still go to CSV.

The slider sits directly under the radar at the same width.

Must run on the main thread (macOS / Tk requirement).
"""

from __future__ import annotations

import queue
import collections
from typing import Optional
import numpy as np

# Force TkAgg backend — the default 'macosx' (Cocoa) backend's event loop
# interferes with PortAudio's audio callbacks, causing silent/dropped frames
# that break openSMILE F0 detection.
import matplotlib
matplotlib.use("TkAgg")

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Slider, Button

# Import display config from prosody module
from prosody import (
    DISPLAY_KEYS, DISPLAY_LABELS, DISPLAY_COLORS, DISPLAY_YLIMS,
    LLD_DISPLAY_KEYS, LLD_DISPLAY_LABELS, LLD_DISPLAY_COLORS, LLD_DISPLAY_YLIMS,
    LLD_DISPLAY_UNITS, NZ_KEYS,
)


# ---- Per-dimension fill colours ------------------------------------------------
EMOTION_COLORS: dict[str, str] = {
    "angry":     "#FF4444",
    "disgusted": "#88AA00",
    "fearful":   "#AA44FF",
    "happy":     "#FFD700",
    "neutral":   "#4488FF",
    "other":     "#888888",
    "sad":       "#5566CC",
    "surprised": "#FF8800",
    "unknown":   "#AAAAAA",
}
DEFAULT_COLOR = "#4488FF"


class RadarDisplay:
    """Animated radar + scrolling timeline with emotion and prosody strips."""

    def __init__(
        self,
        dimensions: list[str],
        update_interval_ms: int = 100,
        smoothing: float = 0.35,
        trail_frames: int = 3,
        timeline_seconds: float = 30.0,
        chunk_duration: float = 4.0,
        vad_threshold: float = 0.3,
        vad_enabled: bool = True,
        prosody_lld_interval: float = 0.5,
    ):
        self.dimensions = dimensions
        self.n = len(dimensions)
        self._alpha = smoothing
        self._trail_max = trail_frames

        # Prosody display keys — frame-level LLD (from prosody module)
        self.pros_keys = list(LLD_DISPLAY_KEYS)
        self.pros_labels = list(LLD_DISPLAY_LABELS)
        self.pros_colors = list(LLD_DISPLAY_COLORS)
        self.pros_ylims = list(LLD_DISPLAY_YLIMS)
        self.pros_units = list(LLD_DISPLAY_UNITS)
        self.n_pros = len(self.pros_keys)
        # Defaults for silence — NaN for voice-only (_nz) features, lower ylim for others
        self.pros_defaults = {
            k: float('nan') if k in NZ_KEYS else yl[0]
            for k, yl in zip(self.pros_keys, self.pros_ylims)
        }
        # For _nz features: show actual voiced values, NaN on unvoiced
        # (no sample-and-hold — cleaner MIDI-style display)

        # ── Radar geometry ──
        self.angles = np.linspace(0, 2 * np.pi, self.n, endpoint=False).tolist()
        self.angles += self.angles[:1]

        self._smoothed = {d: 0.0 for d in dimensions}
        self._trail: list[list[float]] = []

        # ── Timeline history (ring buffers) ──
        self._history_max = 300
        self._timeline_sec = timeline_seconds
        self._history: dict[str, collections.deque] = {
            d: collections.deque(maxlen=self._history_max) for d in dimensions
        }
        # LLD prosody ring buffers (many more points than emotion)
        self._pros_lld_max = 6000  # ~60s at 100 frames/s
        self._pros_lld_times: collections.deque = collections.deque(maxlen=self._pros_lld_max)
        self._pros_lld_data: dict[str, collections.deque] = {
            k: collections.deque(maxlen=self._pros_lld_max) for k in self.pros_keys
        }
        self._history_times: collections.deque = collections.deque(maxlen=self._history_max)
        self._t0: Optional[float] = None

        # ── Figure layout ──
        plt.style.use("dark_background")
        self.fig = plt.figure(figsize=(16, 8))
        self.fig.patch.set_facecolor("#1a1a2e")

        # Top-level: left column (radar + controls) | right column (strips)
        gs_top = gridspec.GridSpec(
            1, 2, figure=self.fig,
            width_ratios=[0.7, 2.2], wspace=0.15,
            left=0.06, right=0.97, top=0.95, bottom=0.04,
        )

        # ═══════════════════════════════════════════════════════════
        # LEFT COLUMN — radar + grouped control panels
        # Rows: radar | emo-hdr | emo-toggle+win slider |
        #        pros-hdr | pros-toggle+lld slider |
        #        vad-hdr | vad-toggle+threshold
        # ═══════════════════════════════════════════════════════════
        gs_left = gridspec.GridSpecFromSubplotSpec(
            10, 1, subplot_spec=gs_top[0],
            height_ratios=[1, 0.02, 0.04, 0.04, 0.02, 0.04, 0.04, 0.02, 0.04, 0.04],
            hspace=0.15,
        )

        # ── Radar ──
        self.ax_radar = self.fig.add_subplot(gs_left[0], polar=True)
        self._setup_radar()

        # ── Group 1: EMOTION ──
        self._emo_hdr_ax = self.fig.add_subplot(gs_left[1])
        self._emo_hdr_ax.set_facecolor("#1a1a2e")
        self._emo_hdr_ax.text(0.0, 0.5, "▸ EMOTION", fontsize=7, color="#FFD700",
                              fontweight="bold", transform=self._emo_hdr_ax.transAxes,
                              va="center")
        self._emo_hdr_ax.set_xticks([]); self._emo_hdr_ax.set_yticks([])
        for sp in self._emo_hdr_ax.spines.values(): sp.set_visible(False)

        # Emotion on/off toggle
        self._emo_toggle_ax = self.fig.add_subplot(gs_left[2])
        self._emo_toggle_ax.set_facecolor("#2a2a3e")
        self._emo_enabled = True
        self._emo_btn = Button(
            self._emo_toggle_ax, "EMO: ON",
            color="#2a3a2a", hovercolor="#3a4a3a",
        )
        self._emo_btn.label.set_color("#FFD700")
        self._emo_btn.label.set_fontsize(8)
        self._emo_btn.on_clicked(self._on_emo_toggle)

        # Window-size slider
        self._slider_ax = self.fig.add_subplot(gs_left[3])
        self._slider_ax.set_facecolor("#2a2a3e")
        self._slider = Slider(
            self._slider_ax, "Win", 1.0, 8.0,
            valinit=chunk_duration, valstep=0.5,
            color="#4488FF",
        )
        self._slider.label.set_color("#888")
        self._slider.label.set_fontsize(8)
        self._slider.valtext.set_color("#ccc")
        self._slider.valtext.set_fontsize(8)
        self._slider.on_changed(self._on_slider_changed)

        # ── Group 2: PROSODY ──
        self._pros_hdr_ax = self.fig.add_subplot(gs_left[4])
        self._pros_hdr_ax.set_facecolor("#1a1a2e")
        self._pros_hdr_ax.text(0.0, 0.5, "▸ PROSODY", fontsize=7, color="#00DDFF",
                               fontweight="bold", transform=self._pros_hdr_ax.transAxes,
                               va="center")
        self._pros_hdr_ax.set_xticks([]); self._pros_hdr_ax.set_yticks([])
        for sp in self._pros_hdr_ax.spines.values(): sp.set_visible(False)

        # Prosody on/off toggle
        self._pros_toggle_ax = self.fig.add_subplot(gs_left[5])
        self._pros_toggle_ax.set_facecolor("#2a2a3e")
        self._pros_enabled = True
        self._pros_btn = Button(
            self._pros_toggle_ax, "LLD: ON",
            color="#2a3a2a", hovercolor="#3a4a3a",
        )
        self._pros_btn.label.set_color("#00DDFF")
        self._pros_btn.label.set_fontsize(8)
        self._pros_btn.on_clicked(self._on_pros_toggle)

        # Prosody LLD interval slider
        self._pros_slider_ax = self.fig.add_subplot(gs_left[6])
        self._pros_slider_ax.set_facecolor('#2a2a3e')
        self._prosody_lld_interval = prosody_lld_interval
        self._pros_slider = Slider(
            self._pros_slider_ax, 'LLD', 0.1, 2.0,
            valinit=prosody_lld_interval, valstep=0.1,
            color='#00DDFF',
        )
        self._pros_slider.label.set_color('#888')
        self._pros_slider.label.set_fontsize(8)
        self._pros_slider.valtext.set_color('#ccc')
        self._pros_slider.valtext.set_fontsize(8)
        self._pros_slider.on_changed(self._on_pros_slider_changed)

        # ── Group 3: VAD ──
        self._vad_hdr_ax = self.fig.add_subplot(gs_left[7])
        self._vad_hdr_ax.set_facecolor("#1a1a2e")
        self._vad_hdr_ax.text(0.0, 0.5, "▸ VAD", fontsize=7, color="#FF6644",
                              fontweight="bold", transform=self._vad_hdr_ax.transAxes,
                              va="center")
        self._vad_hdr_ax.set_xticks([]); self._vad_hdr_ax.set_yticks([])
        for sp in self._vad_hdr_ax.spines.values(): sp.set_visible(False)

        # VAD on/off toggle button
        self._vad_btn_ax = self.fig.add_subplot(gs_left[8])
        self._vad_btn_ax.set_facecolor("#2a2a3e")
        self._vad_enabled = vad_enabled
        btn_label = "VAD: ON" if self._vad_enabled else "VAD: OFF"
        self._vad_btn = Button(
            self._vad_btn_ax, btn_label,
            color="#2a3a2a" if self._vad_enabled else "#3a2a2a",
            hovercolor="#3a4a3a",
        )
        self._vad_btn.label.set_color("#88FF88" if self._vad_enabled else "#FF8888")
        self._vad_btn.label.set_fontsize(8)
        self._vad_btn.on_clicked(self._on_vad_toggle)

        # VAD threshold slider
        self._vad_slider_ax = self.fig.add_subplot(gs_left[9])
        self._vad_slider_ax.set_facecolor("#2a2a3e")
        self._vad_slider = Slider(
            self._vad_slider_ax, "VAD", 0.05, 0.9,
            valinit=vad_threshold, valstep=0.05,
            color="#FF6644",
        )
        self._vad_slider.label.set_color("#888")
        self._vad_slider.label.set_fontsize(8)
        self._vad_slider.valtext.set_color("#ccc")
        self._vad_slider.valtext.set_fontsize(8)
        self._vad_slider.on_changed(self._on_vad_slider_changed)

        # Right column: emotion strips + gap + prosody strips
        height_ratios = [1] * self.n + [0.5] + [2] * self.n_pros
        gs_right = gridspec.GridSpecFromSubplotSpec(
            self.n + 1 + self.n_pros, 1,
            subplot_spec=gs_top[1],
            hspace=0.04,
            height_ratios=height_ratios,
        )

        # ── Emotion strip axes ──
        self.ax_emo_strips: list[plt.Axes] = []
        for i in range(self.n):
            share = self.ax_emo_strips[0] if i > 0 else None
            ax = self.fig.add_subplot(gs_right[i], sharex=share)
            ax.set_facecolor("#1a1a2e")
            ax.set_ylim(0, 1)
            ax.set_yticks([])
            color = EMOTION_COLORS.get(dimensions[i], DEFAULT_COLOR)
            ax.set_ylabel(
                dimensions[i][:5].upper(), fontsize=6, color=color,
                rotation=0, labelpad=30, va="center",
            )
            ax.tick_params(left=False, labelleft=False, bottom=False, labelbottom=False)
            for spine in ax.spines.values():
                spine.set_visible(False)
            self.ax_emo_strips.append(ax)

        # Gap row (invisible)
        ax_gap = self.fig.add_subplot(gs_right[self.n])
        ax_gap.set_visible(False)

        # ── Prosody strip axes ──
        self.ax_pros_strips: list[plt.Axes] = []
        first_ax = self.ax_emo_strips[0]
        for j in range(self.n_pros):
            ax = self.fig.add_subplot(gs_right[self.n + 1 + j], sharex=first_ax)
            ax.set_facecolor("#1a1a2e")
            ylo, yhi = self.pros_ylims[j]
            ax.set_ylim(ylo, yhi)
            ax.set_ylabel(
                self.pros_labels[j], fontsize=7, color=self.pros_colors[j],
                rotation=0, labelpad=30, va="center",
            )
            if j == 0:
                # F0 strip — show Hz ticks via semitone→Hz conversion
                # semitones = 12 * log2(f / 27.5) ⇔ f = 27.5 * 2^(st/12)
                import math
                hz_ticks = [85, 100, 150, 200, 300, 400, 500]
                st_ticks = [12 * math.log2(f / 27.5) for f in hz_ticks]
                visible = [(st, hz) for st, hz in zip(st_ticks, hz_ticks)
                           if ylo <= st <= yhi]
                ax.set_yticks([st for st, _ in visible])
                ax.set_yticklabels(
                    [f"{hz}" for _, hz in visible],
                    fontsize=5, color="#888",
                )
                ax.tick_params(left=True, labelleft=True, length=2, width=0.5)
                # Light Hz gridlines
                for st, _ in visible:
                    ax.axhline(st, color="#333", linewidth=0.3, zorder=0)
                ax.text(
                    1.0, 0.5, "Hz", transform=ax.transAxes,
                    fontsize=5, color="#555", ha="right", va="center",
                )
            else:
                ax.set_yticks([])
                # Y-scale annotations: min/max + units on right edge
                unit = self.pros_units[j]
                ax.text(
                    1.0, 0.02, f"{ylo:g}", transform=ax.transAxes,
                    fontsize=5, color="#666", ha="right", va="bottom",
                )
                ax.text(
                    1.0, 0.95, f"{yhi:g}", transform=ax.transAxes,
                    fontsize=5, color="#666", ha="right", va="top",
                )
                ax.text(
                    1.0, 0.5, unit, transform=ax.transAxes,
                    fontsize=5, color="#555", ha="right", va="center",
                )
                ax.tick_params(left=False, labelleft=False)
            ax.tick_params(bottom=False, labelbottom=False)
            for spine in ax.spines.values():
                spine.set_visible(False)
            ax.spines["bottom"].set_visible(True)
            ax.spines["bottom"].set_color("#333")
            self.ax_pros_strips.append(ax)

        # x-axis label on last prosody strip
        self.ax_pros_strips[-1].tick_params(bottom=True, labelbottom=True, labelsize=7)
        self.ax_pros_strips[-1].set_xlabel("seconds ago", fontsize=8, color="#888")

        # Section headers
        self.ax_emo_strips[0].set_title(
            "EMOTIONS", fontsize=8, color="#666", loc="left", pad=1,
        )
        self.ax_pros_strips[0].set_title(
            "PROSODY", fontsize=8, color="#666", loc="left", pad=2,
        )

        # ── Timeline line / fill artists ──
        self._emo_fills: list[list] = [[] for _ in dimensions]
        self._emo_lines: list = []
        for i, d in enumerate(dimensions):
            color = EMOTION_COLORS.get(d, DEFAULT_COLOR)
            (line,) = self.ax_emo_strips[i].plot(
                [], [], linewidth=0.6, color=color, alpha=0.8,
            )
            self._emo_lines.append(line)

        self._pros_fills: list[list] = [[] for _ in self.pros_keys]
        self._pros_lines: list = []
        for j in range(self.n_pros):
            key = self.pros_keys[j]
            # _nz features: step drawstyle for MIDI-note look, thicker line
            if key in NZ_KEYS:
                (line,) = self.ax_pros_strips[j].plot(
                    [], [], linewidth=2.0, color=self.pros_colors[j], alpha=0.9,
                    drawstyle='steps-mid',
                )
            else:
                (line,) = self.ax_pros_strips[j].plot(
                    [], [], linewidth=1.0, color=self.pros_colors[j], alpha=0.9,
                )
            self._pros_lines.append(line)

        self._audio_capture = None
        self._vad = None
        self._queue: Optional[queue.Queue] = None
        self._prosody_queue: Optional[queue.Queue] = None
        self._latest: Optional[dict] = None
        self._anim: Optional[FuncAnimation] = None
        self._interval = update_interval_ms
        self._is_speech = True

    def _setup_radar(self):
        ax = self.ax_radar
        ax.set_facecolor("#1a1a2e")
        ax.set_ylim(0, 1)
        ax.set_yticks([0.25, 0.5, 0.75, 1.0])
        ax.set_yticklabels(["", "0.5", "", "1.0"], color="#666", size=7)
        ax.set_xticks(self.angles[:-1])
        ax.set_xticklabels(
            [d[:5].upper() for d in self.dimensions], size=7, color="#cccccc",
        )
        ax.spines["polar"].set_color("#333")
        ax.grid(color="#333", linewidth=0.5)

        zeros = [0.0] * (self.n + 1)
        (self._line,) = ax.plot(
            self.angles, zeros, "o-", linewidth=2, markersize=4,
            color=DEFAULT_COLOR, zorder=10,
        )
        self._fill = ax.fill(
            self.angles, zeros, alpha=0.30, color=DEFAULT_COLOR, zorder=5,
        )
        self._trail_fills: list = []

        self._title = ax.set_title(
            "Waiting for audio …", size=13, color="white", pad=15,
        )

    def set_queue(self, q: queue.Queue) -> None:
        self._queue = q

    def set_audio_capture(self, ac) -> None:
        """Wire the slider to the AudioCapture so window size is adjustable."""
        self._audio_capture = ac

    def _on_slider_changed(self, val: float) -> None:
        if self._audio_capture is not None:
            self._audio_capture.set_chunk_duration(val)
            print(f"\n⚙  Window → {val:.1f}s (no overlap)")

    def set_vad(self, vad) -> None:
        """Wire the VAD controls to the SileroVAD instance."""
        self._vad = vad

    def set_prosody_queue(self, q: queue.Queue) -> None:
        """Wire the frame-level LLD prosody queue."""
        self._prosody_queue = q

    def _on_pros_slider_changed(self, val: float) -> None:
        """Update the prosody LLD extraction interval (read by the LLD thread)."""
        self._prosody_lld_interval = val
        print(f"\n⚙  LLD interval → {val:.2f}s")

    @property
    def prosody_lld_interval(self) -> float:
        """Current prosody LLD interval (polled by the LLD thread)."""
        return self._prosody_lld_interval

    def _on_vad_slider_changed(self, val: float) -> None:
        if self._vad is not None:
            self._vad._threshold = val

    def _on_vad_toggle(self, _event) -> None:
        self._vad_enabled = not self._vad_enabled
        if self._vad_enabled:
            self._vad_btn.label.set_text("VAD: ON")
            self._vad_btn.label.set_color("#88FF88")
            self._vad_btn_ax.set_facecolor("#2a3a2a")
        else:
            self._vad_btn.label.set_text("VAD: OFF")
            self._vad_btn.label.set_color("#FF8888")
            self._vad_btn_ax.set_facecolor("#3a2a2a")
        self.fig.canvas.draw_idle()

    @property
    def vad_enabled(self) -> bool:
        return self._vad_enabled

    def _on_emo_toggle(self, _event) -> None:
        self._emo_enabled = not self._emo_enabled
        if self._emo_enabled:
            self._emo_btn.label.set_text("EMO: ON")
            self._emo_btn.label.set_color("#FFD700")
            self._emo_toggle_ax.set_facecolor("#2a3a2a")
        else:
            self._emo_btn.label.set_text("EMO: OFF")
            self._emo_btn.label.set_color("#FF8888")
            self._emo_toggle_ax.set_facecolor("#3a2a2a")
        self.fig.canvas.draw_idle()

    @property
    def emo_enabled(self) -> bool:
        return self._emo_enabled

    def _on_pros_toggle(self, _event) -> None:
        self._pros_enabled = not self._pros_enabled
        if self._pros_enabled:
            self._pros_btn.label.set_text("LLD: ON")
            self._pros_btn.label.set_color("#00DDFF")
            self._pros_toggle_ax.set_facecolor("#2a3a2a")
        else:
            self._pros_btn.label.set_text("LLD: OFF")
            self._pros_btn.label.set_color("#FF8888")
            self._pros_toggle_ax.set_facecolor("#3a2a2a")
        self.fig.canvas.draw_idle()

    @property
    def pros_enabled(self) -> bool:
        return self._pros_enabled

    # ---- animation callback --------------------------------------------------

    def _update(self, _frame):
        # Drain emotion queue
        new_emotion = False
        if self._queue is not None:
            while True:
                try:
                    msg = self._queue.get_nowait()
                    self._latest = msg
                    new_emotion = True
                except queue.Empty:
                    break

        # Drain prosody LLD queue
        if self._prosody_queue is not None:
            while True:
                try:
                    pmsg = self._prosody_queue.get_nowait()
                    self._ingest_prosody_lld(pmsg)
                except queue.Empty:
                    break

        if self._latest is None and len(self._pros_lld_times) == 0:
            return (self._line,)

        # ── Smooth radar values every frame (visual continuity) ──
        if self._latest is not None:
            no_speech = self._latest.get("no_speech", False)
            self._is_speech = not no_speech
            if no_speech:
                for d in self.dimensions:
                    self._smoothed[d] *= 0.85
            else:
                raw_scores = self._latest["scores"]
                for d in self.dimensions:
                    new_val = raw_scores.get(d, 0.0)
                    self._smoothed[d] = (
                        self._alpha * new_val + (1 - self._alpha) * self._smoothed[d]
                    )

        # ── Append to emotion timeline only on new data ──
        if new_emotion and self._latest is not None:
            ts = self._latest.get("timestamp_ms", 0)
            if self._t0 is None:
                self._t0 = ts
            self._history_times.append((ts - self._t0) / 1000.0)
            for d in self.dimensions:
                self._history[d].append(self._smoothed[d])

        self._update_radar()
        self._update_timeline()

        return (self._line,)

    def _ingest_prosody_lld(self, msg: dict) -> None:
        """Append LLD frame data from a prosody_lld_loop message.

        For _sma3nz features (F0, jitter, shimmer, HNR), openSMILE
        returns 0 on unvoiced frames.  We use sample-and-hold: keep the
        last voiced value across gaps so the trace stays visible, and
        fade alpha in the per-frame voiced flag.
        """
        ts_ms = msg.get("timestamp_ms", 0)
        if self._t0 is None:
            self._t0 = ts_ms
        base_t = (ts_ms - self._t0) / 1000.0
        duration = msg["duration_s"]
        frame_times = msg["times"]
        frames = msg["frames"]
        n_frames = len(frame_times)
        # Diagnostic: count voiced F0 frames in this batch
        f0_key = self.pros_keys[0]  # F0semitoneFrom27.5Hz_sma3nz
        f0_vals = frames.get(f0_key, np.array([]))
        n_voiced = int(np.count_nonzero(f0_vals)) if len(f0_vals) > 0 else 0
        if n_voiced > 0:
            nz = f0_vals[f0_vals > 0]
            hz = 27.5 * 2 ** (np.median(nz) / 12)
            print(f"\n[F0-diag] {n_voiced}/{n_frames} voiced, "
                  f"median {np.median(nz):.1f}st = {hz:.0f}Hz, "
                  f"range {nz.min():.1f}-{nz.max():.1f}st")
        else:
            print(f"[F0-diag] 0/{n_frames} voiced (silence)", end="\r")
        for i in range(n_frames):
            abs_t = base_t - duration + frame_times[i]
            self._pros_lld_times.append(abs_t)
            for k in self.pros_keys:
                if k in frames:
                    val = float(frames[k][i])
                    if k in NZ_KEYS:
                        # Show voiced values as-is, unvoiced as NaN (gap)
                        if val <= 0.0:
                            val = float('nan')
                    self._pros_lld_data[k].append(val)
                else:
                    self._pros_lld_data[k].append(self.pros_defaults[k])

    def _update_radar(self):
        values = [self._smoothed[d] for d in self.dimensions]
        values_closed = values + values[:1]

        self._trail.insert(0, values_closed)
        self._trail = self._trail[: self._trail_max]

        if self._is_speech and self._latest and not self._latest.get("no_speech"):
            label = self._latest["label"]
            conf = self._latest["confidence"]
            color = EMOTION_COLORS.get(label, DEFAULT_COLOR)
            self._title.set_text(f"{label.upper()}  {conf:.0%}")
            self._title.set_color(color)
        else:
            color = "#555555"
            self._title.set_text("No speech")
            self._title.set_color("#888888")

        for artist in self._trail_fills:
            artist.remove()
        self._trail_fills.clear()

        for i, trail_vals in enumerate(reversed(self._trail)):
            opacity = 0.08 * (i + 1) / self._trail_max
            fill_artists = self.ax_radar.fill(
                self.angles, trail_vals, alpha=opacity, color=color, zorder=2 + i,
            )
            self._trail_fills.extend(fill_artists)

        self._line.set_ydata(values_closed)
        self._line.set_color(color)
        self._line.set_markerfacecolor(color)

        self._fill[0].remove()
        alpha = 0.30 if self._is_speech else 0.10
        self._fill = self.ax_radar.fill(
            self.angles, values_closed, alpha=alpha, color=color, zorder=5,
        )

    def _update_timeline(self):
        has_emo = len(self._history_times) >= 2
        has_pros = len(self._pros_lld_times) >= 2

        if not has_emo and not has_pros:
            return

        # Common time reference — latest timestamp from either source
        t_now = max(
            self._history_times[-1] if has_emo else 0,
            self._pros_lld_times[-1] if has_pros else 0,
        )

        for ax in self.ax_emo_strips + self.ax_pros_strips:
            ax.set_xlim(-self._timeline_sec, 0)

        # Emotion strips
        if has_emo:
            times = np.array(self._history_times)
            x = times - t_now
            for i, d in enumerate(self.dimensions):
                y = np.array(self._history[d])
                color = EMOTION_COLORS.get(d, DEFAULT_COLOR)
                self._emo_lines[i].set_data(x, y)
                for artist in self._emo_fills[i]:
                    artist.remove()
                fill = self.ax_emo_strips[i].fill_between(
                    x, 0, y, alpha=0.35, color=color, linewidth=0,
                )
                self._emo_fills[i] = [fill]

        # Prosody strips (frame-level LLD)
        if has_pros:
            ptimes = np.array(self._pros_lld_times)
            px = ptimes - t_now
            for j, key in enumerate(self.pros_keys):
                py = np.array(self._pros_lld_data[key])
                baseline = self.pros_ylims[j][0]
                self._pros_lines[j].set_data(px, py)
                for artist in self._pros_fills[j]:
                    artist.remove()
                # Skip fill for _nz features (F0, jitter, etc.) — MIDI-track style
                if key in NZ_KEYS:
                    self._pros_fills[j] = []
                else:
                    fill = self.ax_pros_strips[j].fill_between(
                        px, baseline, py, alpha=0.35, color=self.pros_colors[j], linewidth=0,
                    )
                    self._pros_fills[j] = [fill]

    # ---- blocking entry point ------------------------------------------------

    def run(self) -> None:
        self._anim = FuncAnimation(
            self.fig,
            self._update,
            interval=self._interval,
            blit=False,
            cache_frame_data=False,
        )
        plt.show()
