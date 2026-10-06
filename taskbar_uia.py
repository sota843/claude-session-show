"""Where the Windows 11 taskbar buttons actually are, via UI Automation.

The Win11 taskbar is XAML: its buttons (Widgets, Start, Search, Task View, app
buttons) have no HWNDs, so the only way to know which part of the taskbar is
free is UIA. We ask for the children of the ``TaskbarFrame`` element and keep
their horizontal extents, so the band can avoid sitting on top of them.

UIA calls go cross-process into explorer, so they run on their own thread -
never on the band thread, whose input queue is attached to explorer's. Raw
ctypes COM keeps this dependency-free (no comtypes).
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, List, Optional, Tuple

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

COINIT_MULTITHREADED = 0x0
CLSCTX_INPROC_SERVER = 0x1
VT_BSTR = 8
TreeScope_Children = 2
TreeScope_Descendants = 4
UIA_AutomationIdPropertyId = 30011
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# vtable slots (IUnknown takes 0-2)
_RELEASE = 2
_UIA_ELEMENT_FROM_HANDLE = 6
_UIA_CREATE_TRUE_CONDITION = 21
_UIA_CREATE_PROPERTY_CONDITION = 23
_EL_FIND_FIRST = 5
_EL_FIND_ALL = 6
_EL_BOUNDING_RECT = 43
_ARR_LENGTH = 3
_ARR_GET_ELEMENT = 4


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.WORD), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
                ("r3", wintypes.WORD), ("val", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


ole32.CoInitializeEx.restype = ctypes.HRESULT
ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
ole32.CoUninitialize.restype = None
ole32.CoCreateInstance.restype = ctypes.HRESULT
ole32.CoCreateInstance.argtypes = (ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
ole32.CLSIDFromString.restype = ctypes.HRESULT
ole32.CLSIDFromString.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(GUID))
oleaut32.SysAllocString.restype = ctypes.c_void_p
oleaut32.SysAllocString.argtypes = (wintypes.LPCWSTR,)
oleaut32.SysFreeString.restype = None
oleaut32.SysFreeString.argtypes = (ctypes.c_void_p,)
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.FindWindowExW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = (wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))


def _guid(s: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString(s, ctypes.byref(g))
    return g


CLSID_CUIAutomation = _guid("{ff48dba4-60ef-4201-aa87-54103eef594e}")
IID_IUIAutomation = _guid("{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}")


def _call(obj: ctypes.c_void_p, slot: int, argtypes: tuple, *args) -> int:
    """Call COM method #slot on obj; HRESULT failures raise OSError."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return proto(vtbl[slot])(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[_RELEASE])(obj)


def _out(obj: ctypes.c_void_p, slot: int, argtypes: tuple, *args) -> ctypes.c_void_p:
    """Call a method whose last parameter is an interface out-pointer."""
    res = ctypes.c_void_p()
    _call(obj, slot, argtypes + (ctypes.POINTER(ctypes.c_void_p),), *args, ctypes.byref(res))
    return res


def _rects(uia: ctypes.c_void_p, el: ctypes.c_void_p, scope: int,
           cond: ctypes.c_void_p) -> List[Tuple[int, int]]:
    """Screen-x (left, right) of every element el.FindAll(scope, cond) returns."""
    arr = _out(el, _EL_FIND_ALL, (ctypes.c_int, ctypes.c_void_p), scope, cond)
    if not arr:
        return []
    try:
        n = ctypes.c_int()
        _call(arr, _ARR_LENGTH, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(n))
        spans = []
        for i in range(n.value):
            item = _out(arr, _ARR_GET_ELEMENT, (ctypes.c_int,), i)
            try:
                r = wintypes.RECT()
                _call(item, _EL_BOUNDING_RECT, (ctypes.POINTER(wintypes.RECT),), ctypes.byref(r))
                if r.right > r.left:
                    spans.append((r.left, r.right))
            finally:
                _release(item)
        return sorted(spans)
    finally:
        _release(arr)


def _id_condition(uia: ctypes.c_void_p, automation_id: str) -> ctypes.c_void_p:
    bstr = oleaut32.SysAllocString(automation_id)
    try:
        return _out(uia, _UIA_CREATE_PROPERTY_CONDITION, (ctypes.c_int, VARIANT),
                    UIA_AutomationIdPropertyId, VARIANT(vt=VT_BSTR, val=bstr))
    finally:
        oleaut32.SysFreeString(bstr)


Layout = Tuple[List[Tuple[int, int]], Optional[int]]


def taskbar_layout(uia: ctypes.c_void_p, tray: int) -> Optional[Layout]:
    """(buttons, tray_left) of a Win11 taskbar in screen x: the (left, right) of
    every button, and where the tray icons / clock begin (None = nothing there,
    e.g. a secondary taskbar without a clock). None when there is no XAML
    TaskbarFrame (Windows 10, explorer restarting...)."""
    root = frame = cond_frame = cond_true = cond_tray = None
    try:
        root = _out(uia, _UIA_ELEMENT_FROM_HANDLE, (wintypes.HWND,), tray)
        if not root:
            return None
        cond_frame = _id_condition(uia, "TaskbarFrame")
        frame = _out(root, _EL_FIND_FIRST, (ctypes.c_int, ctypes.c_void_p),
                     TreeScope_Descendants, cond_frame)
        if not frame:
            return None
        cond_true = _out(uia, _UIA_CREATE_TRUE_CONDITION, ())
        spans = _rects(uia, frame, TreeScope_Children, cond_true)
        if not spans:
            return None
        cond_tray = _id_condition(uia, "SystemTrayIcon")  # tray buttons and the clock
        tray_icons = _rects(uia, root, TreeScope_Descendants, cond_tray)
        return spans, (tray_icons[0][0] if tray_icons else None)
    finally:
        for obj in (cond_tray, cond_true, frame, cond_frame, root):
            _release(obj)


def find_taskbar(monitor: str = "primary") -> Optional[int]:
    """HWND of the taskbar to embed in. "secondary" = the leftmost taskbar on a
    non-main monitor, falling back to the main one when there is none."""
    if monitor == "secondary":
        bars, h = [], None
        while True:
            h = user32.FindWindowExW(None, h, "Shell_SecondaryTrayWnd", None)
            if not h:
                break
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            bars.append((r.left, h))
        if bars:
            return min(bars)[1]
    return user32.FindWindowW("Shell_TrayWnd", None)


class TaskbarProbe:
    """Polls the button layout of the taskbar ``find_taskbar()`` returns, on a
    background thread. ``layout_for(hwnd)`` is the latest result for that
    taskbar (None = unknown); ``on_change`` fires whenever it changes."""

    def __init__(self, on_change: Callable[[], None], log: Callable[[str], None] = print,
                 find: Callable[[], Optional[int]] = find_taskbar,
                 interval: float = 1.0) -> None:
        self.on_change = on_change
        self.log = log
        self.find = find
        self.interval = interval
        self._result: Tuple[Optional[int], Optional[Layout]] = (None, None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._first = threading.Event()  # set after the first poll (or on failure)
        self._thread: Optional[threading.Thread] = None

    def layout_for(self, tray: int) -> Optional[Layout]:
        with self._lock:
            hwnd, layout = self._result
        return layout if hwnd == tray else None

    def start(self) -> None:
        """Start polling; returns once the first result is in (or after 2s)."""
        self._thread = threading.Thread(target=self._run, name="taskbar-uia", daemon=True)
        self._thread.start()
        self._first.wait(2)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(3)

    def _run(self) -> None:
        user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        uia = ctypes.c_void_p()
        try:
            ole32.CoCreateInstance(ctypes.byref(CLSID_CUIAutomation), None, CLSCTX_INPROC_SERVER,
                                   ctypes.byref(IID_IUIAutomation), ctypes.byref(uia))
        except OSError as exc:
            self.log(f"[uia] unavailable, band will not avoid taskbar buttons: {exc!r}")
            ole32.CoUninitialize()
            self._first.set()
            return
        last_err = None
        try:
            while not self._stop.is_set():
                layout = None
                tray = self.find()
                if tray:
                    try:
                        layout = taskbar_layout(uia, tray)
                        last_err = None
                    except OSError as exc:  # explorer restarting etc.
                        if repr(exc) != last_err:
                            self.log(f"[uia] query failed: {exc!r}")
                            last_err = repr(exc)
                with self._lock:
                    changed = (tray, layout) != self._result
                    self._result = (tray, layout)
                if changed:
                    self.on_change()
                self._first.set()
                self._stop.wait(self.interval)
        finally:
            _release(uia)
            ole32.CoUninitialize()


if __name__ == "__main__":
    import sys
    import time
    which = sys.argv[1] if len(sys.argv) > 1 else "primary"
    probe = TaskbarProbe(on_change=lambda: print(probe.layout_for(find_taskbar(which))),
                         find=lambda: find_taskbar(which))
    probe.start()
    time.sleep(1.5)
    probe.stop()
