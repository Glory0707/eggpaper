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
"""
import os
import subprocess
import sys

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


def open_window(url: str, size=(1440, 940)) -> str:
    """开一个独立窗口。返回用了哪种方式（给日志/界面提示用），失败返回空串。"""
    exe = browser_exe()
    if not exe:
        return ""
    try:
        subprocess.Popen([exe, f"--app={url}", f"--window-size={size[0]},{size[1]}"],
                         close_fds=True)
        return os.path.basename(exe).replace(".exe", "") + " 应用模式"
    except OSError:
        return ""


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
