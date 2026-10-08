"""A wide battery-style usage meter embedded *inside* the Windows taskbar.

Tray icons are fixed squares, so to get a battery-style wide readout we create
our own window and make it a child of the taskbar (``Shell_TrayWnd``, or
``Shell_SecondaryTrayWnd`` on another monitor), placed just left of the
notification area / clock - the same trick TrafficMonitor uses.

The window is a per-pixel-alpha layered child (Win8+), rendered with Pillow and
pushed via UpdateLayeredWindow, and is click-through (WS_EX_TRANSPARENT).

Because a cross-process child attaches our thread's input queue to explorer's,
the band runs on its own thread that only pumps messages and does quick redraws
- it must never block. If explorer restarts the window dies with the taskbar;
the housekeeping tick notices and re-creates it.

Toggle with ``"taskbar_band"`` in config.json (on by default).
"""
from __future__ import annotations

import ctypes
import functools
import math
import threading
import time
import winreg
from datetime import datetime
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFont

import band_anim
import taskbar_uia
from band_anim import BandAnimator, RowFrame
from usage import Gauge, UsageSnapshot

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_CLIPSIBLINGS = 0x04000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
HWND_TOP = 0
GW_HWNDPREV = 3
ULW_ALPHA = 0x02
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
BI_RGB = 0
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
WM_APP = 0x8000
WM_APP_WAKE = WM_APP + 1
WM_APP_QUIT = WM_APP + 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

CLASS_NAME = "ClaudeUsageTaskbarBand"
SS = 3  # supersampling for anti-aliased shapes/text


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


def _sig(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = argtypes


_sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
     wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
_sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.IsWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.GetParent, wintypes.HWND, wintypes.HWND)
_sig(user32.GetWindow, wintypes.HWND, wintypes.HWND, wintypes.UINT)
_sig(user32.FindWindowW, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
_sig(user32.FindWindowExW, wintypes.HWND, wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
_sig(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_sig(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
_sig(user32.GetDC, wintypes.HDC, wintypes.HWND)
_sig(user32.ReleaseDC, ctypes.c_int, wintypes.HWND, wintypes.HDC)
_sig(user32.UpdateLayeredWindow, wintypes.BOOL, wintypes.HWND, wintypes.HDC,
     ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
     ctypes.POINTER(wintypes.POINT), wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION),
     wintypes.DWORD)
_sig(user32.GetDpiForWindow, wintypes.UINT, wintypes.HWND)
_sig(user32.SetThreadDpiAwarenessContext, ctypes.c_void_p, ctypes.c_void_p)
_sig(user32.MsgWaitForMultipleObjects, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
     wintypes.BOOL, wintypes.DWORD, wintypes.DWORD)
_sig(user32.PeekMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND,
     wintypes.UINT, wintypes.UINT, wintypes.UINT)
_sig(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
_sig(user32.PostThreadMessageW, wintypes.BOOL, wintypes.DWORD, wintypes.UINT,
     wintypes.WPARAM, wintypes.LPARAM)
_sig(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
_sig(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
_sig(gdi32.CreateDIBSection, wintypes.HBITMAP, wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
     ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
_sig(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_sig(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
_sig(kernel32.GetCurrentThreadId, wintypes.DWORD)


# ----- rendering ----------------------------------------------------------------

@functools.lru_cache(maxsize=16)
def _font(px: int) -> ImageFont.FreeTypeFont:
    for name in ("seguisb.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def taskbar_is_light() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


def _color(pct: Optional[float], cfg: Dict[str, Any]) -> Tuple[int, int, int]:
    c = cfg["colors"]
    if pct is None:
        return tuple(c["unknown"])
    th = cfg["thresholds"]
    if pct >= th["crit"]:
        return tuple(c["crit"])
    if pct >= th["warn"]:
        return tuple(c["warn"])
    return tuple(c["ok"])


RESET_COL = 44  # extra width (96-dpi px) for the reset column
MINI_W = 52     # "5h 42%" text only, for when the taskbar is crowded

# Widest first: the band falls back to narrower ones when buttons leave no room.
#   full    = label + battery + % + reset time
#   compact = label + battery + %
#   mini    = label + % (warn/crit tinted)
LAYOUTS = ("full", "compact", "mini")


def _reset_mode(cfg: Dict[str, Any]) -> str:
    return str(cfg.get("taskbar_band_reset", "remaining"))


def band_width(scale: float, cfg: Dict[str, Any], layout: str = "full") -> int:
    if layout == "mini":
        return int(round(MINI_W * scale))
    extra = RESET_COL if layout == "full" and _reset_mode(cfg) != "off" else 0
    return int(round((102 + extra) * scale))


def format_reset(g: Optional[Gauge], mode: str, now: Optional[datetime] = None) -> str:
    """'remaining' -> 43m / 2h13m / 3d4h ; 'clock' -> 14:30 (today) or 10/9 (later)."""
    if not isinstance(g, Gauge) or g.reset is None or mode == "off":
        return ""
    now = now or datetime.now().astimezone()
    if mode == "clock":
        r = g.reset.astimezone()
        return r.strftime("%H:%M") if r.date() == now.date() else f"{r.month}/{r.day}"
    mins = max(0, int((g.reset - now).total_seconds() // 60))
    if mins < 60:
        return f"{mins}m"
    if mins < 24 * 60:
        return f"{mins // 60}h{mins % 60:02d}m"
    return f"{mins // 1440}d{mins % 1440 // 60}h"


def _draw_glint(img: Image.Image, box: Tuple[float, float, float, float], radius: float,
                progress: float, s: float) -> None:
    """A soft, slanted shine at ``progress`` (0..1) across ``box``, clipped to it."""
    x0, y0, x1, y1 = box
    half, skew = 5 * s, 3 * s
    e = 0.5 - 0.5 * math.cos(math.pi * progress)
    cx = x0 - half - skew + (x1 - x0 + 2 * (half + skew)) * e
    layer = Image.new("RGBA", img.size, (255, 255, 255, 0))
    d = ImageDraw.Draw(layer)
    for k in range(-int(half), int(half) + 1):
        a = int(150 * (1 - abs(k) / half))
        if a > 0:
            d.line([(cx + k + skew, y0), (cx + k - skew, y1)], fill=(255, 255, 255, a), width=1)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(list(box), radius=radius, fill=255)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    img.alpha_composite(layer)


ROW_H = 17
BAT_X, BAT_W, BAT_H = 22, 38, 11  # battery body, 96-dpi px
SPARKLE_STEPS = 8  # sizes a twinkle passes through (each a cached sprite)


def _row_centres(H: float, s: float, n: int = 2) -> List[float]:
    top = (H - ROW_H * s * n) / 2
    return [top + ROW_H * s * (i + 0.5) for i in range(n)]


def _fill_box(cy: float, pct: float, s: float) -> Optional[Tuple[float, float, float, float]]:
    """The battery fill for ``pct`` on the row centred at ``cy`` (None if < 1px)."""
    lw = max(1, int(round(1.2 * s)))
    inset = lw + 1.2 * s
    x0, y0 = BAT_X * s + inset, cy - BAT_H * s / 2 + inset
    fw = (BAT_W * s - 2 * inset) * min(max(pct, 0.0), 1.0)
    if fw < 1:
        return None
    return x0, y0, x0 + fw, cy + BAT_H * s / 2 - inset


@functools.lru_cache(maxsize=64)
def _sparkle_sprite(step: int, scale: float) -> Image.Image:
    """A four-pointed twinkle at ``step``/SPARKLE_STEPS of its full size."""
    e = step / SPARKLE_STEPS
    arm = (1.2 + 2.8 * e) * scale  # final px
    side = int(math.ceil(2 * arm)) + 2
    S, c, R = side * SS, side * SS / 2, arm * SS
    w = R * 0.2
    a = round(255 * (0.45 + 0.55 * e))
    im = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    d = ImageDraw.Draw(im)
    g = R * 0.45
    d.ellipse([c - g, c - g, c + g, c + g], fill=(255, 255, 240, a // 3))
    d.polygon([(c, c - R), (c + w, c - w), (c + R, c), (c + w, c + w),
               (c, c + R), (c - w, c + w), (c - R, c), (c - w, c - w)],
              fill=(255, 255, 240, a))
    return im.resize((side, side), Image.LANCZOS)


def draw_glints(img: Image.Image, frames: List[RowFrame], scale: float) -> None:
    """The idle shine onto an already rendered band (final pixels), so the
    settled band can stay cached while it sweeps."""
    s = scale * SS
    for f, cy in zip(frames, _row_centres(img.height * SS, s)):
        box = _fill_box(cy, f.pct, s) if f.pct and f.glint is not None else None
        if box:
            _draw_glint(img, tuple(v / SS for v in box), 1.2 * scale, f.glint, scale)


def draw_sparkles(img: Image.Image, frames: List[RowFrame],
                  sparks: List[band_anim.Sparkle], scale: float) -> None:
    """Composite twinkles onto an already rendered band (final pixels)."""
    s = scale * SS
    cys = _row_centres(img.height * SS, s)
    for sp in sparks:
        step = round(sp.size * SPARKLE_STEPS)
        pct = frames[sp.row].pct
        box = _fill_box(cys[sp.row], pct, s) if pct and step > 0 else None
        if box is None:
            continue
        x0, y0, x1, y1 = (v / SS for v in box)
        sprite = _sparkle_sprite(step, scale)
        cx, cy = x0 + (x1 - x0) * sp.x, y0 + (y1 - y0) * sp.y
        img.alpha_composite(sprite, (max(0, round(cx - sprite.width / 2)),
                                     max(0, round(cy - sprite.height / 2))))


def static_frames(snap: Optional[UsageSnapshot], cfg: Dict[str, Any]) -> List[RowFrame]:
    """Frames for drawing the snapshot as-is (no animation in flight)."""
    return [RowFrame(p, _color(p, cfg), None, 1.0) for p in row_pcts(snap)]


def row_pcts(snap: Optional[UsageSnapshot]) -> List[Optional[float]]:
    gauges = (snap.session, snap.weekly) if snap else (None, None)
    return [g.pct if isinstance(g, Gauge) else None for g in gauges]


def render_band(snap: Optional[UsageSnapshot], cfg: Dict[str, Any], height: int,
                scale: float, light: bool, stale: bool = False,
                layout: str = "full", frames: Optional[List[RowFrame]] = None) -> Image.Image:
    """Two battery rows (5h / 7d) sized to the taskbar height. RGBA, straight alpha.
    ``frames`` (from BandAnimator) override the drawn %/colour mid-animation."""
    w, h = band_width(scale, cfg, layout), height
    mode = _reset_mode(cfg) if layout == "full" else "off"
    W, H, s = w * SS, h * SS, scale * SS
    pct_right = 102 * s - 2 * s  # % column ends where the base band ends
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fg = (20, 20, 20, 255) if light else (255, 255, 255, 255)
    outline = (fg[0], fg[1], fg[2], 200)
    dim = (fg[0], fg[1], fg[2], 170)
    font = _font(int(12 * s))
    small = _font(int(11 * s))
    now = datetime.now().astimezone()
    frames = frames or static_frames(snap, cfg)

    rows = [("5h", snap.session if snap else None), ("7d", snap.weekly if snap else None)]
    for i, ((label, g), cy) in enumerate(zip(rows, _row_centres(H, s, len(rows)))):
        f = frames[i]
        pct = f.pct

        # label
        d.text((2 * s, cy), label, font=font, fill=fg, anchor="lm")

        if layout == "mini":
            # No room for the battery: tint the number once it needs attention.
            tint = (fg if pct is None or pct < cfg["thresholds"]["warn"]
                    else f.color + (round(255 * f.pulse),))
            txt = "--" if pct is None else f"{round(pct * 100)}%"
            d.text((W - 2 * s, cy), txt, font=font, fill=tint, anchor="rm")
            continue

        # battery body + nub
        bx0, bw, bh = BAT_X * s, BAT_W * s, BAT_H * s
        by0 = cy - bh / 2
        lw = max(1, int(round(1.2 * s)))
        d.rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], radius=2.5 * s,
                            outline=outline, width=lw)
        d.rounded_rectangle([bx0 + bw + 1 * s, cy - 2.5 * s, bx0 + bw + 3 * s, cy + 2.5 * s],
                            radius=1 * s, fill=outline)
        if pct:
            box = _fill_box(cy, pct, s)
            if box:
                d.rounded_rectangle(list(box), radius=1.2 * s,
                                    fill=f.color + (round(255 * f.pulse),))
                if f.glint is not None:
                    _draw_glint(img, box, 1.2 * s, f.glint, s)

        # percentage
        txt = "--" if pct is None else f"{round(pct * 100)}%"
        d.text((pct_right, cy), txt, font=font, fill=fg, anchor="rm")

        # time until reset
        rtxt = format_reset(g, mode, now)
        if rtxt:
            d.text((W - 2 * s, cy), rtxt, font=small, fill=dim, anchor="rm")

    img = img.resize((w, h), Image.LANCZOS)
    if stale:
        img.putalpha(img.getchannel("A").point(lambda a: a * 45 // 100))
    return img


EDGE = 6  # 96-dpi px kept clear between the band and the tray / taskbar buttons


def band_side(cfg: Dict[str, Any], on_primary: bool) -> str:
    """"right"/"left" for this taskbar. ``taskbar_band_side`` is either one value
    for every taskbar or {"primary": ..., "secondary": ...}."""
    side = cfg.get("taskbar_band_side", "right")
    if isinstance(side, dict):
        side = side.get("primary" if on_primary else "secondary", "right")
    return str(side)


def place_band(spans: Optional[List[Tuple[int, int]]], right: int, scale: float,
               cfg: Dict[str, Any], side: str = "right") -> Optional[Tuple[int, int, str]]:
    """Pick (x, width, layout) in taskbar client coords.

    ``spans`` are the taskbar buttons' (left, right) and ``right`` is where the
    tray area starts. The widest layout that fits in a free gap wins; among
    gaps, the one touching the tray is preferred, then the one nearest to it.
    With ``side="left"`` it is the leftmost gap instead (next
    to the Widgets button, or the very left when Widgets is off), left-aligned.
    None = no room anywhere. Without spans (Win10 / UIA failed) the band just
    sits left of the tray at full width, as before.
    """
    pad = int(EDGE * scale)
    off = int(cfg.get("taskbar_band_offset_x", 0) * scale)
    home_r = right - pad + off
    if spans is None:
        w = band_width(scale, cfg)
        return home_r - w, w, "full"
    if side == "left":
        return _place_left(spans, right - pad, pad, off, scale, cfg)

    gaps, cur = [], pad
    for l, r in sorted((l - pad, r + pad) for l, r in spans):
        if l >= home_r:
            break
        if l > cur:
            gaps.append((cur, l))
        cur = max(cur, r)
    if cur < home_r:
        gaps.append((cur, home_r))
    home = gaps[-1] if gaps and gaps[-1][1] == home_r else None

    for layout in LAYOUTS:
        if layout == "compact" and _reset_mode(cfg) == "off":
            continue  # identical to "full"
        w = band_width(scale, cfg, layout)
        if home and home[1] - home[0] >= w:
            return home_r - w, w, layout
        fits = [g for g in gaps if g[1] - g[0] >= w]
        if fits:
            a, b = max(fits, key=lambda g: g[1])
            return (a + b - w) // 2, w, layout
    return None


def _place_left(spans: List[Tuple[int, int]], limit: int, pad: int, off: int,
                scale: float, cfg: Dict[str, Any]) -> Optional[Tuple[int, int, str]]:
    """Leftmost free gap the widest layout fits in, band left-aligned in it."""
    gaps, cur = [], pad
    for l, r in sorted((l - pad, r + pad) for l, r in spans):
        if l >= limit:
            break
        if l > cur:
            gaps.append((cur, l))
        cur = max(cur, r)
    if cur < limit:
        gaps.append((cur, limit))
    for layout in LAYOUTS:
        if layout == "compact" and _reset_mode(cfg) == "off":
            continue
        w = band_width(scale, cfg, layout)
        for a, b in gaps:
            if b - a >= w:
                return max(a, min(a + off, b - w)), w, layout
    return None


def _to_premultiplied_bgra(img: Image.Image) -> bytes:
    r, g, b, a = img.split()
    r, g, b = (ImageChops.multiply(ch, a) for ch in (r, g, b))
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


# ----- window -------------------------------------------------------------------

class TaskbarBand:
    """Owns the band window and its message-pumping thread."""

    def __init__(self, cfg: Dict[str, Any], log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.log = log
        self._snap: Optional[UsageSnapshot] = None
        self._stale = False
        self._dirty = True    # new snapshot not yet handed to the animator
        self._repaint = True  # something changed that the window does not show yet
        self._anim = BandAnimator(lambda p: _color(p, cfg), cfg["thresholds"]["crit"],
                                  enabled=bool(cfg.get("taskbar_band_animate", True)),
                                  pulse=bool(cfg.get("taskbar_band_pulse", True)),
                                  sparkle=bool(cfg.get("taskbar_band_sparkle", True)),
                                  shimmer=bool(cfg.get("taskbar_band_shimmer", True)))
        self._scale, self._light = 1.0, False
        self._idle_cache: Optional[Tuple[Any, Image.Image, Optional[Image.Image]]] = None
        self._lock = threading.Lock()
        self._tid: Optional[int] = None
        self._ready = threading.Event()
        self._hwnd: Optional[int] = None
        self._tray: Optional[int] = None
        self._geom: Optional[Tuple[int, int, int, int]] = None
        self._layout: Optional[str] = None
        self._hidden_logged = False
        self._style_key: Optional[Tuple[float, bool, int]] = None
        self._wndproc = WNDPROC(self._proc)  # keep alive
        self._thread: Optional[threading.Thread] = None
        self._failed_logged = False
        # Where the taskbar buttons are, so the band can dodge them.
        self._probe = taskbar_uia.TaskbarProbe(on_change=lambda: self._post(WM_APP_WAKE), log=log,
                                               find=self._find_taskbar)

    # -- public, thread-safe --
    def start(self) -> None:
        self._probe.start()  # first, so the band never appears on top of buttons
        self._thread = threading.Thread(target=self._run, name="taskbar-band", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def update(self, snap: Optional[UsageSnapshot], stale: bool = False) -> None:
        with self._lock:
            self._snap, self._stale, self._dirty = snap, stale, True
        self._post(WM_APP_WAKE)

    def stop(self) -> None:
        self._probe.stop()
        self._post(WM_APP_QUIT)
        if self._thread:
            self._thread.join(3)

    def _post(self, msg: int) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, msg, 0, 0)

    # -- band thread --
    def _proc(self, hwnd, msg, wp, lp):
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _run(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        # Taskbar coords/DPI in physical pixels, matching explorer (PMv2).
        user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = CLASS_NAME
        user32.RegisterClassExW(ctypes.byref(wc))
        self._ready.set()

        msg = wintypes.MSG()
        quit_ = False
        last_tick = 0.0
        while not quit_:
            # Housekeeping once a second; animation frames in between only while
            # something is actually moving.
            delay = (self._anim.next_delay(time.monotonic(), not self._stale, self._fill_ok())
                     if self._geom else None)
            timeout = 1000 if delay is None else max(1, int(delay * 1000))
            user32.MsgWaitForMultipleObjects(0, None, False, timeout, QS_ALLINPUT)
            woke = False
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                woke = True
                if msg.message == WM_APP_QUIT:
                    quit_ = True
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if quit_:
                break
            now = time.monotonic()
            try:
                if woke or now - last_tick >= 1.0:
                    self._tick()
                    last_tick = now
                elif self._geom and self._anim.active(now, not self._stale, self._fill_ok()):
                    self._paint(self._render(now))
            except Exception as exc:  # noqa: BLE001 - never let the pump die
                self.log(f"[band] tick error: {exc!r}")
        if self._hwnd and user32.IsWindow(self._hwnd):
            user32.DestroyWindow(self._hwnd)
        self.log("[band] stopped")

    def _find_taskbar(self) -> Optional[int]:
        return taskbar_uia.find_taskbar(str(self.cfg.get("taskbar_band_monitor", "primary")))

    def _tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._dirty:
                self._anim.retarget(row_pcts(self._snap), now)
                self._dirty, self._repaint = False, True
            stale = self._stale
        tray = self._find_taskbar()
        if not tray:
            return  # explorer restarting
        if (not self._hwnd or not user32.IsWindow(self._hwnd)
                or user32.GetParent(self._hwnd) != tray):
            self._create(tray)
            if not self._hwnd:
                return

        scale = (user32.GetDpiForWindow(tray) or 96) / 96
        light = taskbar_is_light()
        place = self._target_geom(tray, scale)
        if place is None:
            # Buttons fill the taskbar: hide rather than draw on top of them.
            if not self._hidden_logged:
                user32.SetWindowPos(self._hwnd, HWND_TOP, 0, 0, 0, 0,
                                    SWP_HIDEWINDOW | SWP_NOMOVE | SWP_NOSIZE
                                    | SWP_NOZORDER | SWP_NOACTIVATE)
                self.log("[band] no free space in the taskbar - hidden")
                self._hidden_logged = True
            self._geom = None
            return
        geom, layout = place
        if self._hidden_logged or layout != self._layout:
            self.log(f"[band] layout={layout} x={geom[0]}")
            self._hidden_logged = False
        style_key = (scale, light, int(time.time() // 60))

        dirty = (self._repaint or geom != self._geom or layout != self._layout
                 or style_key != self._style_key)
        self._repaint = False

        if geom != self._geom:
            x, y, w, h = geom
            user32.SetWindowPos(self._hwnd, HWND_TOP, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        elif user32.GetWindow(self._hwnd, GW_HWNDPREV):
            # Something (the XAML taskbar content) got stacked above us.
            user32.SetWindowPos(self._hwnd, HWND_TOP, 0, 0, 0, 0,
                                SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOMOVE)
        self._geom, self._layout, self._style_key = geom, layout, style_key
        self._scale, self._light = scale, light
        if dirty or self._anim.active(now, not stale, self._fill_ok()):
            self._paint(self._render(now))

    def _fill_ok(self) -> bool:
        return self._layout != "mini"  # no battery to shine / sparkle on

    def _render(self, now: float) -> Image.Image:
        """The band as of ``now``, mid-animation if one is running."""
        with self._lock:
            snap, stale = self._snap, self._stale
        args = (snap, self.cfg, self._geom[3], self._scale, self._light, stale, self._layout)
        frames = self._anim.frame(now, not stale, fill_ok=self._fill_ok())
        if self._anim.busy(now):
            self._idle_cache = None
            img = render_band(*args, frames)
        else:
            img = self._idle_base(now, args, snap, stale)
            if any(f.glint is not None for f in frames):
                img = img.copy()
                draw_glints(img, frames, self._scale)
        sparks = self._anim.sparkles(now, not stale) if self._fill_ok() else []
        if sparks:
            img = img.copy()
            draw_sparkles(img, frames, sparks, self._scale)
        return img

    def _idle_base(self, now: float, args: Tuple, snap: Optional[UsageSnapshot],
                   stale: bool) -> Image.Image:
        """The settled band, rendered once (a full render is ~10ms). While
        breathing only the crit fill's opacity changes, so both ends of a breath
        are cached and cross-faded."""
        breathing = self._anim.pulsing(not stale)
        key = (snap, stale, self._geom, self._layout, self._scale, self._light,
               int(time.time() // 60), breathing)
        lo = 1 - band_anim.PULSE_DEPTH
        if self._idle_cache is None or self._idle_cache[0] != key:
            full = render_band(*args, self._anim.frame(now, breath=1.0, fill_ok=False))
            dim = (render_band(*args, self._anim.frame(now, breath=lo, fill_ok=False))
                   if breathing else None)
            self._idle_cache = (key, full, dim)
        _, full, dim = self._idle_cache
        if dim is None:
            return full
        return Image.blend(dim, full, (self._anim.breath(now) - lo) / band_anim.PULSE_DEPTH)

    def _create(self, tray: int) -> None:
        if self._hwnd and user32.IsWindow(self._hwnd):
            # Moving to another taskbar (e.g. the secondary monitor came back).
            user32.DestroyWindow(self._hwnd)
        self._tray = tray
        self._geom = None
        self._hidden_logged = False
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            CLASS_NAME, "Claude usage", WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
            0, 0, 1, 1, tray, None, kernel32.GetModuleHandleW(None), None)
        if self._hwnd:
            self.log(f"[band] created hwnd={self._hwnd:#x} in taskbar={tray:#x}")
            self._failed_logged = False
        elif not self._failed_logged:
            self.log(f"[band] CreateWindowEx failed: err={ctypes.get_last_error()}")
            self._failed_logged = True

    def _target_geom(self, tray: int, scale: float
                     ) -> Optional[Tuple[Tuple[int, int, int, int], str]]:
        """((x, y, w, h), layout) in taskbar client coords, or None when the
        taskbar buttons leave no room. Prefers just left of the tray area/clock."""
        rt = wintypes.RECT()
        user32.GetWindowRect(tray, ctypes.byref(rt))
        h = rt.bottom - rt.top
        layout = self._probe.layout_for(tray)
        spans = None
        notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
        rn = wintypes.RECT()
        if notify and user32.GetWindowRect(notify, ctypes.byref(rn)) and rn.right > rn.left:
            right = rn.left - rt.left
        elif layout is not None:
            # Secondary taskbar: only a clock (if any) on the right, no HWND for it.
            right = (layout[1] if layout[1] is not None else rt.right) - rt.left
        else:
            right = (rt.right - rt.left) - int(300 * scale)
        if layout is not None:
            spans = [(l - rt.left, r - rt.left) for l, r in layout[0]]
        on_primary = tray == user32.FindWindowW("Shell_TrayWnd", None)
        place = place_band(spans, right, scale, self.cfg, band_side(self.cfg, on_primary))
        if place is None:
            return None
        x, w, layout = place
        return (x, 0, w, h), layout

    def _paint(self, img: Image.Image) -> None:
        w, h = img.size
        data = _to_premultiplied_bgra(img)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, BI_RGB, 0, 0, 0, 0, 0)
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), DIB_RGB_COLORS,
                                      ctypes.byref(bits), None, 0)
        try:
            if not hbmp:
                self.log(f"[band] CreateDIBSection failed: err={ctypes.get_last_error()}")
                return
            ctypes.memmove(bits, data, len(data))
            old = gdi32.SelectObject(mem, hbmp)
            size = wintypes.SIZE(w, h)
            src = wintypes.POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = user32.UpdateLayeredWindow(self._hwnd, screen, None, ctypes.byref(size), mem,
                                            ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
            if not ok:
                self.log(f"[band] UpdateLayeredWindow failed: err={ctypes.get_last_error()}")
            gdi32.SelectObject(mem, old)
        finally:
            if hbmp:
                gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)


if __name__ == "__main__":
    # Standalone test: embed with fake numbers for N seconds, or just render PNGs.
    import sys

    import config as config_mod
    cfg = config_mod.load_config()
    from datetime import timedelta
    _now = datetime.now().astimezone()
    fake = UsageSnapshot(session=Gauge(42, 100, _now + timedelta(hours=2, minutes=13)),
                         weekly=Gauge(88, 100, _now + timedelta(days=3, hours=4)),
                         error=None, fetched_at=datetime.now().astimezone())
    if len(sys.argv) > 1 and sys.argv[1] == "png":
        for light in (False, True):
            bg = (243, 243, 243, 255) if light else (32, 32, 32, 255)
            for layout in LAYOUTS:
                im = render_band(fake, cfg, 72, 1.5, light, layout=layout)
                canvas = Image.new("RGBA", im.size, bg)
                canvas.alpha_composite(im)
                suffix = "" if layout == "full" else f"_{layout}"
                canvas.save(f"band_{'light' if light else 'dark'}{suffix}.png")
        print("wrote band_dark*.png / band_light*.png")
    elif len(sys.argv) > 1 and sys.argv[1] == "demo":
        # Walk through every animation in the real taskbar.
        def fake_at(s5: float, s7: float) -> UsageSnapshot:
            return UsageSnapshot(session=Gauge(s5, 100, _now + timedelta(hours=2, minutes=13)),
                                 weekly=Gauge(s7, 100, _now + timedelta(days=3, hours=4)),
                                 error=None, fetched_at=datetime.now().astimezone())
        band = TaskbarBand(cfg)
        band.start()
        steps = [((42, 55), "start"), ((47, 58), "small rise: ease + count + shine"),
                 ((52, 66), "7d crosses warn: colour fade"),
                 ((60, 90), "both cross: 7d goes crit -> breathing"),
                 ((2, 90), "5h window resets: drain")]
        for (s5, s7), what in steps:
            print(f"5h={s5}% 7d={s7}%  {what}", flush=True)
            band.update(fake_at(s5, s7))
            time.sleep(4)
        time.sleep(4)
        band.stop()
    else:
        secs = float(sys.argv[1]) if len(sys.argv) > 1 else 15
        band = TaskbarBand(cfg)
        band.start()
        band.update(fake)
        time.sleep(secs)
        band.stop()
