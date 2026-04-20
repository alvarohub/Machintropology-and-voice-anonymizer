"""
Combined live display — grouped boxes layout.

Layout:
  LEFT column (3 stacked boxes with coloured borders):
    ┌──── EMOTION ─────────────────────────────────────┐
    │  [Radar (polar)]  │  [9 emotion strips, stacked] │
    │  [EMO toggle]     [───── Window slider ─────]    │
    └──────────────────────────────────────────────────┘
    ┌──── PROSODY ─────────────────────────────────────┐
    │  [5 prosody strips, stacked full-width]          │
    │  [LLD toggle]     [────── LLD slider ──────]     │
    └──────────────────────────────────────────────────┘
    ┌──── VAD ─────────────────────────────────────────┐
    │  [VAD toggle]     [──── Threshold slider ────]   │
    └──────────────────────────────────────────────────┘
  RIGHT column (tall vertical panel):
    ┌─── TOOLS ──┐
    │  [LOG]     │
    │  [SAVE AS] │
    │  [OSC]     │
    │  (future)  │
    └────────────┘

Must run on the main thread (macOS / Tk requirement).
"""

from __future__ import annotations

import math
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
from matplotlib.patches import FancyBboxPatch

# Import display config from prosody module
from src.prosody import (
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
    """Animated radar + scrolling timeline with grouped control panels."""

    def __init__(
        self,
        dimensions: list[str],
        update_interval_ms: int = 100,
        smoothing: float = 0.35,
        trail_frames: int = 3,
        timeline_seconds: float = 5.0,
        chunk_duration: float = 4.0,
        vad_threshold: float = 0.3,
        vad_enabled: bool = True,
        prosody_lld_interval: float = 0.5,
    ):
        self.dimensions = dimensions
        self.n = len(dimensions)
        self._alpha = smoothing
        self._trail_max = trail_frames

        # Prosody display keys — frame-level LLD
        self.pros_keys = list(LLD_DISPLAY_KEYS)
        self.pros_labels = list(LLD_DISPLAY_LABELS)
        self.pros_colors = list(LLD_DISPLAY_COLORS)
        self.pros_ylims = list(LLD_DISPLAY_YLIMS)
        self.pros_units = list(LLD_DISPLAY_UNITS)
        self.n_pros = len(self.pros_keys)
        self.pros_defaults = {
            k: float('nan') if k in NZ_KEYS else yl[0]
            for k, yl in zip(self.pros_keys, self.pros_ylims)
        }

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
        self._pros_lld_max = 6000  # ~60s at 100 frames/s
        # Full-buffer replacement: store numpy arrays, not deques
        self._pros_lld_times: np.ndarray = np.array([])
        self._pros_lld_data: dict[str, np.ndarray] = {
            k: np.array([]) for k in self.pros_keys
        }
        self._history_times: collections.deque = collections.deque(maxlen=self._history_max)
        self._t0: Optional[float] = None

        # ── Build figure ──
        plt.style.use("dark_background")
        self.fig = plt.figure(figsize=(13, 7))
        self.fig.patch.set_facecolor("#1a1a2e")

        self._build_layout(chunk_duration, vad_threshold, vad_enabled, prosody_lld_interval)

        # Internal state
        self._audio_capture = None
        self._vad = None
        self._queue: Optional[queue.Queue] = None
        self._prosody_queue: Optional[queue.Queue] = None
        self._latest: Optional[dict] = None
        self._anim: Optional[FuncAnimation] = None
        self._interval = update_interval_ms
        self._is_speech = True

    # ═══════════════════════════════════════════════════════════════════════════
    # LAYOUT
    # ═══════════════════════════════════════════════════════════════════════════

    def _build_layout(self, chunk_duration, vad_threshold, vad_enabled, prosody_lld_interval):
        """Build the grouped-box layout with visual rectangles."""
        # Top-level: LEFT (3 group boxes) | RIGHT (tools panel)
        gs_root = gridspec.GridSpec(
            1, 2, figure=self.fig,
            width_ratios=[5, 1],
            left=0.04, right=0.98, top=0.96, bottom=0.03,
            wspace=0.04,
        )

        # Left column: 3 stacked group boxes
        gs_left = gridspec.GridSpecFromSubplotSpec(
            3, 1, subplot_spec=gs_root[0],
            height_ratios=[5.5, 3.5, 0.6],
            hspace=0.06,
        )

        self._build_emotion_group(gs_left[0], chunk_duration)
        self._build_prosody_group(gs_left[1], prosody_lld_interval)
        self._build_vad_group(gs_left[2], vad_threshold, vad_enabled)
        self._build_tools_panel(gs_root[1])
        self._draw_group_borders()

    # ── EMOTION group ──────────────────────────────────────────────────────────

    def _build_emotion_group(self, spec, chunk_duration):
        """EMOTION box: radar (left) + emotion strips (right) + controls."""
        gs = gridspec.GridSpecFromSubplotSpec(
            2, 2, subplot_spec=spec,
            height_ratios=[1, 0.055],
            width_ratios=[1, 1.8],
            hspace=0.08, wspace=0.06,
        )

        # ── Radar (top-left) ──
        self.ax_radar = self.fig.add_subplot(gs[0, 0], polar=True)
        self._setup_radar()

        # ── Emotion strips (top-right, stacked) ──
        gs_strips = gridspec.GridSpecFromSubplotSpec(
            self.n, 1, subplot_spec=gs[0, 1], hspace=0.04,
        )
        self.ax_emo_strips: list[plt.Axes] = []
        for i in range(self.n):
            share = self.ax_emo_strips[0] if i > 0 else None
            ax = self.fig.add_subplot(gs_strips[i], sharex=share)
            ax.set_facecolor("#1a1a2e")
            ax.set_ylim(0, 1)
            ax.set_yticks([])
            color = EMOTION_COLORS.get(self.dimensions[i], DEFAULT_COLOR)
            ax.set_ylabel(
                self.dimensions[i][:5].upper(), fontsize=5, color=color,
                rotation=0, labelpad=24, va="center",
            )
            ax.tick_params(left=False, labelleft=False, bottom=False, labelbottom=False)
            for spine in ax.spines.values():
                spine.set_visible(False)
            self.ax_emo_strips.append(ax)

        self.ax_emo_strips[0].set_title(
            "EMOTIONS", fontsize=7, color="#666", loc="left", pad=1,
        )

        # ── Controls row (spans both columns) ──
        gs_ctrl = gridspec.GridSpecFromSubplotSpec(
            1, 2, subplot_spec=gs[1, :],
            width_ratios=[0.15, 1.0], wspace=0.04,
        )

        self._emo_toggle_ax = self.fig.add_subplot(gs_ctrl[0])
        self._emo_toggle_ax.set_facecolor("#2a3a2a")
        self._emo_enabled = True
        self._emo_btn = Button(
            self._emo_toggle_ax, "EMO ON",
            color="#2a3a2a", hovercolor="#3a4a3a",
        )
        self._emo_btn.label.set_color("#FFD700")
        self._emo_btn.label.set_fontsize(7)
        self._emo_btn.on_clicked(self._on_emo_toggle)

        self._slider_ax = self.fig.add_subplot(gs_ctrl[1])
        self._slider_ax.set_facecolor("#2a2a3e")
        self._slider = Slider(
            self._slider_ax, "Win", 1.0, 8.0,
            valinit=chunk_duration, valstep=0.5,
            color="#4488FF",
        )
        self._slider.label.set_color("#888")
        self._slider.label.set_fontsize(7)
        self._slider.valtext.set_color("#ccc")
        self._slider.valtext.set_fontsize(7)
        self._slider.on_changed(self._on_slider_changed)

        # Track which axes belong to this group (for border drawing)
        self._emo_group_axes = (
            [self.ax_radar] + self.ax_emo_strips +
            [self._emo_toggle_ax, self._slider_ax]
        )

        # ── Line / fill artists for emotion strips ──
        self._emo_fills: list[list] = [[] for _ in self.dimensions]
        self._emo_lines: list = []
        for i, d in enumerate(self.dimensions):
            color = EMOTION_COLORS.get(d, DEFAULT_COLOR)
            (line,) = self.ax_emo_strips[i].plot(
                [], [], linewidth=0.8, color=color, alpha=0.9,
            )
            self._emo_lines.append(line)

    # ── PROSODY group ──────────────────────────────────────────────────────────

    def _build_prosody_group(self, spec, prosody_lld_interval):
        """PROSODY box: prosody strips + toggle + LLD slider."""
        gs = gridspec.GridSpecFromSubplotSpec(
            2, 1, subplot_spec=spec,
            height_ratios=[1, 0.08],
            hspace=0.08,
        )

        # ── Prosody strips (stacked, full width) ──
        gs_strips = gridspec.GridSpecFromSubplotSpec(
            self.n_pros, 1, subplot_spec=gs[0], hspace=0.04,
        )
        first_emo_ax = self.ax_emo_strips[0]
        self.ax_pros_strips: list[plt.Axes] = []
        for j in range(self.n_pros):
            ax = self.fig.add_subplot(gs_strips[j], sharex=first_emo_ax)
            ax.set_facecolor("#1a1a2e")
            ylo, yhi = self.pros_ylims[j]
            ax.set_ylim(ylo, yhi)
            ax.set_ylabel(
                self.pros_labels[j], fontsize=6, color=self.pros_colors[j],
                rotation=0, labelpad=24, va="center",
            )
            if j == 0:
                # F0 strip — Hz tick labels via semitone→Hz conversion
                hz_ticks = [85, 100, 150, 200, 300, 400, 500]
                st_ticks = [12 * math.log2(f / 27.5) for f in hz_ticks]
                visible = [(st, hz) for st, hz in zip(st_ticks, hz_ticks)
                           if ylo <= st <= yhi]
                ax.set_yticks([st for st, _ in visible])
                ax.set_yticklabels(
                    [f"{hz}" for _, hz in visible],
                    fontsize=4, color="#888",
                )
                ax.tick_params(left=True, labelleft=True, length=2, width=0.5)
                for st, _ in visible:
                    ax.axhline(st, color="#333", linewidth=0.3, zorder=0)
                ax.text(
                    1.0, 0.5, "Hz", transform=ax.transAxes,
                    fontsize=4, color="#555", ha="right", va="center",
                )
            else:
                ax.set_yticks([])
                unit = self.pros_units[j]
                ax.text(
                    1.0, 0.02, f"{ylo:g}", transform=ax.transAxes,
                    fontsize=4, color="#666", ha="right", va="bottom",
                )
                ax.text(
                    1.0, 0.95, f"{yhi:g}", transform=ax.transAxes,
                    fontsize=4, color="#666", ha="right", va="top",
                )
                ax.text(
                    1.0, 0.5, unit, transform=ax.transAxes,
                    fontsize=4, color="#555", ha="right", va="center",
                )
                ax.tick_params(left=False, labelleft=False)
            ax.tick_params(bottom=False, labelbottom=False)
            for spine in ax.spines.values():
                spine.set_visible(False)
            ax.spines["bottom"].set_visible(True)
            ax.spines["bottom"].set_color("#333")
            self.ax_pros_strips.append(ax)

        # x-axis label on last prosody strip
        self.ax_pros_strips[-1].tick_params(bottom=True, labelbottom=True, labelsize=6)
        self.ax_pros_strips[-1].set_xlabel("seconds ago", fontsize=7, color="#888")

        self.ax_pros_strips[0].set_title(
            "PROSODY", fontsize=7, color="#666", loc="left", pad=1,
        )

        # ── Controls row ──
        gs_ctrl = gridspec.GridSpecFromSubplotSpec(
            1, 2, subplot_spec=gs[1],
            width_ratios=[0.15, 1.0], wspace=0.04,
        )

        self._pros_toggle_ax = self.fig.add_subplot(gs_ctrl[0])
        self._pros_toggle_ax.set_facecolor("#2a3a2a")
        self._pros_enabled = True
        self._pros_btn = Button(
            self._pros_toggle_ax, "LLD ON",
            color="#2a3a2a", hovercolor="#3a4a3a",
        )
        self._pros_btn.label.set_color("#00DDFF")
        self._pros_btn.label.set_fontsize(7)
        self._pros_btn.on_clicked(self._on_pros_toggle)

        self._pros_slider_ax = self.fig.add_subplot(gs_ctrl[1])
        self._pros_slider_ax.set_facecolor('#2a2a3e')
        self._prosody_lld_interval = prosody_lld_interval
        self._pros_slider = Slider(
            self._pros_slider_ax, 'LLD', 0.1, 2.0,
            valinit=prosody_lld_interval, valstep=0.1,
            color='#00DDFF',
        )
        self._pros_slider.label.set_color('#888')
        self._pros_slider.label.set_fontsize(7)
        self._pros_slider.valtext.set_color('#ccc')
        self._pros_slider.valtext.set_fontsize(7)
        self._pros_slider.on_changed(self._on_pros_slider_changed)

        self._pros_group_axes = (
            self.ax_pros_strips + [self._pros_toggle_ax, self._pros_slider_ax]
        )

        # ── Line / fill artists for prosody strips ──
        self._pros_fills: list[list] = [[] for _ in self.pros_keys]
        self._pros_lines: list = []
        for j in range(self.n_pros):
            key = self.pros_keys[j]
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

    # ── VAD group ──────────────────────────────────────────────────────────────

    def _build_vad_group(self, spec, vad_threshold, vad_enabled):
        """VAD box: toggle + threshold slider (no graph)."""
        gs_ctrl = gridspec.GridSpecFromSubplotSpec(
            1, 2, subplot_spec=spec,
            width_ratios=[0.15, 1.0], wspace=0.04,
        )

        self._vad_btn_ax = self.fig.add_subplot(gs_ctrl[0])
        self._vad_btn_ax.set_facecolor("#2a3a2a" if vad_enabled else "#3a2a2a")
        self._vad_enabled = vad_enabled
        btn_label = "VAD ON" if vad_enabled else "VAD OFF"
        self._vad_btn = Button(
            self._vad_btn_ax, btn_label,
            color="#2a3a2a" if vad_enabled else "#3a2a2a",
            hovercolor="#3a4a3a",
        )
        self._vad_btn.label.set_color("#88FF88" if vad_enabled else "#FF8888")
        self._vad_btn.label.set_fontsize(7)
        self._vad_btn.on_clicked(self._on_vad_toggle)

        self._vad_slider_ax = self.fig.add_subplot(gs_ctrl[1])
        self._vad_slider_ax.set_facecolor("#2a2a3e")
        self._vad_slider = Slider(
            self._vad_slider_ax, "VAD", 0.05, 0.9,
            valinit=vad_threshold, valstep=0.05,
            color="#FF6644",
        )
        self._vad_slider.label.set_color("#888")
        self._vad_slider.label.set_fontsize(7)
        self._vad_slider.valtext.set_color("#ccc")
        self._vad_slider.valtext.set_fontsize(7)
        self._vad_slider.on_changed(self._on_vad_slider_changed)

        self._vad_group_axes = [self._vad_btn_ax, self._vad_slider_ax]

    # ── TOOLS panel ────────────────────────────────────────────────────────────

    def _build_tools_panel(self, spec):
        """RIGHT tools panel: log, save-as, OSC, future placeholders."""
        gs = gridspec.GridSpecFromSubplotSpec(
            7, 1, subplot_spec=spec,
            height_ratios=[0.4, 1, 0.3, 1, 0.3, 1, 5],
            hspace=0.05,
        )

        # Title
        ax_title = self.fig.add_subplot(gs[0])
        ax_title.set_facecolor("#1a1a2e")
        ax_title.text(
            0.5, 0.5, "TOOLS", fontsize=8, color="#888",
            fontweight="bold", transform=ax_title.transAxes,
            ha="center", va="center",
        )
        ax_title.set_xticks([]); ax_title.set_yticks([])
        for sp in ax_title.spines.values():
            sp.set_visible(False)

        # Log toggle
        self._log_btn_ax = self.fig.add_subplot(gs[1])
        self._log_enabled = False
        self._log_btn = Button(
            self._log_btn_ax, "LOG OFF",
            color="#2a2a3e", hovercolor="#3a3a4e",
        )
        self._log_btn.label.set_color("#888")
        self._log_btn.label.set_fontsize(7)
        self._log_btn.on_clicked(self._on_log_toggle)

        # Save As
        self._save_btn_ax = self.fig.add_subplot(gs[3])
        self._save_btn = Button(
            self._save_btn_ax, "SAVE AS\u2026",
            color="#2a2a3e", hovercolor="#3a3a4e",
        )
        self._save_btn.label.set_color("#888")
        self._save_btn.label.set_fontsize(7)
        self._save_btn.on_clicked(self._on_save_clicked)

        # OSC placeholder
        self._osc_btn_ax = self.fig.add_subplot(gs[5])
        self._osc_enabled = False
        self._osc_btn = Button(
            self._osc_btn_ax, "OSC OFF",
            color="#2a2a3e", hovercolor="#3a3a4e",
        )
        self._osc_btn.label.set_color("#555")
        self._osc_btn.label.set_fontsize(7)
        self._osc_btn.on_clicked(self._on_osc_toggle)

        # Spacer (future controls go here)
        ax_spacer = self.fig.add_subplot(gs[6])
        ax_spacer.set_facecolor("#1a1a2e")
        ax_spacer.set_xticks([]); ax_spacer.set_yticks([])
        for sp in ax_spacer.spines.values():
            sp.set_visible(False)

        self._tools_group_axes = [
            ax_title, self._log_btn_ax, self._save_btn_ax,
            self._osc_btn_ax, ax_spacer,
        ]

    # ── Group borders ──────────────────────────────────────────────────────────

    def _draw_group_borders(self):
        """Draw rounded-rectangle borders around each group."""
        pad = 0.008
        groups = [
            (self._emo_group_axes, "#FFD700", 0.35),
            (self._pros_group_axes, "#00DDFF", 0.35),
            (self._vad_group_axes, "#FF6644", 0.35),
            (self._tools_group_axes, "#666666", 0.25),
        ]
        for axes_list, color, alpha in groups:
            positions = [ax.get_position() for ax in axes_list]
            x0 = min(p.x0 for p in positions) - pad
            y0 = min(p.y0 for p in positions) - pad
            x1 = max(p.x1 for p in positions) + pad
            y1 = max(p.y1 for p in positions) + pad
            rect = FancyBboxPatch(
                (x0, y0), x1 - x0, y1 - y0,
                boxstyle="round,pad=0.005",
                facecolor="none", edgecolor=color,
                linewidth=1.0, alpha=alpha,
                transform=self.fig.transFigure,
                clip_on=False,
            )
            self.fig.patches.append(rect)

    # ═══════════════════════════════════════════════════════════════════════════
    # RADAR SETUP
    # ═══════════════════════════════════════════════════════════════════════════

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
            "Waiting for audio \u2026", size=13, color="white", pad=15,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # WIRING (setters)
    # ═══════════════════════════════════════════════════════════════════════════

    def set_queue(self, q: queue.Queue) -> None:
        self._queue = q

    def set_audio_capture(self, ac) -> None:
        """Wire the slider to the AudioCapture so window size is adjustable."""
        self._audio_capture = ac

    def set_vad(self, vad) -> None:
        """Wire the VAD controls to the SileroVAD instance."""
        self._vad = vad

    def set_prosody_queue(self, q: queue.Queue) -> None:
        """Wire the frame-level LLD prosody queue."""
        self._prosody_queue = q

    # ═══════════════════════════════════════════════════════════════════════════
    # SLIDER CALLBACKS
    # ═══════════════════════════════════════════════════════════════════════════

    def _on_slider_changed(self, val: float) -> None:
        if self._audio_capture is not None:
            self._audio_capture.set_chunk_duration(val)
            print(f"\n\u2699  Window \u2192 {val:.1f}s (no overlap)")

    def _on_pros_slider_changed(self, val: float) -> None:
        """Update the prosody LLD extraction interval (read by the LLD thread)."""
        self._prosody_lld_interval = val
        print(f"\n\u2699  LLD interval \u2192 {val:.2f}s")

    @property
    def prosody_lld_interval(self) -> float:
        """Current prosody LLD interval (polled by the LLD thread)."""
        return self._prosody_lld_interval

    def _on_vad_slider_changed(self, val: float) -> None:
        if self._vad is not None:
            self._vad._threshold = val

    # ═══════════════════════════════════════════════════════════════════════════
    # TOGGLE CALLBACKS
    # ═══════════════════════════════════════════════════════════════════════════

    def _on_vad_toggle(self, _event) -> None:
        self._vad_enabled = not self._vad_enabled
        if self._vad_enabled:
            self._vad_btn.label.set_text("VAD ON")
            self._vad_btn.label.set_color("#88FF88")
            self._vad_btn_ax.set_facecolor("#2a3a2a")
        else:
            self._vad_btn.label.set_text("VAD OFF")
            self._vad_btn.label.set_color("#FF8888")
            self._vad_btn_ax.set_facecolor("#3a2a2a")
        self.fig.canvas.draw_idle()

    @property
    def vad_enabled(self) -> bool:
        return self._vad_enabled

    def _on_emo_toggle(self, _event) -> None:
        self._emo_enabled = not self._emo_enabled
        if self._emo_enabled:
            self._emo_btn.label.set_text("EMO ON")
            self._emo_btn.label.set_color("#FFD700")
            self._emo_toggle_ax.set_facecolor("#2a3a2a")
        else:
            self._emo_btn.label.set_text("EMO OFF")
            self._emo_btn.label.set_color("#FF8888")
            self._emo_toggle_ax.set_facecolor("#3a2a2a")
        self.fig.canvas.draw_idle()

    @property
    def emo_enabled(self) -> bool:
        return self._emo_enabled

    def _on_pros_toggle(self, _event) -> None:
        self._pros_enabled = not self._pros_enabled
        if self._pros_enabled:
            self._pros_btn.label.set_text("LLD ON")
            self._pros_btn.label.set_color("#00DDFF")
            self._pros_toggle_ax.set_facecolor("#2a3a2a")
        else:
            self._pros_btn.label.set_text("LLD OFF")
            self._pros_btn.label.set_color("#FF8888")
            self._pros_toggle_ax.set_facecolor("#3a2a2a")
        self.fig.canvas.draw_idle()

    @property
    def pros_enabled(self) -> bool:
        return self._pros_enabled

    # ═══════════════════════════════════════════════════════════════════════════
    # TOOLS CALLBACKS
    # ═══════════════════════════════════════════════════════════════════════════

    def _on_log_toggle(self, _event) -> None:
        self._log_enabled = not self._log_enabled
        if self._log_enabled:
            self._log_btn.label.set_text("LOG ON")
            self._log_btn.label.set_color("#88FF88")
            self._log_btn_ax.set_facecolor("#2a3a2a")
        else:
            self._log_btn.label.set_text("LOG OFF")
            self._log_btn.label.set_color("#888")
            self._log_btn_ax.set_facecolor("#2a2a3e")
        self.fig.canvas.draw_idle()
        print(f"\n\U0001f4dd Logging {'enabled' if self._log_enabled else 'disabled'}")

    @property
    def log_enabled(self) -> bool:
        return self._log_enabled

    def _on_save_clicked(self, _event) -> None:
        """Placeholder for save-as functionality."""
        print("\n\U0001f4be Save As \u2014 not yet connected (coming soon)")

    def _on_osc_toggle(self, _event) -> None:
        self._osc_enabled = not self._osc_enabled
        if self._osc_enabled:
            self._osc_btn.label.set_text("OSC ON")
            self._osc_btn.label.set_color("#88FF88")
            self._osc_btn_ax.set_facecolor("#2a3a2a")
        else:
            self._osc_btn.label.set_text("OSC OFF")
            self._osc_btn.label.set_color("#555")
            self._osc_btn_ax.set_facecolor("#2a2a3e")
        self.fig.canvas.draw_idle()
        print(f"\n\U0001f50c OSC {'enabled' if self._osc_enabled else 'disabled'}")

    @property
    def osc_enabled(self) -> bool:
        return self._osc_enabled

    # ═══════════════════════════════════════════════════════════════════════════
    # ANIMATION
    # ═══════════════════════════════════════════════════════════════════════════

    def _update(self, _frame):
        new_emotion = False
        new_prosody = False

        # Drain emotion queue
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
                    new_prosody = True
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

        # Radar updates every frame (smooth transition via EMA)
        self._update_radar()

        # Timeline only when new data arrives (reduces jitter from
        # fill_between removal/recreation on every animation frame)
        if new_emotion or new_prosody:
            self._update_timeline()

        return (self._line,)

    def _ingest_prosody_lld(self, msg: dict) -> None:
        """Replace all LLD data with the latest full-buffer result.

        The prosody_lld_loop now sends the complete openSMILE output from
        the full audio buffer each cycle.  We replace (not append) so the
        display always shows one coherent openSMILE analysis — no
        cross-batch boundary artifacts.
        """
        wall_now = msg["wall_now"]
        audio_dur = msg["audio_dur"]
        frame_times = msg["times"]    # seconds from start of audio buffer
        frames = msg["frames"]

        # Convert frame times to "seconds ago" (negative = past, 0 = now)
        # A frame at time `t` in the buffer was captured `audio_dur - t` ago
        self._pros_lld_times = frame_times - audio_dur  # all negative

        for k in self.pros_keys:
            if k in frames:
                vals = frames[k].copy()
                if k in NZ_KEYS:
                    vals[vals <= 0] = np.nan
                self._pros_lld_data[k] = vals
            else:
                self._pros_lld_data[k] = np.full(len(frame_times),
                                                  self.pros_defaults[k])

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

        for ax in self.ax_emo_strips + self.ax_pros_strips:
            ax.set_xlim(-self._timeline_sec, 0)

        # Emotion strips — use relative time (seconds ago)
        if has_emo:
            times = np.array(self._history_times)
            t_now = times[-1]
            x = times - t_now   # negative values = past
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

        # Prosody strips — times are already "seconds ago" from _ingest
        if has_pros:
            px = self._pros_lld_times   # already negative (seconds ago)
            for j, key in enumerate(self.pros_keys):
                py = self._pros_lld_data[key]
                baseline = self.pros_ylims[j][0]
                self._pros_lines[j].set_data(px, py)
                for artist in self._pros_fills[j]:
                    artist.remove()
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
