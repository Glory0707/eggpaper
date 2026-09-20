"""全文翻译引擎（pdf2zh）的下载与安装：一次点击，后台全自动把引擎装到用户机器上。

**为什么不把 pdf2zh 打进安装包**：不是 AGPL（eggpaper 本来就是 AGPL-3.0，一起分发
不改变任何义务），是**渠道**——安装包 96MB 已贴着 Gitee 附件 100MB 的单文件上限，
塞进 308MB 的引擎就发不出去了。所以引擎永远"用户机器自己去上游取"，我们只把这一步
做成全自动：点「全文翻译」→ 后台自动下载安装 → 装好自动继续。

**怎么下得动（国内网络）**：官方 GitHub 直连实测 10 KB/s，308MB 等于下不动。
所以按顺序试一张源表，谁快用谁：
1. 更新源同源（{feed}/pdf2zh-*.zip——作者把引擎包和安装包放同一目录时最快，404 就过）；
2. gh-proxy 系镜像（前缀拼接官方 URL，国内通常几 MB/s；这类域名时不时换，死源几秒就跳过）；
3. 官方 GitHub 直连（挂了代理的用户它反而最稳）。
每个源都有**停滞判据**：开下 12 秒平均速度不足 64KB/s、或 25 秒没有任何新字节，
就断开换下一个源——不等它慢慢磨 8 个小时。

**完整性**：官方 zip 的 sha256 钉死在 ENGINE_SHA256，不管从哪个源下来（镜像再不可信
也一样），解压前先校验，不对就删掉报错。**断点续传**：半截文件存在
engines/pdf2zh.partial/，重试（换源、重开应用）都从断点接着下；服务器不支持 Range
（回 200 而不是 206）就从头来。

**装到哪**：{数据目录}/engines/pdf2zh/ —— 升级、卸载都不动这个目录（跟文库、配置
一个待遇）。装完 translate_full.engine_path() 会自己找到它。
"""
import hashlib
import os
import shutil
import threading
import time
import zipfile

import httpx

import appinfo

ENGINE_URL = ("https://github.com/Byaidu/PDFMathTranslate/releases/download/"
              "v1.9.11/pdf2zh-v1.9.11-win64.zip")
ENGINE_SHA256 = os.environ.get("EGGPAPER_ENGINE_SHA256",
                               "aa46c8dd37a4209b2f54072e237dcd1576bd44d8f288107b12046b3744795748")
# 官方指纹，与 GitHub API 的 assets digest 核对过；本地 zip 校验和也一样。换引擎版本时同步换。
# E2E 用假 zip 时以 EGGPAPER_ENGINE_SHA256=（空串）跳过校验。

_MIRRORS = ("https://gh-proxy.com/", "https://gh-proxy.org/", "https://ghfast.top/")
_MIN_SPEED = 64 * 1024      # 停滞判据：开下 12 秒平均不足 64KB/s 判这个源死刑
_SPEED_WINDOW = 12          # （连续无数据的兜底交给 httpx 的 30s 读超时）

_lock = threading.Lock()
_prog = {"state": "idle", "pct": 0, "got": 0, "total": 0, "error": "", "url": "", "path": "",
         "src": "", "tried": []}
_cancel = threading.Event()

def install_dir() -> str:
    return os.path.join(os.path.dirname(appinfo.data_dir()), "engines", "pdf2zh")

def _partial_dir() -> str:
    return install_dir() + ".partial"    # 断点的家：稳定目录，start() 重试不清它，装完才清

def status() -> dict:
    with _lock:
        out = dict(_prog)
        out["tried"] = list(_prog["tried"])
    if out["state"] == "idle":
        got = find_installed()
        if got:
            out.update(state="done", path=got)
    return out

def _set(**kw):
    with _lock:
        _prog.update(kw)

def cancel():
    """用户喊停：断点留着，下次（换源、重开应用）从断点接着下。"""
    _cancel.set()

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

def _sources() -> list:
    """源表：更新源同源 → 镜像 → 官方直连，(url, 展示名) 列表。"""
    out = []
    try:
        import config
        feed = (config.load().get("update", {}).get("feed_url") or "").strip().rstrip("/")
    except Exception:
        feed = ""
    if feed and feed.startswith("http"):
        out.append((f"{feed}/{os.path.basename(ENGINE_URL)}", "更新源"))
    for m in _MIRRORS:
        out.append((m + ENGINE_URL, "镜像"))
    out.append((ENGINE_URL, "官方直连"))
    return out

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()

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
    """校验 → 解开 → 换到正式目录。下载装和本地文件装走的是同一段。"""
    tmp_root = install_dir() + ".tmp"
    try:
        if ENGINE_SHA256 and _sha256(zpath).lower() != ENGINE_SHA256.lower():
            try:
                os.remove(zpath)
            except OSError:
                pass
            _set(state="error", error="下载校验不对（这个源的内容被动过？），已删除。可重试或用「选 zip 安装」")
            return
        shutil.rmtree(tmp_root, ignore_errors=True)
        os.makedirs(tmp_root, exist_ok=True)
        _set(state="unpacking", pct=100)
        ex_dir = os.path.join(tmp_root, "x")
        os.makedirs(ex_dir, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            _safe_extract_all(zf, ex_dir)
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
            _set(state="error", error="包里没有 pdf2zh.exe")
            return
        shutil.rmtree(_partial_dir(), ignore_errors=True)   # 装好了，断点没用了
        _set(state="done", path=exe)
        shutil.rmtree(install_dir() + ".tmp", ignore_errors=True)
    except Exception as e:
        shutil.rmtree(tmp_root, ignore_errors=True)
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")

def _download_one(url: str, zpath: str, src: str) -> str:
    """从一个源下载（带断点续传与停滞判据）。成功返回 ""，失败返回人话原因。

    zpath 与断点 .part 都放在 _partial_dir()（start() 的重试不会清它）；
    下完整后把 .part 改名成 zpath，交给 _unpack 校验+解压。
    """
    part = zpath + ".part"
    headers = {}
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    append = False
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                          timeout=httpx.Timeout(30, connect=15)) as r:
            if r.status_code == 206 and have:
                got = have
                append = True
            else:
                got = 0
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0) + got
            t0 = time.time()
            base = got
            mode = "ab" if append else "wb"
            with open(part, mode) as f:
                for chunk in r.iter_bytes(512 * 1024):
                    if _cancel.is_set():
                        return "已停止"
                    f.write(chunk)
                    got += len(chunk)
                    _set(got=got, total=total, pct=round(got * 100 / total) if total else 0)
                    now = time.time()
                    if now - t0 >= _SPEED_WINDOW:
                        speed = (got - base) / (now - t0)
                        if speed < _MIN_SPEED:
                            return f"太慢（{int(speed / 1024)}KB/s），换源"
                        t0, base = now, got
        if got < total:
            return f"连接中断（{got * 100 // total if total else 0}%）"
        shutil.move(part, zpath)     # 下完整了：挪出断点目录，交给 _unpack 校验+解压
        return ""
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:120]}"

def _install(urls):
    _set(state="downloading", pct=0, got=0, total=0, error="", url=urls[0][0] if urls else "",
         path="", src=urls[0][1] if urls else "", tried=[])
    tmp_root = install_dir() + ".tmp"
    zname = os.path.basename(ENGINE_URL.split("?")[0])
    zpath = os.path.join(_partial_dir(), zname)     # zip 落 partial 目录：不被 _unpack 的暂存清理波及
    try:
        shutil.rmtree(tmp_root, ignore_errors=True)
        os.makedirs(tmp_root, exist_ok=True)
        os.makedirs(_partial_dir(), exist_ok=True)
        tried = []
        for url, src in urls:
            if _cancel.is_set():
                _set(state="idle", src="", url="")
                return
            _set(src=src, url=url)
            why = _download_one(url, zpath, src)
            if not why:
                break
            tried.append(f"{src}：{why}")
            _set(tried=list(tried))
            if why == "已停止":
                _set(state="idle", src="", url="")
                return
        else:
            _set(state="error", src="", url="",
                 error="每个源都没下动——" + "；".join(tried[-3:]) + "。可稍后重试，或用「选 zip 安装」")
            return
        _set(src="校验/解压")
        _unpack(zpath)
        try:
            os.remove(zpath)
            os.remove(zpath + ".part")
        except OSError:
            pass
    except Exception as e:
        shutil.rmtree(tmp_root, ignore_errors=True)
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        _cancel.clear()

def start(url: str = "") -> dict:
    with _lock:
        if _prog["state"] in ("downloading", "unpacking"):
            return dict(_prog)
    _cancel.clear()
    if (url or "").strip():
        urls = [(url.strip(), "指定地址")]
    else:
        urls = _sources()
    _set(state="downloading", pct=0, got=0, total=0, error="", url=urls[0][0], path="",
         src=urls[0][1], tried=[])
    threading.Thread(target=_install, args=(urls,), daemon=True).start()
    return status()

def start_from_zip(zip_path: str) -> dict:
    """从**本地已有的 zip** 装引擎。

    这条路是给"网络到不了任何源"准备的——自己下好一份引擎包拷给对方，
    对方在设置里选这个文件即可（零基础：不需要 Python、不需要能上外网）。
    """
    with _lock:
        if _prog["state"] in ("downloading", "unpacking"):
            return dict(_prog)
    _set(state="unpacking", pct=100, got=0, total=0, error="", url="(本地文件)", path="",
         src="本地文件", tried=[])
    threading.Thread(target=_from_zip, args=(zip_path,), daemon=True).start()
    return status()

def _from_zip(zip_path: str):
    try:
        if not os.path.isfile(zip_path):
            _set(state="error", error="文件不存在")
            return
        _unpack(zip_path)
    except Exception as e:
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")
