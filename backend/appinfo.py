"""应用身份与路径：开发时是仓库里的路径，打包后（PyInstaller）是安装目录与用户数据目录。

打包后有两件必须分清的事：

- **只读资源**（前端 dist、图标、VERSION）随程序走，用 `res_dir()`。它们可能在
  Program Files 这种没有写权限、且升级时会被整个覆盖的地方，绝不能往里写数据。
- **用户数据**（config.yaml、论文库、SQLite、翻译产物）用 `data_dir()`。**升级只换
  程序，不动数据**——这是覆盖安装不丢东西的前提。

数据目录的位置（按优先级）：

1. `EGGPAPER_DATA` 环境变量（测试/便携启动器用）；
2. 安装目录旁的 `data\\`（便携模式：里面有 eggpaper.db 才认，防呆）；
3. 指针文件 `%LOCALAPPDATA%\\eggpaper\\data.location` 里写的路径（设置里改目录后
   生成；重启时 `migrate_if_needed()` 把旧数据整体搬过去）；
4. 默认 `%LOCALAPPDATA%\\eggpaper\\data`。

数据永远不在程序目录的 _internal 里，也不会被升级或卸载碰到：升级只清 _internal，
卸载只删安装器装进去的文件。
"""
import os
import sys
import shutil

VERSION_FALLBACK = "0.0.0"

_MIGRATE_ITEMS = ("eggpaper.db", "config.yaml", "papers", "library", "translated", "home")

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

def _localappdata_base() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")

def default_data_dir() -> str:
    """默认数据目录（C 盘 LOCALAPPDATA）。指针与便携都不设时的落点。"""
    if is_frozen():
        return os.path.join(_localappdata_base(), "eggpaper", "data")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def portable_dir() -> str:
    """便携模式：安装目录旁的 data\\（有 eggpaper.db 才算数）。"""
    return os.path.join(exe_dir(), "data")

def pointer_file() -> str:
    """数据目录指针：里面只有一行路径。设置里改目录时写它。"""
    if is_frozen():
        return os.path.join(_localappdata_base(), "eggpaper", "data.location")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.location")

def read_pointer() -> str:
    try:
        with open(pointer_file(), encoding="utf-8") as f:
            p = f.read().strip()
        return p if p and os.path.isabs(p) else ""
    except OSError:
        return ""

def write_pointer(path: str):
    os.makedirs(os.path.dirname(pointer_file()), exist_ok=True)
    with open(pointer_file(), "w", encoding="utf-8") as f:
        f.write(os.path.abspath(path))

def _has_db(d: str) -> bool:
    return os.path.isfile(os.path.join(d, "eggpaper.db"))

def data_dir() -> str:
    """用户数据目录。EGGPAPER_DATA 优先（测试用），其次便携，其次指针，最后默认。"""
    env = os.environ.get("EGGPAPER_DATA")
    if env:
        return env
    port = portable_dir()
    if _has_db(port):
        return port
    ptr = read_pointer()
    if ptr and os.path.isdir(ptr):
        return ptr
    return default_data_dir()

def migrate_if_needed():
    """按指针把旧数据搬去新位置。必须在任何模块打开数据库**之前**调用。

    只在「新位置还没有 eggpaper.db、旧位置有」时动手——搬完旧目录只剩空壳；
    再调用是空操作。指向便携目录或 EGGPAPER_DATA 时不搬（便携用户自己管理）。
    """
    if os.environ.get("EGGPAPER_DATA"):
        return
    ptr = read_pointer()
    if not ptr:
        return
    if _has_db(ptr):
        return
    src = default_data_dir()
    if not _has_db(src):
        return
    os.makedirs(ptr, exist_ok=True)
    for name in _MIGRATE_ITEMS:
        s = os.path.join(src, name)
        d = os.path.join(ptr, name)
        if os.path.isdir(s):
            shutil.move(s, d)
        elif os.path.isfile(s):
            os.replace(s, d)
    src_eng = os.path.join(os.path.dirname(src), "engines")
    if os.path.isdir(src_eng):
        dst_eng = os.path.join(os.path.dirname(ptr), "engines")
        if not os.path.isdir(dst_eng):
            shutil.move(src_eng, dst_eng)

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
