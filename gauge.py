"""Pillow renderer for the tray icon: a double-ring usage gauge.

Outer ring  = 5-hour session window usage
Inner ring  = weekly window usage

Each ring is drawn on a dim track and filled clockwise from 12 o'clock in
proportion to the usage percentage, coloured green / amber / red by threshold.
Rendered at 4x and downsampled for crisp anti-aliased edges at tray sizes.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

import usage as usage_mod
from usage import Gauge, UsageSnapshot

SS = 4  # supersampling factor

RGBA = Tuple[int, int, int, int]


def _rgba(seq, alpha: int = 255) -> RGBA:
    r, g, b = seq[0], seq[1], seq[2]
    return (int(r), int(g), int(b), alpha)


def _color_for_pct(pct: Optional[float], cfg: Dict[str, Any]) -> RGBA:
    colors = cfg["colors"]
    if pct is None:
        return _rgba(colors["unknown"])
    th = cfg["thresholds"]
    if pct >= th["crit"]:
        return _rgba(colors["crit"])
    if pct >= th["warn"]:
        return _rgba(colors["warn"])
    return _rgba(colors["ok"])


def _draw_ring(draw: ImageDraw.ImageDraw, box, width: int,
               pct: Optional[float], color: RGBA, track: RGBA) -> None:
    """Draw a full track then a filled arc (clockwise from top) for ``pct``."""
    draw.arc(box, start=0, end=360, fill=track, width=width)
    if pct and pct > 0:
        start = -90.0
        end = start + 360.0 * min(pct, 1.0)
        draw.arc(box, start=start, end=end, fill=color, width=width)


def render_double_ring(outer_pct: Optional[float], inner_pct: Optional[float],
                       cfg: Dict[str, Any],
                       center_text: Optional[str] = None) -> Image.Image:
    """Return an RGBA icon image for the given percentages (0..1 or None)."""
    size = int(cfg.get("icon_size", 64))
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Subtle dark disc so the icon is always visible against any taskbar colour.
    draw.ellipse([0, 0, big - 1, big - 1], fill=(28, 28, 30, 235))

    pad = big * 0.03
    ring_w = max(3, int(big * 0.17))
    gap = int(big * 0.05)

    # Outer ring (session)
    outer_box = [pad, pad, big - pad, big - pad]
    _draw_ring(draw, outer_box, ring_w,
               outer_pct, _color_for_pct(outer_pct, cfg), _rgba(cfg["colors"]["track"]))

    # Inner ring (weekly)
    inset = pad + ring_w + gap
    inner_box = [inset, inset, big - inset, big - inset]
    _draw_ring(draw, inner_box, ring_w,
               inner_pct, _color_for_pct(inner_pct, cfg), _rgba(cfg["colors"]["track"]))

    if center_text:
        _draw_center_text(draw, big, center_text, _color_for_pct(outer_pct, cfg))

    return img.resize((size, size), Image.LANCZOS)


def _draw_center_text(draw: ImageDraw.ImageDraw, big: int, text: str, color: RGBA) -> None:
    font_size = int(big * 0.30)
    font = None
    for name in ("arialbd.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            font = ImageFont.truetype(name, font_size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    draw.text(((big - (r - l)) / 2 - l, (big - (b - t)) / 2 - t),
              text, font=font, fill=color)


def make_icon(snap: UsageSnapshot, cfg: Dict[str, Any]) -> Image.Image:
    """Build the tray icon image from a usage snapshot."""
    if not snap.ok:
        return render_double_ring(None, None, cfg, center_text="?")

    s_pct = snap.session.pct if snap.session else None
    w_pct = snap.weekly.pct if snap.weekly else None

    center = None
    if cfg.get("show_center_text") and s_pct is not None:
        center = str(round(s_pct * 100))
    return render_double_ring(s_pct, w_pct, cfg, center_text=center)


if __name__ == "__main__":
    # Visual smoke test: emit sample icons at several usage levels.
    import config as config_mod
    cfg = config_mod.load_config()
    samples = {
        "30": (0.30, 0.45),
        "70": (0.70, 0.55),
        "95": (0.95, 0.80),
        "unknown": (None, None),
    }
    for name, (o, i) in samples.items():
        txt = None if o is None else str(round(o * 100))
        im = render_double_ring(o, i, cfg, center_text=txt)
        out = f"sample_{name}.png"
        im.save(out)
        print("wrote", out)
