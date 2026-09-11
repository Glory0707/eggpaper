"""打包后的入口：起本地服务 → 打开浏览器。开发时不用它（`python backend/main.py`）。

带一个文件参数时（双击 PDF、右键「用 eggpaper 打开」）：`eggpaper.exe "D:/x.pdf"`，
先把这份 PDF 建进库，再打开界面。**应用已经开着的时候**不再起第二个进程——
把路径交给那个实例（HTTP 投递），界面下一次轮询就会切过去。

三个决定：

1. **默认浏览器；`--window` 给一个没有浏览器边框的独立窗口**。这个应用的交互
   （划词、Ctrl+F、缩放、打印、多标签）全长在浏览器上，所以窗口用系统自带的
   Edge/Chrome 的 **应用模式**（`--app=URL`）开——同样的引擎、同样的能力、自己的任务栏
   条目，**不额外背一个 WebView 运行时**。装了 pywebview 的话优先用它（真内嵌窗口）。
2. **没有控制台窗口**（打包时 console=False）。所以任何启动失败都必须写进日志文件，
   路径 %LOCALAPPDATA%\\eggpaper\\logs\\app.log——设置面板里写了怎么找到它。
3. **单实例**。端口上已经有 eggpaper 在跑（用户双击了两次图标），就只打开浏览器，
   不再起第二个进程去抢同一份 SQLite 和文库。
"""
import os
import socket
import subprocess
import sys
import threading
import time
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


def start_tray(url: str, port: int, log) -> bool:
    """托盘图标：打开界面 / 查更新 / 退出。打包版默认开——否则用户想关掉这个
    "后台服务"只能进设置里点，或者去任务管理器。"""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as e:
        log(f"托盘不可用（{e}）")
        return False

    def mark(size=64):
        """托盘图标：和界面里那枚印章同一个形状（椭圆环 + 三行字条）。"""
        ss, im = 4, None
        im = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        k = size * ss / 96
        d.ellipse([18 * k, 14 * k, 78 * k, 82 * k], outline=(29, 78, 95), width=max(2, round(8 * k)))
        for y, w in ((32, 30), (45, 36), (58, 20)):
            d.rounded_rectangle([(48 - w / 2) * k, (y - 4) * k, (48 + w / 2) * k, (y + 4) * k],
                                radius=4 * k, fill=(29, 78, 95))
        return im.resize((size, size), Image.LANCZOS)

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
        icon.stop()
        os._exit(0)

    icon = pystray.Icon("eggpaper", mark(), "eggpaper", menu=pystray.Menu(
        pystray.MenuItem("打开界面", on_open, default=True),
        pystray.MenuItem("在独立窗口打开", on_window),
        pystray.MenuItem("检查更新", on_check),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 eggpaper", on_quit),
    ))
    threading.Thread(target=icon.run, daemon=True).start()
    log("托盘图标已就绪")
    return True


def _open(url: str, log):
    """打开浏览器。EGGPAPER_NO_BROWSER=1 时只打印地址——无头机器、CI、
    以及"我就想看看服务起没起来"的时候用得上。"""
    if os.environ.get("EGGPAPER_NO_BROWSER"):
        log(f"（EGGPAPER_NO_BROWSER=1，不打开浏览器）{url}")
        return
    webbrowser.open(url)


def _serving(port: int) -> str:
    """这个端口上已经跑着 eggpaper 吗？是就返回它的版本号。"""
    try:
        import httpx
        r = httpx.get(f"http://{HOST}:{port}/api/version", timeout=1.2)
        return str(r.json().get("version") or "") if r.status_code == 200 else ""
    except Exception:
        return ""


def _free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, port)) != 0


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


def main():
    _setup_paths()
    import appinfo
    log = _log
    log(f"启动 eggpaper {appinfo.version()}（packaged={appinfo.is_frozen()}）")

    pdf = _pdf_arg(sys.argv)
    want_window = "--window" in sys.argv
    want_tray = "--no-tray" not in sys.argv

    for port in PORTS:                       # 已经有实例在跑：只开浏览器
        v = _serving(port)
        if v:
            log(f"{port} 上已有一个实例（{v}），打开浏览器即可")
            if pdf:
                log(f"把这份 PDF 交给它：{pdf}")
                _handoff(port, pdf)
            if want_window and _open_window(f"http://{HOST}:{port}/", log):
                return 0
            _open(f"http://{HOST}:{port}/", log)
            return 0

    port = next((p for p in PORTS if _free(p)), None)
    if port is None:
        log("没有可用端口（8430-8432 都被占），无法启动")
        return 1

    import main as backend                  # noqa: E402  导入即建好 app 与数据目录
    threading.Thread(target=backend.serve, kwargs={"port": port, "log_level": "warning"},
                     daemon=True).start()

    url = f"http://{HOST}:{port}/"
    for _ in range(60):                      # 等它真的能应答再开浏览器，别甩给用户一个 404
        if _serving(port):
            break
        time.sleep(0.25)
    else:
        log("服务 15 秒内没起来，请把这份日志发给作者")
        return 1
    log(f"服务已就绪：{url}")
    if pdf:
        _handoff(port, pdf)
    if want_tray:
        start_tray(url, port, log)
    if want_window and _open_window(url, log):
        pass
    else:
        _open(url, log)
    try:                                     # 主线程守着：Ctrl+C（开发）或退出接口
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
