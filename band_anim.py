"""Small, event-driven animations for the taskbar band.

Nothing moves while the numbers sit still. When a new reading arrives:
  - the battery fill eases to the new length and the % counts along with it
    (a reset back to ~0% is the same tween, just longer - it drains);
  - a soft shine sweeps across the fill once the % has changed;
  - a threshold crossing cross-fades the fill colour instead of snapping.
The only continuous motion is a slow "breathing" of the fill while usage is in
the critical range.

This module is pure timing/state (no drawing, no Win32): ``BandAnimator.frame``
returns per-row values that ``taskbar_band.render_band`` draws.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

RGB = Tuple[int, int, int]

TWEEN_FPS = 30
PULSE_FPS = 10
TWEEN_BASE = 0.4     # seconds for a small change ...
TWEEN_PER_FULL = 0.8  # ... plus this much for a 0 -> 100% swing
COLOR_SECS = 0.5
GLINT_SECS = 0.7
PULSE_PERIOD = 2.4
PULSE_DEPTH = 0.4    # crit fill dims to 60% at the bottom of a breath


def _ease_out(x: float) -> float:
    return 1 - (1 - x) ** 3


def _ease_in_out(x: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * x)


@dataclass
class RowFrame:
    pct: Optional[float]    # value to draw right now (mid-tween)
    color: RGB              # fill colour right now (mid-crossfade)
    glint: Optional[float]  # 0..1 progress of the shine sweep, None = no shine
    pulse: float            # fill opacity factor: 1.0, lower while breathing


class _Row:
    def __init__(self, pct: Optional[float], color: RGB) -> None:
        self.target = pct
        self.p0 = self.p1 = pct
        self.t0, self.dur = 0.0, 0.0
        self.c0 = self.c1 = color
        self.ct0, self.cdur = 0.0, 0.0
        self.glint_t0: Optional[float] = None

    def pct_at(self, now: float) -> Optional[float]:
        if self.p0 is None or self.p1 is None or self.dur <= 0:
            return self.p1
        x = min(1.0, max(0.0, (now - self.t0) / self.dur))
        return self.p0 + (self.p1 - self.p0) * _ease_out(x)

    def color_at(self, now: float) -> RGB:
        if self.cdur <= 0:
            return self.c1
        e = _ease_in_out(min(1.0, max(0.0, (now - self.ct0) / self.cdur)))
        return tuple(round(a + (b - a) * e) for a, b in zip(self.c0, self.c1))

    def glint_at(self, now: float) -> Optional[float]:
        if self.glint_t0 is None:
            return None
        x = (now - self.glint_t0) / GLINT_SECS
        return x if 0 <= x < 1 else None

    def busy(self, now: float) -> bool:
        return (now < self.t0 + self.dur or now < self.ct0 + self.cdur
                or (self.glint_t0 is not None and now < self.glint_t0 + GLINT_SECS))


class BandAnimator:
    """Per-row animation state. Call ``retarget`` with each new reading and
    ``frame`` for what to draw; ``next_delay`` says when the next frame is due."""

    def __init__(self, color_of: Callable[[Optional[float]], RGB], crit: float,
                 enabled: bool = True, pulse: bool = True) -> None:
        self.color_of = color_of
        self.crit = crit
        self.enabled = enabled
        self.pulse = pulse
        self.rows: List[_Row] = []

    def retarget(self, pcts: Sequence[Optional[float]], now: float) -> None:
        if len(self.rows) != len(pcts):
            self.rows = [_Row(p, self.color_of(p)) for p in pcts]
            return
        for i, pct in enumerate(pcts):
            row = self.rows[i]
            if pct == row.target:
                continue
            color = self.color_of(pct)
            if not self.enabled or pct is None or row.target is None:
                self.rows[i] = _Row(pct, color)  # unknown <-> known: just switch
                continue
            cur = row.pct_at(now)
            row.p0, row.p1, row.t0 = cur, pct, now
            row.dur = TWEEN_BASE + TWEEN_PER_FULL * min(1.0, abs(pct - cur))
            if round(pct * 100) != round(row.target * 100) and pct >= 0.01:
                row.glint_t0 = now + row.dur * 0.5  # shine as the bar settles
            if color != row.c1:
                # Fade alongside the bar, so a long drain does not turn green early.
                row.c0, row.c1 = row.color_at(now), color
                row.ct0, row.cdur = now, max(COLOR_SECS, row.dur)
            row.target = pct

    def _pulsing(self, pulse_ok: bool) -> bool:
        return self.pulse and pulse_ok and any(
            r.target is not None and r.target >= self.crit for r in self.rows)

    def busy(self, now: float) -> bool:
        return any(r.busy(now) for r in self.rows)

    def active(self, now: float, pulse_ok: bool = True) -> bool:
        return self.busy(now) or self._pulsing(pulse_ok)

    def next_delay(self, now: float, pulse_ok: bool = True) -> Optional[float]:
        """Seconds until the next frame, or None when nothing is moving."""
        if self.busy(now):
            return 1 / TWEEN_FPS
        if self._pulsing(pulse_ok):
            return 1 / PULSE_FPS
        return None

    def breath(self, now: float) -> float:
        """Opacity of a breathing fill: 1.0 down to 1 - PULSE_DEPTH and back."""
        return 1 - PULSE_DEPTH * (0.5 - 0.5 * math.cos(2 * math.pi * now / PULSE_PERIOD))

    def frame(self, now: float, pulse_ok: bool = True,
              breath: Optional[float] = None) -> List[RowFrame]:
        """What to draw at ``now``. ``breath`` overrides the breathing opacity
        (used to pre-render the two ends of a breath)."""
        if breath is None:
            breath = self.breath(now) if self._pulsing(pulse_ok) else 1.0
        return [RowFrame(r.pct_at(now), r.color_at(now), r.glint_at(now),
                         breath if r.target is not None and r.target >= self.crit else 1.0)
                for r in self.rows]
