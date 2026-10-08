"""Small animations for the taskbar band.

When a new reading arrives:
  - the battery fill eases to the new length and the % counts along with it
    (a reset back to ~0% is the same tween, just longer - it drains);
  - a soft shine sweeps across the fill once the % has changed;
  - a threshold crossing cross-fades the fill colour instead of snapping.
Idle motion (not while the data is stale):
  - the same shine sweeps across every few seconds, 5h first, then 7d;
  - tiny four-pointed sparkles twinkle on the fill now and then (fewer on a
    short bar);
  - a slow "breathing" of the fill while usage is in the critical range.
Between sweeps and twinkles nothing is redrawn.

This module is pure timing/state (no drawing, no Win32): ``BandAnimator.frame``
returns per-row values that ``taskbar_band.render_band`` draws.
"""
from __future__ import annotations

import functools
import math
import random
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
SHIMMER_PERIOD = 5.0  # an idle shine every this many seconds ...
SHIMMER_STAGGER = 0.15  # ... reaching the next row this much later
SPARKLE_FPS = 15
SPARKLE_SLOTS = 3     # per row; slot k only once the fill reaches k/SLOTS
SPARKLE_PERIOD = 3.0  # each slot twinkles about once per period ...
SPARKLE_SKIP = 0.3    # ... but sits out this share of them, so it never looks regular
SPARKLE_LIFE = 0.9
FILL_MIN = 0.03       # no idle shine / sparkles on an (almost) empty battery


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


@dataclass
class Sparkle:
    row: int
    x: float     # 0..1 along the fill as drawn right now
    y: float     # 0..1 across it
    size: float  # 0..1: pops up quickly, then fades out


def _twinkle(x: float) -> float:
    return _ease_out(x / 0.3) if x < 0.3 else _ease_in_out((1 - x) / 0.7)


@functools.lru_cache(maxsize=256)
def _sparkle_birth(row: int, slot: int, cycle: int) -> Optional[Tuple[float, float, float]]:
    """(start within the cycle, x, y) of one slot's sparkle, or None when it sits
    this cycle out. Seeded by the cycle so every frame agrees on it."""
    rng = random.Random((row * SPARKLE_SLOTS + slot) * 1_000_003 + cycle)
    if rng.random() < SPARKLE_SKIP:
        return None
    return (rng.uniform(0, SPARKLE_PERIOD - SPARKLE_LIFE),
            rng.uniform(0.12, 0.88), rng.uniform(0.2, 0.8))


def _sparkle_at(row: int, slot: int, cycle: int) -> Optional[Tuple[float, float, float]]:
    """(absolute start time, x, y) of a slot's sparkle in ``cycle``. Slots are
    phase-shifted so they never fire in step."""
    b = _sparkle_birth(row, slot, cycle)
    if b is None:
        return None
    off = (slot / SPARKLE_SLOTS + row * 0.37) * SPARKLE_PERIOD
    return off + cycle * SPARKLE_PERIOD + b[0], b[1], b[2]


def _sparkle_cycle(row: int, slot: int, now: float) -> int:
    off = (slot / SPARKLE_SLOTS + row * 0.37) * SPARKLE_PERIOD
    return math.floor((now - off) / SPARKLE_PERIOD)


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
                 enabled: bool = True, pulse: bool = True, sparkle: bool = True,
                 shimmer: bool = True) -> None:
        self.color_of = color_of
        self.crit = crit
        self.enabled = enabled
        self.pulse = pulse
        self.sparkle = sparkle
        self.shimmer = shimmer
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

    def pulsing(self, pulse_ok: bool) -> bool:
        return self.pulse and pulse_ok and any(
            r.target is not None and r.target >= self.crit for r in self.rows)

    def _filled(self) -> List[int]:
        return [i for i, r in enumerate(self.rows) if r.target is not None and r.target >= FILL_MIN]

    def _shimmer_rows(self, live: bool) -> List[int]:
        return self._filled() if self.shimmer and live else []

    def _shimmer_at(self, row: int, now: float) -> Optional[float]:
        """0..1 progress of the idle shine on ``row``, None between sweeps."""
        x = ((now - row * SHIMMER_STAGGER) % SHIMMER_PERIOD) / GLINT_SECS
        return x if x < 1 else None

    def _sparkle_slots(self, live: bool) -> List[Tuple[int, int]]:
        """(row, slot) pairs that may twinkle: more of them on a longer fill."""
        if not (self.sparkle and live):
            return []
        return [(i, k) for i in self._filled()
                for k in range(SPARKLE_SLOTS) if self.rows[i].target >= k / SPARKLE_SLOTS]

    def busy(self, now: float) -> bool:
        return any(r.busy(now) for r in self.rows)

    def active(self, now: float, pulse_ok: bool = True, fill_ok: bool = True) -> bool:
        """``fill_ok`` False: there is no battery drawn (mini layout), so no
        shine or sparkles."""
        live = pulse_ok and fill_ok
        return (self.busy(now) or self.pulsing(pulse_ok)
                or bool(self._shimmer_rows(live)) or bool(self._sparkle_slots(live)))

    def next_delay(self, now: float, pulse_ok: bool = True,
                   fill_ok: bool = True) -> Optional[float]:
        """Seconds until the next frame, or None when nothing is moving.
        Never more than 1s, so the caller's once-a-second housekeeping stays on time."""
        if self.busy(now):
            return 1 / TWEEN_FPS
        delays = []
        if self.pulsing(pulse_ok):
            delays.append(1 / PULSE_FPS)
        rows = self._shimmer_rows(pulse_ok and fill_ok)
        if rows:
            delays.append(self._next_shimmer(now, rows))
        slots = self._sparkle_slots(pulse_ok and fill_ok)
        if slots:
            delays.append(self._next_sparkle(now, slots))
        return min(delays) if delays else None

    def _next_shimmer(self, now: float, rows: List[int]) -> float:
        soonest = 1.0
        for row in rows:
            phase = (now - row * SHIMMER_STAGGER) % SHIMMER_PERIOD
            if phase < GLINT_SECS:
                return 1 / TWEEN_FPS
            soonest = min(soonest, SHIMMER_PERIOD - phase)
        return soonest

    def _next_sparkle(self, now: float, slots: List[Tuple[int, int]]) -> float:
        soonest = 1.0
        for row, slot in slots:
            c = _sparkle_cycle(row, slot, now)
            for cycle in (c, c + 1):
                b = _sparkle_at(row, slot, cycle)
                if b is None:
                    continue
                if b[0] <= now < b[0] + SPARKLE_LIFE:
                    return 1 / SPARKLE_FPS
                if b[0] > now:
                    soonest = min(soonest, b[0] - now)
                    break
        return soonest

    def sparkles(self, now: float, live: bool = True) -> List[Sparkle]:
        """The twinkles alive at ``now`` (none while stale, i.e. ``live`` False)."""
        out = []
        for row, slot in self._sparkle_slots(live):
            b = _sparkle_at(row, slot, _sparkle_cycle(row, slot, now))
            if b is not None and b[0] <= now < b[0] + SPARKLE_LIFE:
                out.append(Sparkle(row, b[1], b[2], _twinkle((now - b[0]) / SPARKLE_LIFE)))
        return out

    def breath(self, now: float) -> float:
        """Opacity of a breathing fill: 1.0 down to 1 - PULSE_DEPTH and back."""
        return 1 - PULSE_DEPTH * (0.5 - 0.5 * math.cos(2 * math.pi * now / PULSE_PERIOD))

    def frame(self, now: float, pulse_ok: bool = True,
              breath: Optional[float] = None, fill_ok: bool = True) -> List[RowFrame]:
        """What to draw at ``now``. ``breath`` overrides the breathing opacity
        (used to pre-render the two ends of a breath); ``fill_ok`` False leaves
        out the idle shine (used to pre-render the settled band)."""
        if breath is None:
            breath = self.breath(now) if self.pulsing(pulse_ok) else 1.0
        shimmer = set(self._shimmer_rows(pulse_ok and fill_ok))
        out = []
        for i, r in enumerate(self.rows):
            glint = r.glint_at(now)
            if glint is None and i in shimmer:
                glint = self._shimmer_at(i, now)  # a value-change shine wins
            out.append(RowFrame(r.pct_at(now), r.color_at(now), glint,
                                breath if r.target is not None and r.target >= self.crit else 1.0))
        return out
