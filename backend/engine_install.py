"""整本翻译引擎（pdf2zh）的下载与安装：一次点击，把引擎装到用户自己的机器上。

**为什么不把 pdf2zh 打进安装包**：它是 AGPL-3.0。把它放进 eggpaper 的安装包对外
分发，AGPL 的分发条款就会传染——整个 eggpaper 都得按 AGPL 开放。所以引擎永远是
"用户机器自己去上游取"，我们只把这一步做成点一下按钮。这条路径干净：我们不再分发它。

**为什么下官方 win64 zip 而不是 pip install**：
1. 官方包是免 Python 的自包含目录（那台电脑不必先装 Python）——发给别人时这是关键；
2. 反而更小：pip 装全套会带 gradio(170MB)、opencv(113MB)、celery/redis/flask 这些
   给 GUI 和服务端用的东西。实测 v1.9.11 的 win64.zip = 308MB。

**装到哪**：{数据目录}/engines/pdf2zh/ —— 升级、卸载都不动这个目录（跟文库、配置
一个待遇）。装完 engine_path() 会自己找到它（见 translate_full.engine_path）。
"""
import os
import shutil
import threading
import time
import zipfile

import httpx

import appinfo

# 官方发布页（GitHub Releases）。国内直连常常慢或不通——所以界面允许改下载地址，
# 也允许"你自己下好 zip 丢进 engines/ 目录"（find_installed 是递归找的）。
DEFAULT_URL = ("https://github.com/Byaidu/PDFMathTranslate/releases/download/"
               "v1.9.11/pdf2zh-v1.9.11-win64.zip")

_lock = threading.Lock()
_prog = {"state": "idle", "pct": 0, "got": 0, "total": 0, "error": "", "url": "", "path": ""}


def install_dir() -> str:
    return os.path.join(os.path.dirname(appinfo.data_dir()), "engines", "pdf2zh")


def status() -> dict:
    with _lock:
        out = dict(_prog)
    if out["state"] == "idle":
        got = find_installed()
        if got:
            out.update(state="done", path=got)
    return out


def _set(**kw):
    with _lock:
        _prog.update(kw)


def find_installed() -> str:
    """在 engines/ 里找 pdf2zh.exe（递归：官方 zip 解压出来是带版本号的子目录）。

    这条也是"手动安装"的路：用户自己把官方 zip 解压到这个目录里，我们照样认。
    """
    import glob
    root = install_dir()
    if not os.path.isdir(root):
        return ""
    for p in sorted(glob.glob(os.path.join(root, "**", "pdf2zh.exe"), recursive=True)):
        if os.path.isfile(p):
            return p
    return ""


def _safe_extract_all(zf: zipfile.ZipFile, dest: str):
    """解压，但**拒绝越界路径**（zip-slip）。带 ../ 的条目能把文件写到目标目录外，
    这个 zip 是从网上下的（还可能来自用户自填的镜像地址），必须挡住。"""
    dest_abs = os.path.abspath(dest)
    for m in zf.infolist():
        target = os.path.abspath(os.path.join(dest, m.filename))
        if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
            raise ValueError(f"压缩包里有越界路径：{m.filename[:80]}")
    zf.extractall(dest)


def _unpack(zpath: str):
    """把引擎 zip 解开、换到正式目录。下载装和本地文件装走的是同一段。"""
    tmp_root = install_dir() + ".tmp"
    try:
        shutil.rmtree(tmp_root, ignore_errors=True)
        os.makedirs(tmp_root, exist_ok=True)
        _set(state="unpacking", pct=100)
        ex_dir = os.path.join(tmp_root, "x")
        os.makedirs(ex_dir, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            _safe_extract_all(zf, ex_dir)
        # 换目录：先把旧的挪开再换，中途失败不留半个引擎目录
        final = install_dir()
        old = final + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(final):
            os.rename(final, old)
        os.rename(ex_dir, final)
        shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(tmp_root, ignore_errors=True)
        exe = find_installed()
        if not exe:
            _set(state="error", error="包里没找到 pdf2zh.exe（下错了文件？要的是官方的 win64 包）")
            return
        _set(state="done", path=exe)
    except Exception as e:
        shutil.rmtree(tmp_root, ignore_errors=True)
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")


def _install(url: str):
    _set(state="downloading", pct=0, got=0, total=0, error="", url=url, path="")
    tmp_root = install_dir() + ".tmp"
    try:
        shutil.rmtree(tmp_root, ignore_errors=True)
        os.makedirs(tmp_root, exist_ok=True)
        zname = os.path.basename(url.split("?")[0]) or "pdf2zh.zip"
        zpath = os.path.join(tmp_root, zname)
        with httpx.stream("GET", url, timeout=httpx.Timeout(1800, connect=20),
                          follow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            got = 0
            with open(zpath, "wb") as f:
                for chunk in r.iter_bytes(512 * 1024):
                    f.write(chunk)
                    got += len(chunk)
                    _set(got=got, total=total, pct=round(got * 100 / total) if total else 0)
        _unpack(zpath)
        try:
            os.remove(zpath)
        except OSError:
            pass
    except Exception as e:
        shutil.rmtree(tmp_root, ignore_errors=True)
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")


def start(url: str = "") -> dict:
    with _lock:
        if _prog["state"] in ("downloading", "unpacking"):
            return dict(_prog)
    _set(state="downloading", pct=0, got=0, total=0, error="", url=url or DEFAULT_URL, path="")
    threading.Thread(target=_install, args=((url or DEFAULT_URL).strip(),), daemon=True).start()
    return status()


def start_from_zip(zip_path: str) -> dict:
    """从**本地已有的 zip** 装引擎。

    这条路是给"网络到不了 GitHub"准备的——国内直连实测 10 KB/s，308MB 等于下不动。
    所以真正靠谱的分发方式是：**自己下好一份引擎包，和安装包一起发给别人**，对方在
    设置里选这个文件即可。零基础（对方不需要 Python、不需要能上外网）就是这么满足的。
    """
    with _lock:
        if _prog["state"] in ("downloading", "unpacking"):
            return dict(_prog)
    _set(state="unpacking", pct=100, got=0, total=0, error="", url="(本地文件)", path="")
    threading.Thread(target=_from_zip, args=(zip_path,), daemon=True).start()
    return status()


def _from_zip(zip_path: str):
    try:
        if not os.path.isfile(zip_path):
            _set(state="error", error="文件不在了")
            return
        _unpack(zip_path)
    except Exception as e:
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")
