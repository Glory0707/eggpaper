"""应用身份与路径：开发时是仓库里的路径，打包后（PyInstaller）是安装目录与用户数据目录。

打包后有两件必须分清的事：

- **只读资源**（前端 dist、图标、VERSION）随程序走，用 `res_dir()`。它们可能在
  Program Files 这种没有写权限、且升级时会被整个覆盖的地方，绝不能往里写数据。
- **用户数据**（config.yaml、论文库、SQLite、翻译产物）用 `data_dir()`，落在
  %LOCALAPPDATA%\\eggpaper\\data。**升级只换程序，不动数据**——这是覆盖安装不丢东西的前提。

开发时两者都在仓库里（res_dir()=仓库根，data_dir()=backend/data），行为一字不变。
"""
import os
import sys

# 兜底版本号故意是 0.0.0（不是当前版本）：万一打包时 VERSION 没带上，
# 这个假版本会在日志里露出来、也会让"检查更新"一直说有新版——比悄悄冒充
# 一个真实版本号好，后者会让用户永远升不上来还查不出原因
VERSION_FALLBACK = "0.0.0"


def is_frozen() -> bool:
    """是不是 PyInstaller 冻结出来的可执行文件。"""
    return bool(getattr(sys, "frozen", False))


def res_dir() -> str:
    """随程序分发的只读资源所在目录。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def exe_dir() -> str:
    """可执行文件所在目录（安装目录）。开发时等于仓库根。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    """用户数据目录。EGGPAPER_DATA 优先（便携模式/测试用）。"""
    env = os.environ.get("EGGPAPER_DATA")
    if env:
        return env
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "eggpaper", "data")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def version() -> str:
    """版本号只有一个来源：仓库根的 VERSION 文件（打包时一并带上）。"""
    for p in (os.path.join(res_dir(), "VERSION"), os.path.join(exe_dir(), "VERSION")):
        try:
            with open(p, encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            continue
    return VERSION_FALLBACK


def dist_dir() -> str:
    """前端构建产物（frontend/dist）。打包后它躺在资源目录里。"""
    return os.path.join(res_dir(), "frontend", "dist")
