"""タスクバーでの見え方（Windows）。

このアプリは Microsoft Store 版の Python（パッケージのアプリ）で動くので、プロセスに AppUserModelID を付けても
タスクバーは Python のアイコンでまとめてしまう。ウィンドウそのものに AppUserModelID と「ピン留めしたときの起動方法・アイコン」を付けると、
Python とは別のアプリとして、ウィンドウのアイコン（assets/icon.ico）で並ぶ。
"""
from __future__ import annotations

import ctypes
import logging
import uuid
from ctypes import POINTER, Structure, byref, c_ubyte, c_ulong, c_ushort, c_void_p, c_wchar_p

log = logging.getLogger("djmix")


class GUID(Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    @classmethod
    def of(cls, s: str) -> "GUID":
        u = uuid.UUID(s)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        g.Data4[:] = list(u.bytes[8:])
        return g


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(Structure):          # VT_LPWSTR だけ使う（x64 で 24 バイト）
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("pwszVal", c_wchar_p), ("pad", c_void_p)]


IID_IPropertyStore = GUID.of("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
FMTID_AUMID = "9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"
PKEY = {"RelaunchCommand": 2, "RelaunchIconResource": 3, "RelaunchDisplayNameResource": 4, "ID": 5}
VT_LPWSTR = 31


def set_window_app_id(hwnd: int, app_id: str, relaunch: str | None = None, icon: str | None = None, name: str | None = None) -> bool:
    """ウィンドウに AppUserModelID（と、ピン留めしたときの起動コマンド・アイコン・名前）を付ける。"""
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHGetPropertyStoreForWindow.argtypes = [c_void_p, POINTER(GUID), POINTER(c_void_p)]
        store = c_void_p()
        hr = shell32.SHGetPropertyStoreForWindow(c_void_p(hwnd), byref(IID_IPropertyStore), byref(store))
        if hr != 0 or not store:
            return False
        vtbl = ctypes.cast(ctypes.cast(store, POINTER(c_void_p))[0], POINTER(c_void_p))
        SetValue = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(vtbl[6])
        Commit = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p)(vtbl[7])
        Release = ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])
        vals = [("RelaunchCommand", relaunch), ("RelaunchIconResource", icon), ("RelaunchDisplayNameResource", name), ("ID", app_id)]
        ok = True
        for key, v in vals:                 # ID は最後に（起動方法がそろってから切り替える）
            if v is None:
                continue
            pk = PROPERTYKEY(GUID.of(FMTID_AUMID), PKEY[key])
            pv = PROPVARIANT(VT_LPWSTR, 0, 0, 0, v, None)
            ok &= SetValue(store, byref(pk), byref(pv)) == 0
        Commit(store)
        Release(store)
        return ok
    except Exception:
        log.exception("set_window_app_id")
        return False


def find_own_window(title: str) -> int | None:
    """このプロセスの、タイトルが title の見えているトップレベルウィンドウ。"""
    import os
    from ctypes import wintypes
    u = ctypes.windll.user32
    pid = os.getpid()
    found = []
    buf = ctypes.create_unicode_buffer(512)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, byref(p))
        if p.value == pid and u.IsWindowVisible(h):
            u.GetWindowTextW(h, buf, 512)
            if buf.value == title:
                found.append(h)
                return False
        return True
    u.EnumWindows(cb, 0)
    return found[0] if found else None
