"""Read-only Win32 window identity and physical-pixel geometry."""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from dataclasses import asdict, dataclass
import os


class WindowError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    process_path: str
    title: str
    class_name: str
    outer_x: int
    outer_y: int
    outer_width: int
    outer_height: int
    client_x: int
    client_y: int
    client_width: int
    client_height: int
    dpi: int
    foreground: bool
    minimized: bool

    @property
    def identity(self) -> str:
        return f"{self.pid}:{self.hwnd}:{self.process_path.casefold()}"

    def as_dict(self) -> dict:
        return {**asdict(self), "identity": self.identity}


def discover_window() -> WindowInfo:
    if os.name != "nt":
        raise WindowError("Live window capture requires Windows.")
    user = c.WinDLL("user32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
    user.GetClientRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
    user.ClientToScreen.argtypes = [w.HWND, c.POINTER(w.POINT)]
    user.GetDpiForWindow.argtypes = [w.HWND]
    user.GetDpiForWindow.restype = w.UINT
    user.IsWindowVisible.argtypes = [w.HWND]
    user.IsIconic.argtypes = [w.HWND]
    user.GetForegroundWindow.restype = w.HWND
    user.SetThreadDpiAwarenessContext.argtypes = [c.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = c.c_void_p
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    matches = []

    @callback_type
    def collect(hwnd, _):
        title = c.create_unicode_buffer(512)
        user.GetWindowTextW(hwnd, title, len(title))
        if title.value == "MapleStoryM" and user.IsWindowVisible(hwnd):
            matches.append(hwnd)
        return True

    user.EnumWindows(collect, 0)
    if len(matches) != 1:
        raise WindowError(f"Expected one visible MapleStoryM window, found {len(matches)}.")
    hwnd = matches[0]
    pid = w.DWORD()
    user.GetWindowThreadProcessId(hwnd, c.byref(pid))
    process = kernel.OpenProcess(0x1000, False, pid.value)
    if not process:
        raise WindowError("Cannot verify the selected window's executable.")
    try:
        path = c.create_unicode_buffer(32768)
        size = w.DWORD(len(path))
        if not kernel.QueryFullProcessImageNameW(process, 0, path, c.byref(size)):
            raise WindowError("Cannot read the selected window's executable identity.")
    finally:
        kernel.CloseHandle(process)
    class_name = c.create_unicode_buffer(256)
    user.GetClassNameW(hwnd, class_name, len(class_name))
    if os.path.basename(path.value).casefold() != "maplestorym.exe" or class_name.value != "UnityWndClass":
        raise WindowError("Window title matched but executable/class identity did not.")
    old_context = user.SetThreadDpiAwarenessContext(c.c_void_p(-4))
    if not old_context:
        raise WindowError("Cannot establish physical-pixel DPI awareness.")
    try:
        outer, client, origin = w.RECT(), w.RECT(), w.POINT(0, 0)
        if not (user.GetWindowRect(hwnd, c.byref(outer)) and user.GetClientRect(hwnd, c.byref(client)) and user.ClientToScreen(hwnd, c.byref(origin))):
            raise WindowError("Cannot determine game client geometry.")
        return WindowInfo(
            int(hwnd), int(pid.value), path.value, "MapleStoryM", class_name.value,
            outer.left, outer.top, outer.right - outer.left, outer.bottom - outer.top,
            origin.x, origin.y, client.right - client.left, client.bottom - client.top,
            int(user.GetDpiForWindow(hwnd)), user.GetForegroundWindow() == hwnd,
            bool(user.IsIconic(hwnd)),
        )
    finally:
        user.SetThreadDpiAwarenessContext(old_context)
