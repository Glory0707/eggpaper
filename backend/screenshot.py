"""应用内截图：把 eggpaper 窗口当前画面原样抓成 PNG。

窗口像素从屏幕上直接抓（PIL.ImageGrab）——所见即所得，页边笔迹的混合模式、
护眼底纹这些渲染效果和用户看到的分毫不差。为什么不走 CDP/浏览器自截：
--app 窗口挂在用户已在跑的 Edge/Chrome 进程上时调试端口不生效，专属
user-data-dir 又会把 localStorage（阅读位置、语言、蛋）整体丢掉，都不如
"抓屏幕上那块窗口"诚实。窗口在屏幕上被遮挡的部分也会如实截到——截图时
用户正看着它，这就是"以当前视图为准"。

认两种窗口：标题恰好 "eggpaper" 的独立窗口，和 "eggpaper - Microsoft Edge"
这类浏览器标签页窗口（活动标签正停在 eggpaper 上——页面不活跃时按键根本
到不了 onKey，所以命中即正确）。都没有就是最小化了，明确报错。
"""
import base64
import io
import os
import re
import time


def folder() -> str:
    """截图落盘目录：数据目录下的 screenshots/。"""
    import config
    return os.path.join(config.DATA_DIR, "screenshots")


def capture_window() -> bytes:
    """抓当前 eggpaper 独立窗口的可见区域，返回 PNG 字节。

    找不到窗口（普通浏览器标签页）或窗口最小化时抛 ValueError——那是给
    界面上的提示文案，不是堆栈。
    """
    import ctypes
    from ctypes import wintypes
    if os.name != "nt":
        raise ValueError("截图目前只在 Windows 上可用")
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 物理像素；进程级，重复调用静默失败没关系
    except Exception:
        pass
    u = ctypes.windll.user32

    hits = []                     # (hwnd, iconic)：Z 序记录所有候选
    visible = []                  # 顺带记录可见窗口标题：找不到时报错能直接对答案
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if len(visible) >= 14 or not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, b, n + 1)
        t = b.value.strip()
        if t and len(visible) < 14:
            visible.append(t)
        if "eggpaper" in t.lower() and len(hits) < 3:
            hits.append((hwnd, bool(u.IsIconic(hwnd))))
        return True

    u.EnumWindows(CB(cb), 0)
    if not hits:
        hint = " / ".join(visible[:6]) or "(桌面没有任何可见窗口)"
        raise ValueError(f"没找到 eggpaper 的窗口——窗口还在吗？当前可见的窗口有：{hint}")
    hwnd, iconic = next(((h, i) for h, i in hits if not i), hits[0])
    if iconic:                    # 最小化的窗口在屏幕上没有像素可抓：先还原到前台
        u.ShowWindow(hwnd, 9)     # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        time.sleep(0.35)
    best = hwnd

    rect = wintypes.RECT()
    # GetWindowRect 带不可见的 resize 边（Win10/11 约 7px），DWM 的扩展边界才是看得见的框
    r = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        best, 9, ctypes.byref(rect), ctypes.sizeof(rect))   # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
    if r != 0:
        u.GetWindowRect(best, ctypes.byref(rect))
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


_BAD = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")

def build_name(title: str, page: int) -> str:
    """统一命名：egg_20260919-154530_标题片段_p3.png——按时间排、看名字知出处。
    标题清洗成文件系统安全的短横线片段（≤24 字符），没有就省略；页码同理。"""
    parts = ["egg_" + time.strftime("%Y%m%d-%H%M%S")]
    slug = _BAD.sub("_", (title or "").strip()).strip("_")[:24]
    if slug:
        parts.append(slug)
    try:
        if int(page) > 0:
            parts.append(f"p{int(page)}")
    except (TypeError, ValueError):
        pass
    return "_".join(parts) + ".png"


def save(png: bytes, title: str, page: int) -> str:
    """落盘并返回文件名；同一秒截两张时后一张补 -2。"""
    d = folder()
    os.makedirs(d, exist_ok=True)
    name = build_name(title, page)
    stem, ext = os.path.splitext(name)
    cand, i = name, 1
    while os.path.exists(os.path.join(d, cand)):
        i += 1
        cand = f"{stem}-{i}{ext}"
    with open(os.path.join(d, cand), "wb") as f:
        f.write(png)
    return cand


def crop_viewport(png: bytes, chrome_top: float, border: float, dpr: float) -> bytes:
    """窗口截图 → 纯视口区域：前端量好的浏览器镶边（标题栏 + 边框，CSS 像素）×
    dpr 裁掉，选区坐标从此和 client 坐标一一对应。"""
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    d = max(1.0, dpr or 1)
    t = max(0, round((chrome_top or 0) * d))
    b = max(0, round((border or 0) * d))
    img = img.crop((b, t, max(b + 1, img.width - b), max(t + 1, img.height - b)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def to_base64(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")
