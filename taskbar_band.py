"""A wide battery-style usage meter embedded *inside* the Windows taskbar.

Tray icons are fixed squares, so to get a battery-style wide readout we create
our own window and make it a child of the taskbar (``Shell_TrayWnd``), placed
just left of the notification area - the same trick TrafficMonitor uses.

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
import threading
import time
import winreg
from datetime import datetime
from ctypes import wintypes
from typing import Any, Callable, Dict, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFont

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
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
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


def _reset_mode(cfg: Dict[str, Any]) -> str:
    return str(cfg.get("taskbar_band_reset", "remaining"))


def band_width(scale: float, cfg: Dict[str, Any]) -> int:
    extra = 0 if _reset_mode(cfg) == "off" else RESET_COL
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


def render_band(snap: Optional[UsageSnapshot], cfg: Dict[str, Any], height: int,
                scale: float, light: bool, stale: bool = False) -> Image.Image:
    """Two battery rows (5h / 7d) sized to the taskbar height. RGBA, straight alpha."""
    w, h = band_width(scale, cfg), height
    mode = _reset_mode(cfg)
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

    rows = [("5h", snap.session if snap else None), ("7d", snap.weekly if snap else None)]
    row_h = 17 * s
    top = (H - row_h * len(rows)) / 2
    for i, (label, g) in enumerate(rows):
        pct = g.pct if isinstance(g, Gauge) else None
        cy = top + row_h * (i + 0.5)

        # label
        d.text((2 * s, cy), label, font=font, fill=fg, anchor="lm")

        # battery body + nub
        bx0, bw, bh = 22 * s, 38 * s, 11 * s
        by0 = cy - bh / 2
        lw = max(1, int(round(1.2 * s)))
        d.rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], radius=2.5 * s,
                            outline=outline, width=lw)
        d.rounded_rectangle([bx0 + bw + 1 * s, cy - 2.5 * s, bx0 + bw + 3 * s, cy + 2.5 * s],
                            radius=1 * s, fill=outline)
        if pct:
            inset = lw + 1.2 * s
            fw = (bw - 2 * inset) * min(max(pct, 0.0), 1.0)
            if fw >= 1:
                d.rounded_rectangle([bx0 + inset, by0 + inset, bx0 + inset + fw, by0 + bh - inset],
                                    radius=1.2 * s, fill=_color(pct, cfg) + (255,))

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
        self._dirty = True
        self._lock = threading.Lock()
        self._tid: Optional[int] = None
        self._ready = threading.Event()
        self._hwnd: Optional[int] = None
        self._tray: Optional[int] = None
        self._geom: Optional[Tuple[int, int, int, int]] = None
        self._style_key: Optional[Tuple[float, bool, int]] = None
        self._wndproc = WNDPROC(self._proc)  # keep alive
        self._thread: Optional[threading.Thread] = None
        self._failed_logged = False

    # -- public, thread-safe --
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="taskbar-band", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def update(self, snap: Optional[UsageSnapshot], stale: bool = False) -> None:
        with self._lock:
            self._snap, self._stale, self._dirty = snap, stale, True
        self._post(WM_APP_WAKE)

    def stop(self) -> None:
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
        while not quit_:
            user32.MsgWaitForMultipleObjects(0, None, False, 1000, QS_ALLINPUT)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_APP_QUIT:
                    quit_ = True
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if not quit_:
                try:
                    self._tick()
                except Exception as exc:  # noqa: BLE001 - never let the pump die
                    self.log(f"[band] tick error: {exc!r}")
        if self._hwnd and user32.IsWindow(self._hwnd):
            user32.DestroyWindow(self._hwnd)
        self.log("[band] stopped")

    def _tick(self) -> None:
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return  # explorer restarting
        if (not self._hwnd or not user32.IsWindow(self._hwnd)
                or user32.GetParent(self._hwnd) != tray):
            self._create(tray)
            if not self._hwnd:
                return

        scale = (user32.GetDpiForWindow(tray) or 96) / 96
        light = taskbar_is_light()
        geom = self._target_geom(tray, scale)
        style_key = (scale, light, int(time.time() // 60))

        with self._lock:
            dirty = self._dirty or geom != self._geom or style_key != self._style_key
            snap, stale = self._snap, self._stale
            self._dirty = False

        if geom != self._geom:
            x, y, w, h = geom
            user32.SetWindowPos(self._hwnd, HWND_TOP, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        elif user32.GetWindow(self._hwnd, GW_HWNDPREV):
            # Something (the XAML taskbar content) got stacked above us.
            user32.SetWindowPos(self._hwnd, HWND_TOP, 0, 0, 0, 0,
                                SWP_NOACTIVATE | 0x0001 | 0x0002)  # NOSIZE|NOMOVE
        if dirty:
            self._paint(render_band(snap, self.cfg, geom[3], scale, light, stale))
        self._geom, self._style_key = geom, style_key

    def _create(self, tray: int) -> None:
        self._tray = tray
        self._geom = None
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            CLASS_NAME, "Claude usage", WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
            0, 0, 1, 1, tray, None, kernel32.GetModuleHandleW(None), None)
        if self._hwnd:
            self.log(f"[band] created hwnd={self._hwnd:#x} in Shell_TrayWnd={tray:#x}")
            self._failed_logged = False
        elif not self._failed_logged:
            self.log(f"[band] CreateWindowEx failed: err={ctypes.get_last_error()}")
            self._failed_logged = True

    def _target_geom(self, tray: int, scale: float) -> Tuple[int, int, int, int]:
        """(x, y, w, h) in Shell_TrayWnd client coords: just left of the tray area."""
        rt = wintypes.RECT()
        user32.GetWindowRect(tray, ctypes.byref(rt))
        h = rt.bottom - rt.top
        w = band_width(scale, self.cfg)
        notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
        rn = wintypes.RECT()
        if notify and user32.GetWindowRect(notify, ctypes.byref(rn)) and rn.right > rn.left:
            right = rn.left - rt.left
        else:
            right = (rt.right - rt.left) - int(300 * scale)
        x = right - w - int(6 * scale) + int(self.cfg.get("taskbar_band_offset_x", 0) * scale)
        return (x, 0, w, h)

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
            im = render_band(fake, cfg, 72, 1.5, light)
            canvas = Image.new("RGBA", im.size, bg)
            canvas.alpha_composite(im)
            canvas.save(f"band_{'light' if light else 'dark'}.png")
        print("wrote band_dark.png / band_light.png")
    else:
        secs = float(sys.argv[1]) if len(sys.argv) > 1 else 15
        band = TaskbarBand(cfg)
        band.start()
        band.update(fake)
        time.sleep(secs)
        band.stop()
