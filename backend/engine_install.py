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
import glob
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile

import httpx

import appinfo
import fsutil    # 静默删文件/流式 sha256：与 main/translate_full/update 共用同一份


def applog(msg: str):
    """本模块的日志出口：默认 stdout；main 启动时替换成 _applog——打包版 console=False
    没有 stdout，不接上的话"引擎安装到底卡在哪"一个字都留不下（与 llm.applog 同款接法）。"""
    print(msg)


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

from translate_full import _no_window, _kill_tree, engine_version   # 子进程藏控制台/连树杀/版本比较：与 translate_full 共用同一份（两处各写一份迟早漂移）

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
    if _cancel.is_set():
        # 用户在解压期/下载期点了停止，取消旗可能还亮着：预热是可选的加分项，
        # 不做也能用（翻译时引擎会自己按需补），别为一个取消过的安装再挂 30 分钟
        applog("[eggpaper] 引擎安装已取消，跳过预热")
        return
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
        # 用 Popen 而不是 subprocess.run：warmup 最长能跑半小时，界面上的
        # 「停止下载」不该在这段时间变成死键（run 没有插旗的地方）。取消/超时
        # 都走 **连树杀**——warmup 也是 multiprocessing 启动器，只杀父进程会留
        # 一群孙进程攥着引擎目录，后续升级的 rmtree/rename 全被它们挡住
        proc = subprocess.Popen([exe, "--warmup"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, creationflags=flags, startupinfo=si)
        deadline = time.time() + 1800
        while proc.poll() is None:
            if _cancel.is_set():
                _kill_tree(proc)
                applog("[eggpaper] 引擎预热被用户停止（安装已就绪，翻译时引擎会按需补资产）")
                return
            if time.time() > deadline:
                _kill_tree(proc)
                raise TimeoutError("warmup 超过 30 分钟")
            time.sleep(1)
        if proc.returncode == 0 and pkg_dir:
            for zp in glob.glob(os.path.join(pkg_dir, "offline_assets_*.zip")):
                try:
                    os.rename(zp, zp + ".installed")
                except OSError:
                    pass
    except Exception as e:
        # warmup 失败不挡安装（装完的状态照旧推进到 done），但留一行痕：不然后面翻译
        # 每批都白付 10-20 秒资产重哈希时，没人知道是这一步没跑成
        applog(f"[eggpaper] 引擎预热失败（不挡安装，翻译时引擎会自己按需补）："
               f"{type(e).__name__}: {str(e)[:160]}")

def settle_assets() -> int:
    """旧版装的引擎没赶上 _warmup 的「zip 改名收起」（或当时 warmup 失败）：
    每批翻译都白付 220MB 重哈希的 10-20 秒。资产缓存齐全就把 zip 收起，
    返回收起个数（0 = 没有可收的，或缓存不齐不敢收——zip 是离线恢复的唯一
    来源，缓存被清过时收了它，再恢复就只能联网重下）。"""
    try:
        import glob
        zips = glob.glob(os.path.join(install_dir(), "**", "offline_assets_*.zip"),
                         recursive=True)
        if not zips:
            return 0
        cache = os.path.join(appinfo.data_dir(), "home", ".cache", "babeldoc")
        if not os.path.isfile(os.path.join(cache, "cache.v1.db")):
            return 0
        if not all(os.path.isdir(os.path.join(cache, d)) for d in ("models", "fonts", "cmap")):
            return 0
        n = 0
        for zp in zips:
            try:
                os.rename(zp, zp + ".installed")
                n += 1
            except OSError:
                pass
        return n
    except Exception as e:
        applog(f"[eggpaper] 资产包归档（settle_assets）没做成，跳过：{type(e).__name__}: {str(e)[:160]}")
        return 0

def restore_assets_zip() -> int:
    """缓存不全时把收起的资产包放回 zip 名，让引擎自己离线恢复（返回放回个数）。

    为什么需要：`_warmup` 成功后会把这个 zip 改名 `.installed` 收起（不然每个引擎进程
    启动都要把 ~220MB 资产重哈希一遍，每批白付 10-20 秒）。但改名之后引擎就不认它了
    ——数据目录被清、home/ 缓存丢失时，引擎只能去它硬编码的三个资源站现取（BabelDOC
    的 FONT_METADATA_URL：github raw / huggingface / modelscope，没有环境变量入口，
    HF_ENDPOINT 之类一概无效——2026-10-12 读引擎源码确认），国内网络下就是"取资源失败"。
    放回 zip 名后，引擎启动器见到它会自己 restore 回缓存——离线也能把资产补回来。

    缓存齐全时不动（放回反而让每批多付重哈希的税）；恢复跑过一轮后，下一次翻译开头的
    settle_assets 见缓存齐全会再把它收起，自愈闭环。"""
    try:
        cache = os.path.join(appinfo.data_dir(), "home", ".cache", "babeldoc")
        if os.path.isfile(os.path.join(cache, "cache.v1.db")) and \
                all(os.path.isdir(os.path.join(cache, d)) for d in ("models", "fonts", "cmap")):
            return 0
        n = 0
        for zp in glob.glob(os.path.join(install_dir(), "**", "offline_assets_*.zip.installed"),
                            recursive=True):
            try:
                os.rename(zp, zp[: -len(".installed")])
                n += 1
            except OSError:
                pass
        if n:
            applog(f"[eggpaper] 资产缓存不全：把 {n} 个 offline 资产包放回 zip 名（引擎离线恢复）")
        return n
    except Exception as e:
        applog(f"[eggpaper] 资产包放回（restore_assets_zip）没做成，跳过：{type(e).__name__}: {str(e)[:160]}")
        return 0

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
    if kw.get("state") == "done":
        _invalidate_find_cache()     # 新引擎刚落地：find_installed 的缓存别拿着旧空值骗人

def cancel():
    """用户喊停：断点留着，下次（换源、重开应用）从断点接着下。"""
    _cancel.set()

_find_cache = {"at": 0.0, "path": ""}
_find_lock = threading.Lock()    # _find_cache 的读写锁：端点线程（engine_path → find_installed）
                                 # 与后台安装/清扫线程（换装、回滚、启动清扫）并发翻这本簿子

def _invalidate_find_cache():
    """引擎目录树刚动过：find_installed 的结果立刻失效（10 秒 TTL 里可能还躺着旧结论）。
    只置 at=0，不递归 find_installed——调用点自己决定什么时候重查。"""
    with _find_lock:
        _find_cache["at"] = 0.0

def find_installed() -> str:
    """在 engines/ 里找 pdf2zh.exe（递归：官方 zip 解压出来是带版本号的子目录）。

    这条也是"手动安装"的路：用户自己把官方 zip 解压到这个目录里，我们照样认。
    手动解压的旧版本可能和自动装的一份并存——**认版本号最高的那份**（保留最新的即可，
    2026-10-10 口径），解析不出版本当最老，同版本取 mtime 新的。

    结果缓存 10 秒：递归 glob 要走遍引擎的 site-packages（几千个文件），而它挂在
    「点全文翻译」的同步路径上（engine_path → find_installed），每次点击都走一遍
    纯属浪费。装完/换引擎的时刻由 _set(state="done") 主动失效，TTL 只是兜底。
    """
    import glob
    import time as _time
    now = _time.time()
    with _find_lock:
        if now - _find_cache["at"] < 10:
            return _find_cache["path"]
    cands = []
    root = install_dir()
    if os.path.isdir(root):
        cands = [p for p in glob.glob(os.path.join(root, "**", "pdf2zh.exe"), recursive=True)
                 if os.path.isfile(p)]
    if not cands:
        got = ""
    elif len(cands) == 1:
        got = cands[0]
    else:
        def _key(p: str):
            try:
                mt = os.path.getmtime(p)
            except OSError:
                mt = 0.0
            return (engine_version(p), mt)
        got = max(cands, key=_key)
    with _find_lock:
        _find_cache["at"] = now
        _find_cache["path"] = got
    return got

def _exe_under(root: str) -> str:
    """root 下有没有引擎 exe（绕过 find_installed 的 10 秒缓存：清扫/回滚要看的是盘上真相）。"""
    if not os.path.isdir(root):
        return ""
    return next(iter(glob.glob(os.path.join(root, "**", "pdf2zh.exe"), recursive=True)), "")

def sweep_leftovers(max_age: float = 7 * 86400) -> list:
    """启动清扫：升级换装与断点下载的残骸（2026-10-10 用户口径：更新只留最新，
    别把别人电脑的文件越堆越大）。

    - pdf2zh.old / pdf2zh.tmp：换装中途的残骸。_unpack 是「旧目录 rename 成 .old →
      新目录就位 → 删 .old」，那最后一删可能被杀软/僵尸 pdf2zh 的文件锁静默挡掉
      （ignore_errors）——正常路径删不掉的，启动时再试一遍；.tmp 是解压中途崩的。
      **删 .old 前必须先确认正式目录里真有引擎**：换装半途失败时 .old 是唯一可用
      副本（新目录没就位），无脑删 = 用户得重下 600MB——这时把 .old 恢复成正式名。
    - pdf2zh.partial：断点下载的家。**新鲜的留着**（7 天内：600MB 的断点，重试还能
      接着下）；过期的删——换版本之后旧断点永远续不上了，留着纯属占地。

    正在下载/解压时不动（那是活在用的目录）。返回清掉的条目清单（写启动日志用）。
    """
    with _lock:
        if _prog["state"] in ("downloading", "unpacking", "warming"):
            return []
    root = os.path.dirname(install_dir())
    cleaned = []
    # 正式目录里已经有引擎，这些残骸（.old 与让路用的 .old.<时间戳>）就是垃圾；
    # 没有引擎的话它们反而是唯一可用的副本——恢复一个回来，别删
    olds = [os.path.join(root, "pdf2zh.old")]
    try:
        olds += sorted(glob.glob(os.path.join(root, "pdf2zh.old.*")))
    except Exception:
        pass
    have_engine = _exe_under(install_dir())
    for op in olds:
        if not os.path.isdir(op):
            continue
        if have_engine:
            shutil.rmtree(op, ignore_errors=True)
            if not os.path.isdir(op):
                cleaned.append(os.path.basename(op))
        else:
            try:
                os.rename(op, install_dir())    # 正式目录没引擎：这是唯一可用副本
                _invalidate_find_cache()        # 刚把目录挪回来，find_installed 缓存别给旧结论
                cleaned.append(f"{os.path.basename(op)} → pdf2zh（恢复可用引擎）")
                have_engine = True              # 恢复一个就够，后面的同批当垃圾处理
                for rest in olds[olds.index(op) + 1:]:
                    if os.path.isdir(rest):
                        shutil.rmtree(rest, ignore_errors=True)
            except OSError:
                pass
    tmp = os.path.join(root, "pdf2zh.tmp")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
        if not os.path.isdir(tmp):
            cleaned.append("pdf2zh.tmp")
    # 上传落盘的暂存区：正常路径装完即删（start_from_zip 的 cleanup），但"上传到一半
    # 关掉软件"这类中断会留下半截 zip——按年龄扫掉，别让 2GB 的残骸永驻
    updir = install_dir() + ".upload"
    if os.path.isdir(updir):
        now = time.time()
        for fn in os.listdir(updir):
            fp = os.path.join(updir, fn)
            try:
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > max_age:
                    os.remove(fp)
                    cleaned.append(f"pdf2zh.upload/{fn}")
            except OSError:
                pass
        try:
            if not os.listdir(updir):
                os.rmdir(updir)
                cleaned.append("pdf2zh.upload/")
        except OSError:
            pass
    partial = _partial_dir()
    if os.path.isdir(partial):
        now = time.time()
        try:
            names = os.listdir(partial)
        except OSError:
            names = []
        for fn in names:
            fp = os.path.join(partial, fn)
            try:
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > max_age:
                    os.remove(fp)
                    cleaned.append(f"pdf2zh.partial/{fn}")
            except OSError:
                pass
        try:
            if not os.listdir(partial):
                os.rmdir(partial)
                cleaned.append("pdf2zh.partial/")
        except OSError:
            pass
    return cleaned

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

def _safe_extract_all(zf: zipfile.ZipFile, dest: str):
    """解压，但**拒绝越界路径**（zip-slip）。带 ../ 的条目能把文件写到目标目录外，
    这个 zip 是从网上下的（还可能来自用户自填的镜像地址），必须挡住。"""
    dest_abs = os.path.abspath(dest)
    for m in zf.infolist():
        target = os.path.abspath(os.path.join(dest, m.filename))
        if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
            raise ValueError(f"压缩包里有越界路径：{m.filename[:80]}")
    zf.extractall(dest)

def _verify_zip(zpath: str, own: bool = True) -> bool:
    """按官方哈希验一遍；不对时**把包和分段断点一起清掉**（own=False 的包只报不动）。

    清断点是必须的：分段是"按长度续传"的——内容换了（上游重传、版本换名复用
    pdf2zh.partial/ 里的旧 .partN）之后，续传只会在旧字节上接着写，拼出来的永远是
    坏包。不清就成"每次重试都秒失败"的死循环，提示还写着"已删除，可重试"，
    用户点十次也出不去（.partN 只被 7 天年龄清扫收走）。
    """
    if not ENGINE_SHA256:
        return True
    if fsutil.sha256_file(zpath).lower() == ENGINE_SHA256.lower():
        return True
    if own:
        fsutil.rm_quiet(zpath)
        shutil.rmtree(_partial_dir(), ignore_errors=True)
    return False

def _unpack(zpath: str, own: bool = True):
    """校验 → 解开 → 换到正式目录。下载装和本地文件装走的是同一段。

    换装口径：old 里的旧引擎**只有等新目录验证过关（找得到 pdf2zh.exe）才删**。
    半途失败（新目录没就位/新包里没有 exe）就把 old 回滚回正式名——不然盘上
    只剩一个 .old，启动清扫把它一删，用户唯一能用的引擎就没了（重下 600MB）。
    own：这份 zip 是不是我们自己的（网络下载/上传落盘的临时拷贝）。own=False 的
    （用户自己挑的文件）校验不过只报错，绝不删用户的文件。
    """
    tmp_root = install_dir() + ".tmp"
    final = install_dir()
    old = final + ".old"
    bak = old                   # 实际用的备份名：.old 被占时退到带时间戳的
    try:
        if not _verify_zip(zpath, own=own):
            if own:
                _set(state="error", error="下载校验不对，已删除，可重试")
            else:
                _set(state="error", error="这份包内容校验不对（不是官方引擎包）")
            return
        shutil.rmtree(tmp_root, ignore_errors=True)
        os.makedirs(tmp_root, exist_ok=True)
        _set(state="unpacking", pct=100)
        ex_dir = os.path.join(tmp_root, "x")
        os.makedirs(ex_dir, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            _safe_extract_all(zf, ex_dir)
        if _cancel.is_set():
            # 解压期点了「停止下载」：就当没装（正式目录还没动过一根手指），
            # 验好的包留在 partial 里，下次点安装秒过下载段
            shutil.rmtree(tmp_root, ignore_errors=True)
            _set(state="idle", pct=0, got=0, src="", url="")
            return
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(old):
            # 上次的 .old 还占着名字（exe 被杀软/僵尸进程攥着，rmtree 静默失败）：
            # Windows 的 rename 撞已存在的目标直接 FileExistsError，不让开的话
            # 每次升级都在这里硬失败，而且包已被删、重试要从头下 600MB。
            # 换个带时间戳的名字让路，启动清扫认 pdf2zh.old* 前缀。
            bak = f"{old}.{int(time.time())}"
        if os.path.isdir(final):
            os.rename(final, bak)
        os.rename(ex_dir, final)
        _invalidate_find_cache()    # 刚换了目录树：先失效缓存再找——TTL 里可能还躺着
        exe = find_installed()      # 装前那次「没装」的旧结论，快装完（<10s）时实测踩中
        if not exe:
            # 这套新目录里没有 exe：不能用。旧引擎还在备份里，原样放回来
            shutil.rmtree(final, ignore_errors=True)
            if os.path.isdir(bak):
                try:
                    os.rename(bak, final)
                    _invalidate_find_cache()
                except OSError:
                    pass
            _set(state="error", error="包里没有 pdf2zh.exe")
            return
        shutil.rmtree(bak, ignore_errors=True)     # 新引擎验证过关，才轮到删旧的
        shutil.rmtree(old, ignore_errors=True)     # 历史遗留的 .old 也顺手再试一次
        shutil.rmtree(tmp_root, ignore_errors=True)
        shutil.rmtree(_partial_dir(), ignore_errors=True)   # 装好了，断点没用了
        _warmup(exe)
        _set(state="done", path=exe)
    except Exception as e:
        shutil.rmtree(tmp_root, ignore_errors=True)
        # 换装半途失败：正式目录没就位而备份还在 → 回滚，别把唯一可用的旧引擎
        # 留在残骸状态（下次启动的清扫会把它当垃圾收走）
        if not os.path.isdir(final) and os.path.isdir(bak):
            try:
                os.rename(bak, final)
                _invalidate_find_cache()
            except OSError:
                pass
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
        fsutil.rm_quiet(p)
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
                if _verify_zip(zpath):
                    break
                # 这个源给的包哈希不对（镜像发过期的/被改过的内容）：换下一个源，
                # 别就此认输——官方直连往往是好的。坏包与分段断点已在 _verify_zip 清掉
                why = "内容校验不对"
            tried.append(f"{src}：{why}")
            _set(tried=list(tried))
            if why == "已停止":
                _set(state="idle", src="", url="")
                return
        else:
            _set(state="error", src="", url="",
                 error="每个源都没下动——" + "；".join(tried[-3:]) + "。可稍后重试")
            return
        _set(src="校验/解压")
        _unpack(zpath, own=True)    # 哈希已在循环里验过（own=True 允许它清坏包兜底）
        fsutil.rm_quiet(zpath)
        fsutil.rm_quiet(zpath + ".part")
    except Exception as e:
        shutil.rmtree(tmp_root, ignore_errors=True)
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        _cancel.clear()

def start(url: str = "") -> dict:
    # 检查+占位必须同一把锁（_set 自己也拿 _lock，锁内只能直接 update，否则死锁）：
    # 否则两个毫秒级相邻的请求可双双通过守卫，二开安装会在预热中的 exe 脚下玩 rename
    with _lock:
        if _prog["state"] in ("downloading", "unpacking", "warming"):
            return dict(_prog)
        _prog.update(state="downloading", pct=0, got=0, total=0, error="", url=url or "",
                     path="", src="", tried=[])
    if (url or "").strip():
        urls = [(url.strip(), "指定地址")]
    else:
        urls = _sources()
    _set(state="downloading", pct=0, got=0, total=0, error="", url=urls[0][0], path="",
         src=urls[0][1], tried=[])
    threading.Thread(target=_install, args=(urls,), daemon=True).start()
    return status()

def start_from_zip(zip_path: str, cleanup: bool = False) -> dict:
    """从**本地已有的 zip** 装引擎。

    这条路是给"网络到不了任何源"准备的——自己下好一份引擎包拷给对方，
    对方在设置里选这个文件即可（零基础：不需要 Python、不需要能上外网）。
    cleanup=True 表示这份 zip 是我们自己的临时拷贝（上传落盘的那份），装完就删；
    用户自己挑的文件永远不碰。
    """
    with _lock:
        if _prog["state"] in ("downloading", "unpacking", "warming"):
            busy = dict(_prog)
        else:
            busy = None
            _prog.update(state="unpacking", pct=100, got=0, total=0, error="", url="(本地文件)",
                         path="", src="本地文件", tried=[])
    if busy is not None:
        # 安装中：这份上传包不会被接收。是我们自己的临时拷贝就当场收掉——上传端点
        # 只在装完时删它，忙分支一返回就再没人认领，600MB~2GB 的残骸永驻盘上
        if cleanup:
            fsutil.rm_quiet(zip_path)
        return busy
    threading.Thread(target=_from_zip, args=(zip_path, cleanup), daemon=True).start()
    return status()

def _from_zip(zip_path: str, cleanup: bool = False):
    try:
        if not os.path.isfile(zip_path):
            _set(state="error", error="文件不存在")
            return
        _unpack(zip_path, own=cleanup)
        if cleanup:
            fsutil.rm_quiet(zip_path)   # ~600MB 的包：装完就收，别在 .upload 里常驻
    except Exception as e:
        _set(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        # 取消旗在这里也要收：上一次取消（或取消发生在本次安装期间）留下的旗子
        # 不清，下一次网络安装会在第一个检查点就自我中止——用户点了安装，界面
        # 毫秒级弹回 idle，看起来像"点了没反应"
        _cancel.clear()
