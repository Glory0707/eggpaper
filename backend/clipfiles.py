"""把文件送上 Windows 剪贴板（CF_HDROP 文件列表）。

文库「转发」的底层：本机应用够不着"替用户选哪个聊天"，文件列表上剪贴板是离
微信/QQ 聊天输入框最近的一步——用户到聊天框 Ctrl+V 即发。纯 ctypes 实现，
不引第三方依赖；非 Windows 明确报不支持（本产品当前只发 Windows 安装包）。
"""
import ctypes
import os
import time
from ctypes import wintypes

CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

# 64 位 Windows 上句柄是 64 位值：ctypes 的默认 restype 是 32 位 c_int，不显式声明
# 的话 GlobalAlloc 返回的句柄会被截断成废指针（GlobalLock 直接失败）。
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_shell32 = ctypes.windll.shell32     # DragQueryFileW 在 shell32（解析 HDROP 列表）
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]


class DROPFILES(ctypes.Structure):
    _fields_ = [("pFiles", wintypes.DWORD),      # 文件名区起始偏移 = 结构体大小
                ("pt", wintypes.POINT),          # 拖放点（不用，置 0）
                ("fNC", wintypes.BOOL),          # 非客户区标志（不用，置 0）
                ("fWide", wintypes.BOOL)]        # 1 = 文件名是 UTF-16


def _hdrop_blob(paths: list) -> bytes:
    """DROPFILES 头 + UTF-16 文件名列表（每个名字 NUL 结尾，再补一个 NUL 收尾）。"""
    hdr = DROPFILES(ctypes.sizeof(DROPFILES), wintypes.POINT(0, 0), 0, 1)
    return bytes(hdr) + ("\0".join(paths) + "\0\0").encode("utf-16-le")


def read_hdrop() -> list:
    """剪贴板上现存的文件列表（没有返回 []）。给测试做备份/恢复用。"""
    if os.name != "nt":
        return []
    if not _user32.OpenClipboard(None):
        return []
    try:
        h = _user32.GetClipboardData(CF_HDROP)
        if not h:
            return []
        p = _kernel32.GlobalLock(h)
        if not p:
            return []
        try:
            n = _shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
            out = []
            buf = ctypes.create_unicode_buffer(1024)
            for i in range(n):
                if _shell32.DragQueryFileW(h, i, buf, 1024):
                    out.append(buf.value)
            return out
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def copy_files(paths: list) -> tuple:
    """把一组文件放上剪贴板（CF_HDROP）。返回 (成功, 失败原因)。

    OpenClipboard 常被别的程序攥着（资源管理器里选中文件就占一下）——重试几拍
    再认输，别把偶发占用当成失败。SetClipboardData 成功后句柄归系统，不许 free。"""
    paths = [os.path.abspath(p) for p in paths if p]
    if not paths:
        return False, "没有可复制的文件"
    if os.name != "nt":
        return False, "此系统不支持复制文件到剪贴板"
    for p in paths:
        if not os.path.exists(p):
            return False, f"文件不在：{os.path.basename(p)}"
    blob = _hdrop_blob(paths)
    for _ in range(10):
        if not _user32.OpenClipboard(None):
            time.sleep(0.08)
            continue
        try:
            _user32.EmptyClipboard()
            h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(blob))
            if not h:
                return False, "内存分配失败"
            p = _kernel32.GlobalLock(h)
            if not p:
                _kernel32.GlobalFree(h)
                return False, "内存锁定失败"
            try:
                ctypes.memmove(p, blob, len(blob))
            finally:
                _kernel32.GlobalUnlock(h)
            if not _user32.SetClipboardData(CF_HDROP, h):
                _kernel32.GlobalFree(h)
                return False, "写剪贴板失败"
            return True, ""
        finally:
            _user32.CloseClipboard()
    return False, "剪贴板被占用"
