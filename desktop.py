"""打包后的入口：起本地服务 → 打开浏览器。开发时不用它（`python backend/main.py`）。

三个决定：

1. **默认浏览器，不内嵌窗口**。这个应用的交互（划词、Ctrl+F、缩放、打印、多标签）
   全长在浏览器上；换成 WebView 壳子只会丢掉这些，还要多背一个运行时。
2. **没有控制台窗口**（打包时 console=False）。所以任何启动失败都必须写进日志文件，
   路径 %LOCALAPPDATA%\\eggpaper\\logs\\app.log——设置面板里写了怎么找到它。
3. **单实例**。端口上已经有 eggpaper 在跑（用户双击了两次图标），就只打开浏览器，
   不再起第二个进程去抢同一份 SQLite 和文库。
"""
import os
import socket
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


def main():
    _setup_paths()
    import appinfo
    log = _log
    log(f"启动 eggpaper {appinfo.version()}（packaged={appinfo.is_frozen()}）")

    for port in PORTS:                       # 已经有实例在跑：只开浏览器
        v = _serving(port)
        if v:
            log(f"{port} 上已有一个实例（{v}），打开浏览器即可")
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
    _open(url, log)
    try:                                     # 主线程守着：Ctrl+C（开发）或退出接口
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
