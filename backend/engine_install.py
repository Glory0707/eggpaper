"""全文翻译引擎（pdf2zh_next，BabelDOC 内核）的下载与安装：一次点击，后台全自动把引擎装到用户机器上。

**为什么不把 pdf2zh 打进安装包**：不是 AGPL（eggpaper 本来就是 AGPL-3.0，一起分发
不改变任何义务），是**渠道**——安装包 96MB 已贴着 Gitee 附件 100MB 的单文件上限，
塞进 ~600MB 的引擎就发不出去了。所以引擎永远"用户机器自己去上游取"，我们只把这一步
做成全自动：点「全文翻译」→ 后台自动下载安装 → 装好自动继续。

**怎么下得动（国内网络）**：官方 GitHub 直连实测 10 KB/s，600MB 等于下不动。
所以按顺序试一张源表，谁快用谁：
1. 更新源同源（{feed}/pdf2zh-*.zip——作者把引擎包和安装包放同一目录时最快，404 就过）；
2. gh-proxy 系镜像（前缀拼接官方 URL，国内通常几 MB/s；这类域名时不时换，死源几秒就跳过）；
3. 官方 GitHub 直连（挂了代理的用户它反而最稳）。
每个源都有**停滞判据**：开下 12 秒平均速度不足 64KB/s、或 25 秒没有任何新字节，
就断开换下一个源——不等它慢慢磨 8 个小时。

**完整性**：官方 zip 的 sha256 钉死在 ENGINE_SHA256（与 GitHub API 的 assets digest
逐字核对过），不管从哪个源下来（镜像再不可信也一样），解压前先校验，不对就删掉报错。
**断点续传**：半截文件存在 engines/pdf2zh.partial/，重试（换源、重开应用）都从断点
接着下；服务器不支持 Range（回 200 而不是 206）就从头来。

**装到哪**：{数据目录}/engines/pdf2zh/ —— 升级、卸载都不动这个目录（跟文库、配置
一个待遇）。装完 translate_full.engine_path() 会自己找到它。从 1.9 升上来也落这里：
下载完 _unpack 原地换掉旧目录，旧引擎无残留。

**装完要预热**：with-assets 包资产内置，`--warmup` 只做本地校验（无网也秒过）；万一
失败不挡安装，翻译时引擎会自己按需补。
"""
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile

import httpx

import appinfo

ENGINE_URL = os.environ.get(
    "EGGPAPER_ENGINE_URL",
    "https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/releases/download/"
    "v2.9.0/pdf2zh-v2.9.0-BabelDOC-v0.6.4-with-assets-win64.zip")
# 测试/局域网分发可用 EGGPAPER_ENGINE_URL 覆盖（文件名里要带 pdf2zh-v<版本> 供解析）。
ENGINE_SHA256 = os.environ.get("EGGPAPER_ENGINE_SHA256",
                               "6916a2f299b029cfb75803c780528088d93e7694d5597c4250ba2dcf5598f1d8")
# 官方指纹，与 GitHub API 的 assets digest 核对过；本地 zip 校验和也一样。换引擎版本时同步换。
# E2E 用假 zip 时以 EGGPAPER_ENGINE_SHA256=（空串）跳过校验。
# **必须用 with-assets 变体**（比普通包大 ~220MB）：普通包首译要在线下载版面模型与字体
# （上游竞速含 huggingface，国内时常超时——实测整个翻译直接死在预热上），with-assets
# 把资产全部内置，装完即离线可用；官方文档同样推荐受限网络用它。

def pinned_version() -> str:
    """钉住版本：从 ENGINE_URL 文件名里解析（pdf2zh-v2.9.0-… → "2.9.0"）。

    升级链路的基准：已装引擎的版本比它老，设置页就亮「有新版 + 升级」。
    解析不出（URL 被自定义且没按规范命名）返回 ""，升级提示安静关闭。"""
    m = re.search(r"pdf2zh-v(\d+(?:\.\d+){1,3})", os.path.basename(ENGINE_URL))
    return m.group(1) if m else ""

_MIRRORS = ("https://gh-proxy.com/", "https://gh-proxy.org/", "https://ghfast.top/")
_MIN_SPEED = 64 * 1024      # 停滞判据：开下 12 秒平均不足 64KB/s 判这个源死刑
_SPEED_WINDOW = 12          # （连续无数据的兜底交给 httpx 的 30s 读超时）

_lock = threading.Lock()
_prog = {"state": "idle", "pct": 0, "got": 0, "total": 0, "error": "", "url": "", "path": "",
         "src": "", "tried": []}
_cancel = threading.Event()

def install_dir() -> str:
    return os.path.join(os.path.dirname(appinfo.data_dir()), "engines", "pdf2zh")

from translate_full import _no_window   # 子进程藏控制台：与 translate_full 共用一份

def _warmup(exe: str):
    """装完先 `--warmup` 一次：把内置的 offline_assets 包 restore 进缓存并整体校验。

    HOME/USERPROFILE 指到数据目录的 home/（跟翻译子进程同一套），资产缓存在我们自己的
    文件夹里，不散落 C 盘。校验通过（退出码 0）后把包里的 offline_assets zip 改名收起：
    2.x 启动器**每次进程启动**见到这个 zip 都会把全部资产重新校验一遍（~220MB 的哈希，
    实测让每批翻译多等 10-20 秒）——资产既已入缓存，就不必每批都交这笔税。
    万一缓存日后被清，warmup 会按需在线重下（with-assets 包本来也只在这时才需要网）。
    warmup 失败不挡安装：状态照旧推进到 done，zip 原样保留。
    """
    _set(state="warming", pct=100)
    flags, si = _no_window()
    env = {**os.environ}
    pkg_dir = ""
    try:
        home = os.path.join(appinfo.data_dir(), "home")
        os.makedirs(home, exist_ok=True)
        env["HOME"] = home
        env["USERPROFILE"] = home
        pkg_dir = os.path.dirname(exe)
    except Exception:
        pass
    try:
        r = subprocess.run([exe, "--warmup"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800,
                           env=env, creationflags=flags, startupinfo=si)
        if r.returncode == 0 and pkg_dir:
            import glob
            for zp in glob.glob(os.path.join(pkg_dir, "offline_assets_*.zip")):
                try:
                    os.rename(zp, zp + ".installed")
                except OSError:
                    pass
    except Exception:
        pass

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
        _warmup(exe)
        _set(state="done", path=exe)
    except Exception as e:
        shutil.rmtree(tmp_root, ignore_errors=True)
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")

_PARALLEL_CONNS = 4      # 分段并行下载的连接数：镜像普遍按单连接限速，4 段约 3~4 倍

def _download_one(url: str, zpath: str) -> str:
    """从一个源下载完整 zip（带断点续传与停滞判据）。成功返回 ""，失败返回人话原因。

    zpath 与断点都放在 _partial_dir()（start() 的重试不会清它）。先探服务器吃不吃
    Range：吃就 4 线程分段并行（镜像按单连接限速是常态，并行是下载速度的大头），
    不吃或探不动就退回单流。两边的半截文件各自续传，互不兼容时以旧单流 .part 优先。
    """
    part = zpath + ".part"
    try:
        with httpx.stream("GET", url, headers={"Range": "bytes=0-0"},
                          follow_redirects=True, timeout=httpx.Timeout(30, connect=15)) as r:
            if r.status_code == 206 and (r.headers.get("content-range") or "").startswith("bytes"):
                cr = r.headers["content-range"]          # bytes 0-0/总长
                total = int(cr.split("/")[-1])
                if total > 32 * 1024 * 1024:             # 小文件不值得开多线程
                    return _download_parallel(url, zpath, total)
    except Exception:
        pass                                             # 探测失败照走单流
    return _download_single(url, part, zpath)

def _download_single(url: str, part: str, done: str) -> str:
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
        shutil.move(part, done)      # 下完整了：挪出断点目录，交给 _unpack 校验+解压
        return ""
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:120]}"

class _Pace:
    """并行分段的聚合进度与停滞判据：所有线程共写，进度取总和，速度看全窗口。"""

    def __init__(self, total: int):
        self.total = total
        self.got = 0
        self.t0 = time.time()
        self.base = 0                       # 窗口起点
        self.lock = threading.Lock()

    def add(self, n: int) -> str:
        """累计 n 个字节。返回 "" 或停滞原因。"""
        with self.lock:
            self.got += n
            _set(got=self.got, total=self.total,
                 pct=round(self.got * 100 / self.total) if self.total else 0)
            now = time.time()
            span = now - self.t0
            if span >= _SPEED_WINDOW:
                speed = (self.got - self.base) / span
                self.t0, self.base = now, self.got
                if speed < _MIN_SPEED:
                    return f"太慢（{int(speed / 1024)}KB/s），换源"
        return ""

def _download_parallel(url: str, zpath: str, total: int) -> str:
    """Range 分段并行：每段独立 .partN、独立断点续传、独立停滞检测。"""
    os.makedirs(_partial_dir(), exist_ok=True)
    part = zpath + ".part"          # 旧单流的断点：有它就别开并行，接着单流走完
    if os.path.isfile(part) and os.path.getsize(part) > 0:
        return _download_single(url, part, zpath)
    n = _PARALLEL_CONNS
    span = (total + n - 1) // n
    parts = [os.path.join(_partial_dir(), f".part{i}") for i in range(n)]
    pace = _Pace(total)
    errs = []

    def worker(i: int):
        if _cancel.is_set():
            return
        begin, end = i * span, min((i + 1) * span, total) - 1
        fp = parts[i]
        have = os.path.getsize(fp) if os.path.isfile(fp) else 0
        if have >= end - begin + 1:
            return                                   # 这段早就下完了
        try:
            with httpx.stream("GET", url, headers={"Range": f"bytes={begin + have}-{end}"},
                              follow_redirects=True, timeout=httpx.Timeout(30, connect=15)) as r:
                if r.status_code != 206:
                    errs.append(f"段{i}：服务器不回 206")
                    return
                with open(fp, "ab") as f:
                    for chunk in r.iter_bytes(512 * 1024):
                        if _cancel.is_set():
                            return
                        f.write(chunk)
                        have += len(chunk)
                        why = pace.add(len(chunk))
                        if why:
                            errs.append(f"段{i}：{why}")
                            return
                if have < end - begin + 1:
                    errs.append(f"段{i}：连接中断")
        except Exception as e:
            errs.append(f"段{i}：{type(e).__name__}: {str(e)[:80]}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if _cancel.is_set():
        return "已停止"
    if errs:
        return errs[0]
    done = all(os.path.getsize(p) == (min((i + 1) * span, total) - i * span)
               for i, p in enumerate(parts))
    if not done:
        return "分段长度不齐"
    with open(zpath, "wb") as out:
        for p in parts:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out)
    for p in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    return ""

def _install(urls):
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
            why = _download_one(url, zpath)
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
