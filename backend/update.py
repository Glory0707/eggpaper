"""更新：查更新源、下载新安装包、把它交给安装器。

**更新源就是一个静态目录**（本机、局域网、对象存储、GitHub Releases 都行），里面两份东西：

    latest.json                 这一版是什么、装什么、有什么变化
    eggpaper-<版本>-setup.exe   安装包本身

`latest.json`：
    {"version": "0.2.0",
     "url": "http://192.168.1.5:8440/eggpaper-0.2.0-setup.exe",   # 也可以写相对文件名
     "size": 41234567, "sha256": "…", "pub_date": "2026-09-12",
     "notes": "这一版改了什么（换行会原样显示）", "min_version": "0.1.0"}

为什么自己写而不用 electron-updater 那套：这个应用是"本地服务 + 浏览器界面"，
安装包是 Inno Setup 做的，用户装的是一个 exe——查/下/换这三步各自都很简单，
依赖一套为 Electron 设计的更新框架反而是负担。

**升级不动数据**：数据在 %LOCALAPPDATA%\\eggpaper\\data，安装器只覆盖程序目录，
所以"下载新包 → 静默装掉 → 重启"之后，文库、批注、问答、配置原样都在。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import httpx

import appinfo

_cache = {}          # feed_url -> {"at": ts, "data": dict}
_lock = threading.Lock()
_progress = {"state": "idle", "pct": 0, "got": 0, "total": 0, "path": "", "error": "", "message": ""}


def _ver_tuple(v: str):
    """版本号比较用：0.10.2 → (0,10,2)。非数字段一律当 0，不抛异常。"""
    out = []
    for part in str(v or "").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0, 0, 0])[:4]


def newer(latest: str, current: str) -> bool:
    return _ver_tuple(latest) > _ver_tuple(current)


def check(feed_url: str, force: bool = False, cache_hours: float = 6) -> dict:
    """读更新源，回答"有没有新版本"。网络只是"查一下"，失败一律当"没有更新"处理——
    这是打开软件时的一次安静探测，不该让任何人看到一个报错弹窗。"""
    cur = appinfo.version()
    feed_url = (feed_url or "").strip().rstrip("/")
    if not feed_url:
        return {"ok": False, "reason": "nofeed", "current": cur, "has_update": False}
    with _lock:
        hit = _cache.get(feed_url)
        if hit and not force and time.time() - hit["at"] < cache_hours * 3600:
            return hit["data"]
    try:
        # follow_redirects 必须开：GitHub Releases 的永久链接
        # （github.com/<user>/<repo>/releases/latest/download/latest.json）是 302，
        # 不跟的话拿到的是空重定向体，永远"没读到更新源"
        r = httpx.get(f"{feed_url}/latest.json", timeout=6, follow_redirects=True,
                      headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        info = r.json()
    except Exception as e:
        out = {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:120]}", "current": cur,
               "has_update": False}
        with _lock:
            _cache[feed_url] = {"at": time.time(), "data": out}
        return out
    latest = str(info.get("version") or "")
    url = str(info.get("url") or "")
    if url and not url.lower().startswith(("http://", "https://")):
        url = f"{feed_url}/{url.lstrip('/')}"          # 允许只写文件名
    data = {
        "ok": True, "current": cur, "latest": latest, "has_update": bool(latest) and newer(latest, cur),
        "notes": info.get("notes") or "", "url": url, "size": int(info.get("size") or 0),
        "sha256": (info.get("sha256") or "").lower(), "pub_date": info.get("pub_date") or "",
        # 低于这个版本必须先升级（协议不兼容那种），前端据此不给"稍后"
        "required": bool(info.get("min_version")) and newer(info["min_version"], cur),
        "feed": feed_url,
    }
    with _lock:
        _cache[feed_url] = {"at": time.time(), "data": data}
    return data


def progress() -> dict:
    return dict(_progress)


def _set(**kw):
    with _lock:
        _progress.update(kw)


def download_dir() -> str:
    """安装包只允许落在这里、也只允许装这里的文件。"""
    return os.path.join(tempfile.gettempdir(), "eggpaper-update")


def download(url: str, sha256: str = "", size: int = 0):
    """把安装包下到临时目录，校验哈希。后台线程里跑，进度用 progress() 轮。"""
    _set(state="downloading", pct=0, got=0, total=size, path="", error="", message="")
    try:
        out_dir = download_dir()
        os.makedirs(out_dir, exist_ok=True)
        name = os.path.basename(url.split("?")[0]) or "eggpaper-setup.exe"
        out = os.path.join(out_dir, name)
        h = hashlib.sha256()
        with httpx.stream("GET", url, timeout=httpx.Timeout(600, connect=15), follow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or size or 0)
            got = 0
            with open(out, "wb") as f:
                for chunk in r.iter_bytes(256 * 1024):
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    _set(got=got, total=total, pct=round(got * 100 / total) if total else 0)
        # 没有 sha256 就不认：写成 `if sha256 and ...` 的话，发布方漏写 sha256（或旧格式
        # latest.json）时整段校验被跳过，一个被改动过的包会被直接标成 ready。
        if not sha256:
            os.remove(out)
            _set(state="error", error="更新源没给 sha256，无法校验完整性（发布方需要补上这一项）")
            return
        if h.hexdigest().lower() != sha256:
            os.remove(out)
            _set(state="error", error="下下来的安装包校验不一致（可能没下完或被改过），已丢弃")
            return
        _set(state="ready", path=out, pct=100, message="安装包已就绪")
    except Exception as e:
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:160]}")


def start_download(url: str, sha256: str = "", size: int = 0):
    if _progress["state"] == "downloading":
        return
    threading.Thread(target=download, args=(url, sha256, size), daemon=True).start()


def install(path: str) -> bool:
    """把安装包交给系统，然后自己退出。

    Inno Setup 的参数含义：/SILENT 只有进度条没有向导；/CLOSEAPPLICATIONS 关掉正在跑的
    旧版本（我们的 exe 会被关掉，这正是我们要的）；/RESTARTAPPLICATIONS 装完再拉起来。
    这里用 Popen 而不是等它跑完——安装器要替换的正是当前这个进程占着的文件。

    **只接受刚下载到那个临时目录里的 .exe**：这个接口本机无鉴权（127.0.0.1 + 任意网页都能
    POST），把调用方给的 path 交给 Popen 等于"以 eggpaper 的名义执行任意本机程序"。开发模式
    （源码）只提示不自杀。
    """
    if not path:
        return False
    real = os.path.realpath(path)
    home = os.path.realpath(download_dir())
    if os.path.dirname(real) != home or not real.lower().endswith(".exe") or not os.path.exists(real):
        return False
    args = [real]
    if os.name == "nt":
        args += ["/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
    try:
        subprocess.Popen(args, close_fds=True)
    except Exception:
        return False

    if not is_packaged():
        return True          # 开发模式：跑的是源码，装完也不该把这边的进程杀掉

    # 给安装器一点点时间拿到文件句柄，然后把自己关掉
    def bye():
        time.sleep(1.2)
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return True


def is_packaged() -> bool:
    """打包版才有"下载并安装"这条路（开发模式下正在跑的是源码，装它没有意义）。"""
    return appinfo.is_frozen()


def open_folder(path: str) -> None:
    """下好了但用户不想现在装：把文件所在的文件夹打开，别让安装包无声无息躺在 temp 里。"""
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass
