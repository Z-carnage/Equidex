#!/usr/bin/env python3
"""
EQUIDEX desktop app entry point.

Runs the local data server (serve.py) in a background thread, then opens the terminal in a
FRAMELESS native window via pywebview (Edge WebView2 on Windows) — no OS title bar; the app's own
top bar carries the window controls and acts as the drag handle. Falls back to the default browser
if a native window can't be created.
"""
import threading, functools, socket, time, ctypes, os, winreg
from ctypes import wintypes
from http.server import ThreadingHTTPServer

import serve   # reuse the request handler, proxy routes and static-file serving

WINDOW_TITLE = "EQUIDEX"
WIN = None          # the pywebview Window, set in main()


def _asset(name):
    return os.path.join(serve.BASE_DIR, name)


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


# Declare prototypes so 64-bit HWND/handles are passed intact (an undeclared restype is
# a 32-bit int and truncates the handle, so DWM/SetWindowPos calls silently hit nothing).
_u32 = ctypes.windll.user32
_dwm = ctypes.windll.dwmapi
_u32.FindWindowW.restype = wintypes.HWND
_u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_ENUMWINDOWSPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_u32.EnumWindows.argtypes = [_ENUMWINDOWSPROC, wintypes.LPARAM]
_u32.EnumWindows.restype = wintypes.BOOL
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.GetWindowThreadProcessId.restype = wintypes.DWORD
_u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_u32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_u32.GetWindow.restype = wintypes.HWND
_u32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
_u32.IsIconic.argtypes = [wintypes.HWND]
_u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_u32.MonitorFromWindow.restype = ctypes.c_void_p
_u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_u32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
_u32.LoadImageW.restype = ctypes.c_void_p
_u32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_uint,
                            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_u32.SendMessageW.restype = ctypes.c_void_p
_u32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
_u32.DestroyIcon.argtypes = [ctypes.c_void_p]
_dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


def _hwnd():
    # Several EQUIDEX instances can coexist. A title-only FindWindow can pick
    # another process's window, making our caption buttons resize that instance.
    found = []
    @_ENUMWINDOWSPROC
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            title = ctypes.create_unicode_buffer(256)
            _u32.GetWindowTextW(hwnd, title, len(title))
            if title.value == WINDOW_TITLE:
                found.append(hwnd)
                return False
        return True
    _u32.EnumWindows(visit, 0)
    return found[0] if found else None


def _square_corners(hwnd):
    """Turn off the Windows 11 rounded corners on the frameless window (removes the corner gap)."""
    try:
        pref = ctypes.c_int(1)   # DWMWCP_DONOTROUND
        _dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))  # DWMWA_WINDOW_CORNER_PREFERENCE
    except Exception:
        pass


def _work_area(hwnd):
    """Work area (monitor minus the taskbar) of the monitor containing the window."""
    hmon = _u32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
    mi = _MONITORINFO(); mi.cbSize = ctypes.sizeof(_MONITORINFO)
    _u32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    w = mi.rcWork
    return (w.left, w.top, w.right - w.left, w.bottom - w.top)


def _taskbar_is_light():
    """True when the Windows taskbar/system chrome uses the light theme."""
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "SystemUsesLightTheme"); winreg.CloseKey(k)
        return v == 1
    except Exception:
        return False   # default: dark taskbar


def _apply_taskbar_icon(hwnd):
    """Pick the icon that stays visible on the current taskbar colour and set it on the window."""
    # light taskbar -> dark mark; dark taskbar -> white mark
    ico = _asset("equidex-light.ico" if _taskbar_is_light() else "equidex-dark.ico")
    IMAGE_ICON = 1; LR_LOADFROMFILE = 0x10; LR_DEFAULTSIZE = 0x40; WM_SETICON = 0x80
    big = _u32.LoadImageW(None, ico, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
    sml = _u32.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
    if big: _u32.SendMessageW(hwnd, WM_SETICON, 1, big)   # ICON_BIG
    if sml: _u32.SendMessageW(hwnd, WM_SETICON, 0, sml)   # ICON_SMALL
    return [h for h in (big, sml) if h]


def _grow_1px(hwnd):
    """Push the window edges out by 1px on every side (covers the hairline edge)."""
    r = _RECT(); _u32.GetWindowRect(hwnd, ctypes.byref(r))
    _u32.SetWindowPos(hwnd, None, r.left - 1, r.top - 1,
                      (r.right - r.left) + 2, (r.bottom - r.top) + 2, 0x0004 | 0x0020)


def _chrome_watcher():
    """Keep the frameless window's corners square and its taskbar icon matched to the taskbar colour."""
    global _GROWN
    last = None; prev = []
    while True:
        hwnd = _hwnd()
        if hwnd:
            _repair_webview_position(hwnd)
            _square_corners(hwnd)     # idempotent; re-assert in case Windows re-rounds it
            if not _GROWN:
                try: _grow_1px(hwnd)
                except Exception: pass
                _GROWN = True
            cur = _taskbar_is_light()
            if cur != last:
                for h in prev:
                    try: _u32.DestroyIcon(h)
                    except Exception: pass
                prev = _apply_taskbar_icon(hwnd)
                last = cur
        time.sleep(2)


def _repair_webview_position(hwnd):
    """Recover a displaced docked WebView after native minimize/activation.

    Some restore paths move the form back but leave its child at the minimized
    off-screen location. Only repair that geometry mismatch; never reload the page.
    """
    if _u32.IsIconic(hwnd):
        return False
    child = _u32.GetWindow(hwnd, 5)  # GW_CHILD: pywebview's docked browser control
    if not child:
        return False
    client = _RECT(); bounds = _RECT(); origin = wintypes.POINT(0, 0)
    if not (_u32.GetClientRect(hwnd, ctypes.byref(client)) and
            _u32.ClientToScreen(hwnd, ctypes.byref(origin)) and
            _u32.GetWindowRect(child, ctypes.byref(bounds))):
        return False
    width, height = client.right - client.left, client.bottom - client.top
    if width <= 0 or height <= 0:
        return False
    # Ignore normal border/DPI rounding. A docked child should share the client origin.
    if abs(bounds.left - origin.x) <= 2 and abs(bounds.top - origin.y) <= 2:
        return False
    return bool(_u32.SetWindowPos(child, None, 0, 0, width, height, 0x0004 | 0x0010))


def _on_restored():
    hwnd = _hwnd()
    if hwnd:
        _repair_webview_position(hwnd)


def _start_server():
    """Start the data/static server on a free port in a daemon thread; return its URL."""
    port = serve._pick_port(8777)
    handler = functools.partial(serve.Handler, directory=serve.BASE_DIR)
    try:
        srv = serve.DualStackServer(("::", port), handler)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", port), handler)   # IPv4-only fallback
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # probe the loopback of the family actually bound (::1 for the IPv6 socket): 127.0.0.1 only reaches it
    # when dual-stack could be enabled, and would be refused if the socket ended up IPv6-only
    lo = serve.loopback_for(srv)
    serve.warm_start(port, lo)   # Institutions tab data (13F totals, congress, contracts) fetched in the background
    for _ in range(50):   # wait until the port accepts connections before pointing the window at it
        s = socket.socket(srv.address_family, socket.SOCK_STREAM)
        try:
            if s.connect_ex((lo, port)) == 0:
                break
        finally:
            s.close()
        time.sleep(0.05)
    return f"http://localhost:{port}/{serve.APP_PAGE}"


class Api:
    """Exposed to the page as window.pywebview.api — custom window controls for the frameless window."""
    def __init__(self):
        self._max = False
        self._prev = None   # saved (x, y, w, h) to restore to

    def minimize(self):
        try: WIN.minimize()
        except Exception: pass

    def toggle_maximize(self):
        # "Maximize" fills the monitor WORK AREA (excludes the taskbar) rather than the whole screen,
        # so a frameless window doesn't cover the taskbar.
        try:
            hwnd = _hwnd()
            if not hwnd:
                return self._max
            SWP = 0x0004 | 0x0020   # NOZORDER | FRAMECHANGED
            if self._max and self._prev:
                x, y, w, h = self._prev
                _u32.SetWindowPos(hwnd, None, x, y, w, h, SWP)
                self._max = False
            else:
                r = _RECT(); _u32.GetWindowRect(hwnd, ctypes.byref(r))
                self._prev = (r.left, r.top, r.right - r.left, r.bottom - r.top)
                x, y, w, h = _work_area(hwnd)
                _u32.SetWindowPos(hwnd, None, x - 1, y - 1, w + 2, h + 2, SWP)  # 1px past each side, no edge gap
                self._max = True
        except Exception:
            pass
        return self._max

    def is_maximized(self):
        return self._max

    def close(self):
        try: WIN.destroy()
        except Exception: pass


_GROWN = False   # the one-time 1px edge grow has been applied (see _grow_1px / _chrome_watcher)

def _open_maximized(api):
    """Window just appeared: apply the 1px edge grow to its normal rect (so Restore returns to a gap-free
    window), then fill the work area exactly as the caption button does, so the glyph/state stay in sync."""
    global _GROWN
    hwnd = _hwnd()
    if not hwnd:
        return
    if not _GROWN:
        try: _grow_1px(hwnd)
        except Exception: pass
        _GROWN = True
    if not api._max:
        api.toggle_maximize()
    try: WIN.evaluate_js("window.__eqSetMax && window.__eqSetMax(true)")   # caption glyph -> Restore
    except Exception: pass


def _webview_storage():
    """Folder for the window's saved data (portfolio, watchlists, preferences). It sits outside the install
    folder so it survives reinstalls and uninstalls. None falls back to pywebview's own default folder."""
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EQUIDEX", "webview")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def main():
    global WIN
    url = _start_server()
    try:
        import webview
        api = Api()
        WIN = webview.create_window(WINDOW_TITLE, url, width=1440, height=920, min_size=(960, 640),
                                    frameless=True, easy_drag=False, js_api=api)
        WIN.events.shown += lambda: _open_maximized(api)   # open filling the work area (taskbar stays visible)
        WIN.events.restored += _on_restored
        threading.Thread(target=_chrome_watcher, daemon=True).start()  # square corners + taskbar-aware icon
        # private_mode defaults to True, which discards localStorage when the window closes
        webview.start(private_mode=False, storage_path=_webview_storage())   # blocks until the window is closed, then the process exits
    except Exception:
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
