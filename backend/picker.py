"""Windows 原生目录选择框：让用户"点选"数据目录，而不是手打路径。

后端进程和用户在同一台机器、同一个桌面会话里（127.0.0.1 的本地服务），
所以弹原生对话框是可行的——浏览器网页本身拿不到真实文件路径，这一步
只能由本机进程代劳。取消返回空串。
"""
import os
import ctypes
from ctypes import wintypes

BIF_RETURNONLYFSDIRS = 0x00000001
BIF_EDITBOX = 0x00000010
BIF_NEWDIALOGSTYLE = 0x00000040

class BROWINFOW(ctypes.Structure):
    _fields_ = [("hwndOwner", wintypes.HWND),
                ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", wintypes.LPWSTR),
                ("lpszTitle", wintypes.LPCWSTR),
                ("ulFlags", wintypes.UINT),
                ("lpfn", wintypes.LPVOID),
                ("lParam", wintypes.LPARAM),
                ("iImage", ctypes.c_int)]

def pick_folder(title: str = "选择数据目录") -> str:
    """弹出原生目录选择框。确定返回完整路径，取消返回空串。

    COM 必须在使用它的线程上初始化；FastAPI 的每个请求跑在自己的线程里，
    进出配对初始化/反初始化即可。
    """
    if os.name != "nt":
        return ""
    ole32 = ctypes.oledll.ole32
    shell32 = ctypes.windll.shell32
    ole32.CoInitializeEx(None, 2)
    try:
        display = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        bi = BROWINFOW()
        bi.hwndOwner = None
        bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
        bi.lpszTitle = title
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
        path = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        ok = shell32.SHGetPathFromIDListW(pidl, path)
        ole32.CoTaskMemFree(pidl)
        return path.value if ok else ""
    except Exception:
        return ""
    finally:
        try:
            ole32.CoUninitialize()
        except Exception:
            pass
