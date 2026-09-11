"""把界面开成一个"没有浏览器边框"的独立窗口。

放在 backend/ 里是为了两处共用：`desktop.py`（双击启动时按 `--window` 走）和
`POST /api/window`（界面里点「在独立窗口打开」）。

为什么用 Edge/Chrome 的**应用模式**而不是 WebView 壳子：Windows 上 WebView2 的
运行时提供者就是 Edge——同一个引擎，应用模式还不用往安装包里塞那几十兆
（pywebview 一路会带 pythonnet，实测 +25MB）。窗口没有地址栏、没有标签页。

**一个说清楚的取舍**：应用模式窗口的任务栏按钮图标是**浏览器自己的**（Chromium 给
这种窗口不设专属图标）。要让任务栏显示 eggpaper 的图标，只能用原生窗口（pywebview
那一路）——那是 +25MB 与一层冻结风险换来的，需要时再说。
"""
import os
import subprocess

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
