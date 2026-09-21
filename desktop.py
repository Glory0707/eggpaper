"""打包后的入口：起本地服务 → 打开浏览器。开发时不用它（`python backend/main.py`）。

带一个文件参数时（双击 PDF、右键「用 eggpaper 打开」）：`eggpaper.exe "D:/x.pdf"`，
先把这份 PDF 建进库，再打开界面。**应用已经开着的时候**不再起第二个进程——
把路径交给那个实例（HTTP 投递），界面下一次轮询就会切过去。

三个决定：

1. **默认浏览器；`--window` 给一个没有浏览器边框的独立窗口**。这个应用的交互
   （划词、Ctrl+F、缩放、打印、多标签）全长在浏览器上，所以窗口用系统自带的
   Edge/Chrome 的 **应用模式**（`--app=URL`）开——同样的引擎、同样的能力、自己的任务栏
   条目，**不额外背一个 WebView 运行时**。
2. **没有控制台窗口**（打包时 console=False）。所以任何启动失败都必须写进日志文件，
   路径 %LOCALAPPDATA%\\eggpaper\\logs\\app.log——设置面板里写了怎么找到它。
3. **单实例**。端口上已经有 eggpaper 在跑（用户双击了两次图标），就只打开浏览器，
   不再起第二个进程去抢同一份 SQLite 和文库。
"""
import ctypes
from ctypes import wintypes
import json
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser

HOST = "127.0.0.1"
PORTS = (8430, 8431, 8432)

def _setup_paths():
    """冻结后把自带的 backend 目录挂到 sys.path 上（模块都在那儿）。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
        be = os.path.join(base, "backend")
        if os.path.isdir(be):
            sys.path.insert(0, be)
    else:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

def _log_path() -> str:
    import appinfo
    d = os.path.join(os.path.dirname(appinfo.data_dir()), "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "app.log")

def _log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if not getattr(sys, "frozen", False):
        print(line)

def _dpi_aware():
    """先声明 DPI 感知，再问系统"托盘图标要多大"——否则问到的永远是 96 DPI 下的 16px，
    在 125%/150% 缩放的屏幕上被系统放大，看着就是糊的。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

def start_tray(url: str, port: int, log) -> bool:
    """托盘图标：打开界面 / 查更新 / 退出。打包版默认开——否则用户想关掉这个
    "后台服务"只能进设置里点，或者去任务管理器。"""
    try:
        import pystray
        from PIL import Image
    except ImportError as e:
        log(f"托盘不可用（{e}）")
        return False

    def tray_image():
        """托盘图标：**按 Windows 要的尺寸原生画**，不缩放。

        为什么不能画个 32px 交给它缩：pystray 的 Windows 后端会把这张图存成
        **单尺寸** ico，再用 LoadImage(LR_DEFAULTSIZE) 取系统小图标尺寸（100% 缩放下是
        16px）——32px 被硬缩到 16px，糊的根源就在这一步（源码里 serialized_image +
        LR_DEFAULTSIZE）。按目标尺寸画，1:1 落上去才清晰。
        """
        import mark
        try:
            size = ctypes.windll.user32.GetSystemMetrics(49) or 16
        except Exception:
            size = 16
        size = max(16, min(64, size))
        log(f"托盘图标按 {size}px 原生绘制")
        return mark.draw(size, tile=True)

    def guard(name, fn):
        """托盘菜单的回调在托盘线程里跑，抛出去的异常没人接——**用户看到的就是"点了没反应"**。
        所以每个回调都包一层：出错也写进日志，绝不静默。"""
        def wrapped(icon, item):
            try:
                fn(icon, item)
            except Exception:
                log(f"托盘菜单「{name}」出错：")
                log(traceback.format_exc())
        return wrapped

    def on_open(icon, item):
        _open(url, log)

    def on_window(icon, item):
        if not _open_window(url, log):
            _open(url, log)

    def on_check(icon, item):
        """查更新：有新版就弹个气泡，点「打开界面」就能升级。"""
        try:
            import httpx
            r = httpx.get(f"http://{HOST}:{port}/api/update/check?force=1", timeout=15).json()
            if r.get("has_update"):
                icon.notify(f"有新版本 {r['latest']}（当前 {r['current']}），打开界面即可更新", "eggpaper")
                _open(url, log)
            elif r.get("ok"):
                icon.notify(f"已经是最新的（{r['current']}）", "eggpaper")
            else:
                icon.notify("没读到更新源：设置里填一个地址", "eggpaper")
        except Exception as e:
            log(f"查更新失败：{e}")

    def on_quit(icon, item):
        log("从托盘退出")
        # 必须走 /api/quit，不能 icon.stop()+os._exit 硬杀：那样 _QUITTING 不会置位，
        # 所有开着的网页/独立窗口（3 秒轮询 open-request）等不到 quitting，
        # 只能对着 ERR_CONNECTION_REFUSED 发呆。让位升级（reason=upgrade）不走这里。
        graceful = False
        try:
            import httpx
            r = httpx.post(f"http://{HOST}:{port}/api/quit",
                           json={"reason": "user"}, timeout=5).json()
            graceful = bool(r.get("ok"))
        except Exception as e:
            log(f"请求后端退出失败：{e}")
        icon.stop()
        if not graceful:
            os._exit(0)     # 开发模式 /api/quit 不接管：托盘自己收场
        # 打包版：/api/quit 已广播 quitting，宽限一拍后才整进程退出——
        # 这一拍就是留给所有开着的页面自行关闭的，这里千万别抢跑 os._exit

    icon = pystray.Icon("eggpaper", tray_image(), "eggpaper", menu=pystray.Menu(
        pystray.MenuItem("打开界面", guard("打开界面", on_open), default=True),
        pystray.MenuItem("在独立窗口打开", guard("在独立窗口打开", on_window)),
        pystray.MenuItem("检查更新", guard("检查更新", on_check)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 eggpaper", guard("退出 eggpaper", on_quit)),
    ))
    threading.Thread(target=icon.run, daemon=True).start()
    log("托盘图标已就绪")
    return True

def _window_only_mode(url: str) -> int:
    """`--window-only`：本进程只开一个窗口（独立进程，不干扰服务进程）。"""
    import window as winmod
    return winmod.run_window_only(url)

def _open_window(url: str, log) -> bool:
    """独立窗口：实现放在 backend/window.py（界面里那颗「在独立窗口打开」走同一份）。

    这里只负责"开 + 记日志"。**这个函数曾经被我从文件里删掉过而没人发现**：
    托盘菜单回调里抛的异常当时是被静默吞掉的，用户点「在独立窗口打开」就是"没反应"，
    日志里一个字都没有。现在托盘回调有守卫（出错必写日志），这种缺失不会再无声无息。
    """
    try:
        import window as winmod
    except Exception:
        log("独立窗口不可用（模块没打进包）：")
        log(traceback.format_exc())
        return False
    how = winmod.open_window(url)
    log(f"独立窗口：{how}" if how else "没找到可用的浏览器（Edge/Chrome），退回默认浏览器")
    return bool(how)

def _open(url: str, log):
    """打开浏览器。EGGPAPER_NO_BROWSER=1 时只打印地址——无头机器、CI、
    以及"我就想看看服务起没起来"的时候用得上。"""
    if os.environ.get("EGGPAPER_NO_BROWSER"):
        log(f"（EGGPAPER_NO_BROWSER=1，不打开浏览器）{url}")
        return
    webbrowser.open(url)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

def _alive(pid: int) -> bool:
    """这个 pid 还活着吗。

    **不要用 `os.kill(pid, 0)`**：Windows 上它等价于 TerminateProcess(handle, 0)——
    不是"探活"，是真把那个进程干掉。用 OpenProcess + WaitForSingleObject 看它还在不在。
    """
    WAIT_TIMEOUT = 0x00000102
    PROCESS_SYNCHRONIZE = 0x00100000
    h = _kernel32.OpenProcess(PROCESS_SYNCHRONIZE, False, pid)
    if not h:
        return False
    try:
        return _kernel32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        _kernel32.CloseHandle(h)

def _instance_file() -> str:
    import appinfo
    return os.path.join(os.path.dirname(appinfo.data_dir()), "instance.json")

def _running_instance():
    """已经有一个 eggpaper 在跑吗？有就返回 (端口, 它记下的版本号)。

    读的是它自己写的 instance.json（含 pid），再确认那个 pid 还活着。
    **不用"连一下端口试试"那种判断**：实测在有的机器上，进程连自己的
    127.0.0.1 会卡在 SYN_SENT（链路被丢包而不是被拒绝），于是"明明在跑却探不到"。
    pid 加文件锁是本地操作，不受这条链路影响。
    """
    try:
        with open(_instance_file(), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    pid = int(info.get("pid") or 0)
    if pid <= 0 or pid == os.getpid():
        return None
    if not _alive(pid):
        return None
    port = int(info.get("port") or 0) or None
    if not port:
        return None
    return port, str(info.get("version") or "")

def _ver_tuple(v: str):
    out = []
    for part in str(v or "").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0, 0, 0])[:4]

def _ask_quit(port: int) -> bool:
    """请那个实例退出（它自己有 /api/quit）。升级接管的让位不通知页面关窗——
    旧页面要留给版本轮询自动刷新到新实例。"""
    try:
        import httpx
        r = httpx.post(f"http://{HOST}:{port}/api/quit",
                       json={"reason": "upgrade"}, timeout=5)
        return r.status_code == 200
    except Exception as e:
        _log(f"请旧实例退出失败：{type(e).__name__}: {e}")
        return False

def _port_free(port: int) -> bool:
    """这个端口能不能绑上。用 bind 而不是 connect——绑定是本机操作，
    不会像"回连自己"那样在某些机器上卡住。"""
    with socket.socket() as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False

def _write_instance(port: int):
    try:
        with open(_instance_file(), "w", encoding="utf-8") as f:
            json.dump({"port": port, "pid": os.getpid(),
                       "version": __import__("appinfo").version()}, f)
    except OSError as e:
        _log(f"写 instance.json 失败：{e}")

def _pdf_arg(argv) -> str:
    """命令行里那一个 PDF 路径（双击/右键菜单传进来的 %1）。没有就返回空串。"""
    for a in argv[1:]:
        a = a.strip().strip('"')
        if a.lower().endswith(".pdf") and os.path.isfile(a):
            return a
    return ""

def _handoff(port: int, pdf: str) -> bool:
    """把"要打开的文件"交给已经开着的那个实例：它自己会切过去。"""
    try:
        import httpx
        r = httpx.post(f"http://{HOST}:{port}/api/papers/import-path",
                       json={"path": pdf}, timeout=180)
        return r.status_code == 200
    except Exception as e:
        _log(f"投递给已有实例失败：{type(e).__name__}: {e}")
        return False

def _install_crash_log(log):
    """未捕获的异常一律写日志。

    打包版（console=False）没有 stderr，崩溃就是"窗口闪一下/什么都没发生"，
    连一句原因都留不下——排查只能靠猜。主线程和子线程都挂上。
    """
    def hook(exc_type, exc, tb):
        # 头和栈合成一次写：分两行的话，进程若在两写之间死掉，日志就只剩
        # 一个没有内容的头（2026-09-21 实测发生过）
        log("未捕获的异常：\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.excepthook = hook
    try:
        threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass

def main():
    _setup_paths()
    _dpi_aware()
    if "--window-only" in sys.argv:
        port = _running_instance() or PORTS[0]
        return _window_only_mode(f"http://{HOST}:{port}/")
    import appinfo
    log = _log
    _install_crash_log(log)
    log(f"启动 eggpaper {appinfo.version()}（packaged={appinfo.is_frozen()}）")

    pdf = _pdf_arg(sys.argv)
    want_window = "--window" in sys.argv
    want_tray = "--no-tray" not in sys.argv

    running = _running_instance()
    if running:
        old_port, old_ver = running
        if old_ver and _ver_tuple(old_ver) < _ver_tuple(appinfo.version()):
            log(f"发现旧版本 {old_ver} 还在跑（端口 {old_port}），请它退出，这次用 {appinfo.version()}")
            _ask_quit(old_port)
            for _ in range(24):
                if _port_free(old_port):
                    break
                time.sleep(0.5)
            else:
                log(f"旧版本没退出（端口 {old_port} 仍被占）——在托盘菜单里点「退出 eggpaper」再打开一次")
            running = _running_instance()
    if running:
        port = running[0]
        log(f"已有一个 eggpaper 在跑（端口 {port}），打开界面即可")
        if pdf:
            log(f"把这份 PDF 交给它：{pdf}")
            _handoff(port, pdf)
        if want_window and _open_window(f"http://{HOST}:{port}/", log):
            return 0
        _open(f"http://{HOST}:{port}/", log)
        return 0

    port = next((p for p in PORTS if _port_free(p)), None)
    if port is None:
        log("没有可用端口（8430-8432 都被占），无法启动")
        return 1

    try:
        import main as backend
    except Exception:
        log("后端模块导入失败：")
        log(traceback.format_exc())
        return 1
    def _serve():
        try:
            backend.serve(port=port, log_level="warning")
        except Exception:
            log("服务线程挂了：")
            log(traceback.format_exc())

    threading.Thread(target=_serve, daemon=True).start()

    url = f"http://{HOST}:{port}/"
    if not backend.READY.wait(45):
        log(f"服务 45 秒内没起来（端口 {port}）——如果上面没有别的错，把这份日志发给作者")
        return 1
    if _port_free(port):
        log(f"服务报告已启动，但 {port} 还是空的——端口可能被别的程序抢了，重开一次即可")
        return 1
    _write_instance(port)
    log(f"服务已就绪：{url}")
    if pdf:
        _handoff(port, pdf)
    if want_tray:
        start_tray(url, port, log)
    if want_window and _open_window(url, log):
        pass
    else:
        _open(url, log)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    sys.exit(main())
