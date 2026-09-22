"""把界面开成一个"没有浏览器边框"的独立窗口。

放在 backend/ 里是为了三处共用：`desktop.py`（启动时按 `--window` 走）、
`--window-only`（单开一个窗口进程）、以及 `POST /api/window`（界面里那颗按钮）。

**用 Edge/Chrome 的应用模式**（`--app=URL`）：Windows 上 WebView2 的运行时提供者就是
Edge——同一个引擎，不用往安装包里塞几十兆，也没有额外的冻结风险。窗口没有地址栏、
没有标签页，任务栏里就是 eggpaper 自己。

**一条试过但放弃的路**：原生窗口（pywebview + pythonnet）能让任务栏按钮显示我们的图标，
但在**打包环境里 `import webview` 就卡住**——pythonnet 加载 .NET 运行时，握着 GIL 不撒手，
连"10 秒没出来就退回应用模式"这种兜底计时都跑不到（主线程要 GIL 才能跑 Python 回调）。
一个会僵住的窗口不值得为了一个任务栏图标去换，所以这条路撤掉了，留这段话防止再试一遍。

**关于任务栏/标题栏图标**（这是整条链路里最绕的一个，结论务必看全）：
Chromium 给应用模式窗口的图标取自 favicon，而且按**物理像素**挑档位再放大——150% 缩放下
取 24px 当逻辑 24，再放大到 36/48 物理像素，源头小，怎么画都软；`--app-icon` 开关实测无效
（红方块 A/B：任务栏零变化）；内联 SVG favicon 更糟，会被栅格化成很小的位图再放大。
favicon 那一侧我们仍按每档 DPI 的精确尺寸给足原图（见 `tools/make_icon.py` 的 ICO_SIZES 与
`frontend/index.html` 的 `?v=`），但真正让任务栏达到**桌面快捷方式同级清晰度**的是
`_give_window_icon()`：窗口拉起后，从进程外用 `WM_SETICON` 把按窗口 DPI 现画的精确像素位图
（`mark.draw` + `CreateIconIndirect`，不再经 LoadImage 选档）注入进去——Windows 11 任务栏
读的是 `ICON_SMALL2`，给它满物理尺寸（150% 下 48）即 1:1。favicon 加载后会把图标重设回去，
所以后台线程反复压约 20 秒，歇 10 秒再补一拍（合计约 30 秒跨度的压制）。
"""
import os
import subprocess
import threading
import time
import traceback

CANDIDATES = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)

def browser_exe() -> str:
    for p in CANDIDATES:
        p = os.path.expandvars(p)
        if os.path.isfile(p):
            return p
    return ""

def icon_path() -> str:
    """随程序分发的多尺寸 ICO（桌面快捷方式/exe 用的是同一份）。"""
    import appinfo
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for p in (os.path.join(appinfo.res_dir(), "eggpaper.ico"),
              os.path.join(root, "installer", "eggpaper.ico")):
        if os.path.isfile(p):
            return p
    return ""

def _hicon_from_pil(img):
    """一张 RGBA 位图 → HICON（32bpp 带 alpha）。

    为什么不用 LoadImage 从多尺寸 ICO 里取：实测它按"最近档"选图，ICO 里同时有 42/48/60
    时，请求 48 也可能给你一张 60 再缩到 48——任务栏槽位明明是 48 物理像素（红方块标定），
    一旦被重采样，边缘就发软。这里按调用方给的**精确像素**现画一张，CreateIconIndirect
    1:1 交给窗口，绝不再缩放。
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    gdi = ctypes.windll.gdi32
    gdi.CreateBitmap.restype = ctypes.c_void_p
    gdi.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                                 wintypes.UINT, ctypes.c_void_p]
    gdi.DeleteObject.argtypes = [ctypes.c_void_p]

    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    buf = bytearray()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            buf += bytes((b, g, r, a))
    hcolor = gdi.CreateBitmap(w, h, 1, 32, bytes(buf))
    row = ((w + 31) // 32) * 4
    hmask = gdi.CreateBitmap(w, h, 1, 1, bytes(row * h))

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", ctypes.c_void_p),
                    ("hbmColor", ctypes.c_void_p)]

    user32.CreateIconIndirect.restype = ctypes.c_void_p
    user32.CreateIconIndirect.argtypes = [ctypes.c_void_p]
    hicon = user32.CreateIconIndirect(ctypes.byref(ICONINFO(1, 0, 0, hmask, hcolor)))
    gdi.DeleteObject(hcolor)
    gdi.DeleteObject(hmask)
    return hicon or None

def _give_window_icon(pid: int):
    """**从外部把我们的多尺寸 ICO 塞进 Edge 应用模式窗口**（任务栏 + 标题栏图标）。

    为什么必须走这一步（彩色指纹实验量出来的，不是猜的）：应用模式窗口的任务栏图标
    Chromium 取自 favicon.ico，而且按**物理像素**挑档位——150% 缩放下它取 24px 那一档，
    再被 Windows 当逻辑 24 放大 1.5 倍到 36 物理像素。源头位图偏小，怎么画都软；
    `--app-icon` 开关实测无效（红方块 A/B：任务栏零变化）。
    原生 WM_SETICON 没有这层缩放：下面**按窗口 DPI 现画**精确像素的位图（mark.draw +
    CreateIconIndirect，见 `_hicon_from_pil`；多尺寸 ICO 的 LoadImage 只留作现画失败的退路），
    于是任务栏拿到的和桌面快捷方式是**同一份、按物理尺寸 1:1 的位图**——这就是用户认可的那枚。

    在后台线程里做：窗口刚出现时 favicon 还没加载完，Chromium 会在加载后把图标重设回
    favicon，所以要反复压 ~20 秒。本地是单页应用，之后不再导航，图标也就稳定了。
    """
    def work():
        try:
            import ctypes
            from ctypes import wintypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except OSError:
                pass
            u = ctypes.windll.user32
            u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                                         wintypes.LPARAM), wintypes.LPARAM]
            u.GetWindowTextLengthW.restype = ctypes.c_int
            u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            u.SendMessageW.restype = ctypes.c_void_p
            u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                       ctypes.c_void_p]
            u.GetDpiForWindow.restype = wintypes.UINT
            u.GetDpiForWindow.argtypes = [wintypes.HWND]
            u.GetSystemMetricsForDpi.restype = ctypes.c_int
            u.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT, wintypes.HWND]
            li = u.LoadImageW
            li.restype = ctypes.c_void_p
            li.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                           ctypes.c_int, ctypes.c_int, wintypes.UINT]

            ico = icon_path()
            WM_SETICON, IMAGE_ICON, LR_LOADFROMFILE = 0x0080, 1, 0x10
            SM_CXICON, SM_CXSMICON = 11, 49
            CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def find_hwnds(timeout=20):
                """所有标题为 "eggpaper" 的可见窗口（exact=窗口进程就是本进程起的）。
                **收集全部而不是挑一个**：Edge/Chrome 已在跑时 --app 会挂到已有进程上、
                Popen 那个 pid 立刻退场，exact 落空后 titled 里往往不止一个候选
                （用户开着多个窗口、测试窗口也在）——全部注入才不会漏。"""
                deadline = time.time() + timeout
                while time.time() < deadline:
                    exact, titled = [], []

                    def cb(hwnd, _):
                        if not u.IsWindowVisible(hwnd):
                            return True
                        n = u.GetWindowTextLengthW(hwnd)
                        if not n:
                            return True
                        b = ctypes.create_unicode_buffer(n + 1)
                        u.GetWindowTextW(hwnd, b, n + 1)
                        if b.value.strip().lower() != "eggpaper":
                            return True
                        wp = wintypes.DWORD()
                        u.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
                        (exact if wp.value == pid else titled).append(hwnd)
                        return True

                    u.EnumWindows(CB(cb), 0)
                    if exact or titled:
                        return exact + titled
                    time.sleep(0.5)
                return []

            hwnds = find_hwnds()
            if not hwnds:
                _window_log("注入窗口图标：没找到独立窗口，放弃")
                return

            def metrics(idx):
                try:
                    return u.GetSystemMetricsForDpi(idx, u.GetDpiForWindow(hwnds[0]), hwnds[0])
                except OSError:
                    return u.GetSystemMetrics(idx)

            bx, sx = metrics(SM_CXICON), metrics(SM_CXSMICON)
            hbig = hsmall = 0
            try:
                import mark
                hbig = _hicon_from_pil(mark.draw(bx, tile=True))
                hsmall = _hicon_from_pil(mark.draw(sx, tile=True))
            except Exception:
                _window_log("现画窗口图标失败，退回 LoadImage：\n" + traceback.format_exc())
            if not hbig:
                ico = icon_path()
                hbig = li(0, ico, IMAGE_ICON, bx, bx, LR_LOADFROMFILE) if ico else 0
            if not hsmall:
                ico = ico or icon_path()
                hsmall = li(0, ico, IMAGE_ICON, sx, sx, LR_LOADFROMFILE) if ico else 0
            if not hbig and not hsmall:
                _window_log("注入窗口图标：拿不到任何 HICON")
                return

            def apply():
                for h in hwnds:
                    if hbig:
                        u.SendMessageW(h, WM_SETICON, 1, hbig)
                    if hsmall:
                        u.SendMessageW(h, WM_SETICON, 0, hsmall)
                    if hbig:
                        u.SendMessageW(h, WM_SETICON, 2, hbig)

            for _ in range(27):
                apply()
                time.sleep(0.75)
            time.sleep(10)
            apply()
            _window_log(f"窗口图标已按物理像素注入（大 {bx}px / 小 {sx}px，任务栏用大图档，{len(hwnds)} 个窗口）")
        except Exception:
            _window_log("注入窗口图标出错：\n" + traceback.format_exc())

    threading.Thread(target=work, daemon=True).start()

_last_proc = None

# ---- 图标守护 ---------------------------------------------------------------
# 注入只在 open_window 起的那只窗口上有 30 秒压制；用户在浏览器里手动新开的
# eggpaper 窗口（Ctrl+N、复制窗口）、favicon 加载失败或晚到的窗口，任务栏图标会
# 掉回 Chromium 的 favicon 档甚至默认纸页图标——多只窗口同组时还会互相污染。
# 守护线程常驻：每 15 秒把所有 eggpaper 顶层窗口的图标重钉一次（SetIcon 幂等、
# 开销可忽略），任务栏从此钉死在按窗口 DPI 现画的清晰位图上，不再看 Chromium 心情。

_icon_cache = {}          # dpi -> (hbig, hsmall)
_guard_started = False

def _pin_all():
    """枚举标题恰好是 "eggpaper" 的可见顶层窗口，逐一重钉图标。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    hwnds = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if not u.IsWindowVisible(h) or u.IsIconic(h):
            return True
        n = u.GetWindowTextLengthW(h)
        if not n:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        if b.value.strip() == "eggpaper":
            hwnds.append(h)
        return True

    u.EnumWindows(CB(cb), 0)
    if not hwnds:
        return
    for h in hwnds:
        dpi = 96
        try:
            dpi = u.GetDpiForWindow(h)
        except Exception:
            pass
        if dpi not in _icon_cache:
            _icon_cache[dpi] = _make_icon_pair(dpi, h)
        hbig, hsmall = _icon_cache[dpi]
        if hbig:
            u.SendMessageW(h, 0x0080, 1, hbig)     # WM_SETICON ICON_BIG
        if hsmall:
            u.SendMessageW(h, 0x0080, 0, hsmall)   # WM_SETICON ICON_SMALL
        if hbig:
            u.SendMessageW(h, 0x0080, 2, hbig)     # ICON_BIG2：任务栏读的是这一档

def _make_icon_pair(dpi: int, hwnd: int):
    """按窗口 DPI 现画大小两枚 HICON（白底圆角卡片 + 三杠蛋，mark.draw 同一份几何）。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        try:
            bx = u.GetSystemMetricsForDpi(11, dpi, hwnd)    # SM_CXICON
            sx = u.GetSystemMetricsForDpi(49, dpi, hwnd)    # SM_CXSMICON
        except Exception:
            bx, sx = u.GetSystemMetrics(11), u.GetSystemMetrics(49)
        bx, sx = max(16, bx or 32), max(16, sx or 16)
        import mark
        hbig = _hicon_from_pil(mark.draw(bx, tile=True))
        hsmall = _hicon_from_pil(mark.draw(sx, tile=True))
        if hbig or hsmall:
            _window_log(f"守护现画图标：大 {bx}px / 小 {sx}px（dpi {dpi}）")
        return hbig or None, hsmall or None
    except Exception:
        _window_log("守护现画图标失败：\n" + traceback.format_exc())
        return None, None

def start_icon_guard():
    global _guard_started
    if _guard_started or os.name != "nt":
        return
    _guard_started = True

    def work():
        while True:
            try:
                _pin_all()
            except Exception:
                _window_log("图标守护出错：\n" + traceback.format_exc())
            time.sleep(15)

    threading.Thread(target=work, daemon=True).start()
    _window_log("图标守护已就位（15s 周期）")

def open_window(url: str, size=(1440, 940)) -> str:
    """开一个独立窗口。返回用了哪种方式（给日志/界面提示用），失败返回空串。"""
    global _last_proc
    exe = browser_exe()
    if not exe:
        return ""
    try:
        _last_proc = subprocess.Popen([exe, f"--app={url}",
                                       f"--window-size={size[0]},{size[1]}"],
                                      close_fds=True)
    except OSError:
        return ""
    _give_window_icon(_last_proc.pid)
    return os.path.basename(exe).replace(".exe", "") + " 应用模式"

def _window_log(msg: str):
    """窗口进程是 console=False 的，print 到不了任何地方——失败只能靠日志说话。"""
    try:
        import time
        import appinfo
        d = os.path.join(os.path.dirname(appinfo.data_dir()), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "app.log"), "a", encoding="utf-8") as f:
            f.write("[%s] [window] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass

def run_window_only(url: str) -> int:
    """`--window-only`：只开一个窗口（独立进程，不干扰服务进程）。"""
    _window_log(f"窗口进程启动，目标 {url}")
    if open_window(url):
        return 0
    _window_log("没找到可用的浏览器（Edge/Chrome），开不出窗口")
    return 1
