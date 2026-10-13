"""pdf2zh_next（BabelDOC 内核）全文翻译封装：subprocess 隔离，绝不让 AGPL 代码进入本项目。

这个文件里的每一条"绕路"都是实测踩出来的，别照直觉改回去：

1. **必须带 CREATE_NO_WINDOW**。`pdf2zh.exe` 是控制台程序，而 eggpaper 打包版
   （console=False）自己没有控制台。Windows 的规矩是：没有控制台的进程去起一个
   控制台子进程，就给它**新开一个黑窗**。于是用户点一下「全文翻译」，屏幕正中间
   蹦出一个终端——那个终端不是我们的日志窗口，是 pdf2zh 自己的控制台。

2. **默认服务不能想当然**。bing 用的是 www.bing.com，在多数国内网络下可达；google
   的 translate.google.com 则常常连不上。连不上不是"慢"：它会在每个请求上重试，
   进程 CPU 恒为 0，界面永远停在「翻译中」，能拖到天荒地老。所以开跑前先做一次
   TCP 预检（2.5 秒），不通就当场换服务或者报错，别让它静默挂着。

3. **进度只在 stdout/stderr**。子进程用 Popen 逐行读（stderr 并进 stdout）：
   页进度报给前端并写进 app.log。capture_output 那种跑法等于全程黑箱，用户什么都看不到。

4. **产物必须按源文件名的词干找**。整个翻译件共用一个 out_dir，而 pdf2zh 按输入名
   命名产物（`abc.pdf` → `abc-mono.pdf`）。用 `sorted(os.listdir())[0]` 认产物，译第二篇时
   会把第一篇的译文认成自己的——A 的译文挂到 B 上。

5. **引擎必须 2.x**。1.9 经典版不支持 glossary（术语锁定是产品第一卖点，全文链就
   缺它），命令行参数也完全不同（`--service x` vs 每家一个 `--x` 旗标）。engine_probe
   解析 `--version` 的大版本号，1.x 一律报"旧版引擎"并引导重装（engine_install 会
   原地换掉），绝不让 1.9 混进来跑出一套没有术语锁定的译文。

6. **key 只经环境变量走（PDF2ZH_*）**。2.x 的配置体系是 pydantic-settings：
   `--openai-api-key` 对应环境变量 `PDF2ZH_OPENAI_API_KEY`（`--` 换 `PDF2ZH_`、
   `-` 换 `_`）。子进程的 HOME/USERPROFILE 指到数据目录的 home/——pdf2zh_next 在
   import 时就会创建 `~/.config/pdf2zh`，不指过去就散落在 C 盘用户目录；顺带把它
   可能自动落盘的 config.v3.toml 在任务收尾时扫掉（里面有 key 就不该留着）。
"""
import collections
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

import fsutil    # 静默删文件的小工具：与 main/engine_install/update 共用同一份
import llm     # 只借 human_error 把失败翻成人话（llm 不反向依赖本模块，无环）
import pdfparse  # 页数：只为 len(doc) 开一遍文档的调用点收口（pdfparse 不反向依赖本模块）

JOBS = {}
_RUNNING = {}
_WORKERS = {}    # pid -> 这一轮的流水线线程：判"状态说在跑、进程里有没有活"用（见 start）
_PROBE = {}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROG = re.compile(r"(\d+)%\|[^|]*\|\s*(\d+)/(\d+)")
AUTH_FAILS = ("authentication fails", "invalid api key", "incorrect api key",
              "401 - ", "unauthorized", "invalid_api_key", "api key is required")
# 引擎取字体/模型资源失败（BabelDOC 的 warmup 阶段，与实际用哪家翻译服务无关）：
# 本机此刻连不上它列的资源站（github raw / huggingface / modelscope，取最快的那家）。
# 这不是"这一页坏了"——整篇每一页都会卡在同一处，每页各试两遍只是把时间耗光。
# 2026-10-11 实测：代理停掉后 huggingface 超时，16 页的翻译在 8 分钟里一页都没成。
# 只认"带网络超时特征"的那一种：同一句前缀也可能是 home 缓存并发撞车的 NameError
# （见 _run_version 文档），那种是重试就能好的，不该按网络问题收工。
_ASSET_FAILS = ("all upstreams failed", "failed to get fastest upstream")
_ASSET_NET_HINTS = ("timeout", "timed out", "connect", "connection", "getaddrinfo", "ssl", "proxy")
_ASSET_HINT = ("翻译引擎取字体/模型资源失败：连不上它的资源站（GitHub / HuggingFace / "
               "ModelScope）。检查网络或开代理后重新点翻译——不必等，整篇都会卡在同一处")

def _is_asset_net_fail(low: str) -> bool:
    if any(k in low for k in _ASSET_FAILS):
        return True
    return "asset coroutine failed" in low and any(h in low for h in _ASSET_NET_HINTS)

PROBE_TIMEOUT = 2.5
PROBE_TTL = 600

SERVICE_HOST = {
    "google": "translate.google.com",
    "bing": "www.bing.com",
    "deepl": "api-free.deepl.com",
    "openai": "api.openai.com",
    "deepseek": "api.deepseek.com",
    "zhipu": "open.bigmodel.cn",
    "silicon": "api.siliconflow.cn",
    "modelscope": "api-inference.modelscope.cn",
    "gemini": "generativelanguage.googleapis.com",
    "grok": "api.x.ai",
    "groq": "api.groq.com",
    "tencent": "tmt.tencentcloudapi.com",
}
AUTO_FALLBACK = ("bing",)

# ---------------- 连通性预检 ----------------

def probe(host: str, port: int = 443, timeout: float = PROBE_TIMEOUT, ttl: float = PROBE_TTL):
    """能不能连上 host:port。返回 (是否通, 不通时的一句人话)。结果缓存 ttl 秒。
    缓存键含端口：同一台 127.0.0.1 上的 443 与 8080 是两个服务的两回事。"""
    if not host:
        return True, ""
    key = f"{host}:{port}"
    hit = _PROBE.get(key)
    now = time.time()
    if hit and now - hit[2] < ttl:
        return hit[0], hit[1]
    ok, why = False, ""
    try:
        ip = socket.gethostbyname(host)
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        ok = True
    except Exception as e:
        why = f"{host} 连不上（{type(e).__name__}）"
    _PROBE[key] = (ok, why, now)
    return ok, why

def choose_service(service: str, host: str = ""):
    """定下这次真正用哪个服务。返回 (服务名或 None, 给用户的一句话)。

    host 传了就以它为准（openai 服务用的是用户在设置里填的那个端点），
    可以带端口（`127.0.0.1:8080` 这类本地网关）——端口不参与就按 443 探，
    把明明活着的端点判成"连不通"，然后静默跌落 bing。
    """
    service = (service or "bing").strip()
    target = host or SERVICE_HOST.get(service, "")
    h, _, port_s = target.partition(":")
    ok, why = probe(h, int(port_s) if port_s.isdigit() else 443)
    if ok:
        return service, ""

    for alt in AUTO_FALLBACK:
        if alt == service or not probe(SERVICE_HOST.get(alt, ""))[0]:
            continue
        return alt, f"{service} 在你的网络下不通（{why}），已自动改用 {alt}"
    return None, (f"{why}。「设置 → 全文翻译服务」换一个能用的（国内推荐 bing），"
                  f"或者先连上外网再试。")

# ---------------- 引擎：在哪、能不能跑 ----------------

def engine_path(explicit: str = "") -> str:
    """找 pdf2zh 引擎可执行文件；找不到返回空串。

    **只查 PATH 和 exe 同目录是不够的**——pip 装的 pdf2zh 落在 Python 的 Scripts
    目录，而那个目录不保证在 PATH 里（pip 自己都会提醒"不在 PATH"）。实测本机
    `%LOCALAPPDATA%\\Programs\\Python\\Python312\\Scripts\\pdf2zh.exe` 就躺在那儿，
    而 `shutil.which` 一个都看不见——"明明装了却说没装"就是这么来的。

    顺序：设置里指定的 → 数据目录 engines/（自动装的最新版插在最前）→ PATH → exe 同目录
    → 常见 Python Scripts。engines/ 靠前是因为它给"手动放一个进来"留了稳位：那目录
    升级、卸载都不动。
    """
    import glob
    if os.environ.get("EGGPAPER_ENGINE_OFF"):
        return ""      # E2E 专用：假设这台机器没有引擎，验证"缺引擎自动安装"链路
    want = (explicit or "").strip().strip('"')
    if want and os.path.exists(want):
        return want
    cand = []
    w = shutil.which("pdf2zh")
    if w:
        cand.append(w)
    here = os.path.dirname(os.path.abspath(sys.executable))
    cand.append(os.path.join(here, "pdf2zh.exe"))
    try:
        import engine_install
        got = engine_install.find_installed()
        if got:
            cand.insert(0, got)
    except Exception:
        pass
    for pat in (r"%LOCALAPPDATA%\Programs\Python\Python3*\Scripts\pdf2zh.exe",
                r"%APPDATA%\Python\Python3*\Scripts\pdf2zh.exe",
                r"C:\Python3*\Scripts\pdf2zh.exe",
                r"C:\Program Files\Python3*\Scripts\pdf2zh.exe"):
        try:
            cand.extend(glob.glob(os.path.expandvars(pat)))
        except Exception:
            pass
    for p in cand:
        if p and os.path.exists(p):
            return p
    return ""

_VER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

def _parse_ver(out: str) -> tuple:
    """`--version` 输出 → (major, minor, patch)；认不出返回 ()。"""
    m = _VER.search(out or "")
    if not m:
        return ()
    try:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    except ValueError:
        return ()

def _run_version(path: str) -> tuple:
    """跑一次 `--version`，返回 (returncode, 合并输出)；起不来返回 (-1, "")。

    HOME/USERPROFILE 指到数据目录 home/：pdf2zh_next 在 import 时就会创建
    ~/.config/pdf2zh——探测也要守"我们写的都规范在 eggpaper 文件夹里"这条线。

    **全程持锁，探测必须串行**：BabelDOC 2.9.0 在 home 无缓存时会边启边初始化
    资产（onnx 模型），两个 --version 并发跑会撞车——实测一个崩出
    `RuntimeError: asset coroutine failed: NameError: name 'exit' is not defined`。
    启动预热探测和用户点翻译时的探测就是这么撞上的（serve 起来 3 秒内点翻译）。
    """
    if not path or not os.path.exists(path):
        return -1, ""
    flags, si = _no_window()
    try:
        with _RUNVER_LOCK:
            r = subprocess.run([path, "--version"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120,
                               creationflags=flags, startupinfo=si, env=_page_env({}))
    except Exception:
        return -1, ""
    return r.returncode, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()

_RUNVER_LOCK = threading.RLock()   # 可重入：engine_probe_cached 持锁时 engine_probe → _run_version 同线程再进

def engine_version(path: str) -> tuple:
    """解析引擎版本号为 (major, minor, patch)；认不出返回 ()。

    结果按 mtime 缓存：`--version` 实测要好几秒（要 import 整套依赖），这是设置页
    每次打开都要调的口；文件被换掉（重装/升级）mtime 一变立刻重探。
    """
    if not path or not os.path.exists(path):
        return ()
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return ()
    hit = _VERS.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    rc, out = _run_version(path)
    ver = _parse_ver(out)
    _VERS[path] = (mt, ver)
    return ver

_VERS = {}

def engine_probe(path: str) -> tuple:
    """跑一次 `pdf2zh --version`，确认它**真的能跑**而且是 2.x。返回 (可用, 说明)。

    为什么不能只看文件在不在：pip 卸载后残留的 .exe 壳照样在，运行时报
    `ModuleNotFoundError: No module named 'pdf2zh'`（本机实测就有一个）。那种
    "引擎看着在、每页都失败"的现场，只查文件存在永远查不出来。
    为什么必须 2.x：1.9 经典版没有 glossary（全文术语锁定缺它不成），命令行协议
    也整个不同。1.x 一律按"旧版引擎"处理，主流程会引导 engine_install 原地升级。
    """
    if not path:
        return False, "没找到"
    rc, out = _run_version(path)
    if rc == -1:
        return False, "起不来"
    low = out.lower()
    if "no module named 'pdf2zh'" in low or "modulenotfounderror" in low:
        return False, "exe 在但包已丢（空壳）"
    if rc == 0 and "pdf2zh" in low:
        ver = _parse_ver(out)
        try:
            _VERS[path] = (os.path.getmtime(path), ver)   # 探一次，版本缓存同享（设置页首开少跑一遍 --version）
        except OSError:
            pass
        if ver and ver[0] < 2:
            return False, f"旧版引擎 {'.'.join(map(str, ver))}（全文术语锁定需要 2.x，请升级）"
        # 2.x 的 stdout 前面挂着 rich 日志（时间戳/模块名），只挑干净的那行给用户看
        line = next((ln.strip() for ln in out.splitlines()
                     if re.match(r"^\s*pdf2zh-next version:", ln)), "")
        if not line and ver:
            line = f"pdf2zh-next {ver[0]}.{ver[1]}.{ver[2]}"
        return True, (line[:60] if line else "pdf2zh")
    tail = out.splitlines()[-1].strip()[:160] if out else ""
    return False, tail or f"退出码 {rc}"

_ENGINE = {}

def engine_probe_cached(path: str, ttl: float = 900) -> tuple:
    """engine_probe 的缓存版。`--version` 实测要 3 秒（pdf2zh 得 import 整套依赖），
    而这是**点一下按钮就要过的一道门**——每次等 3 秒不值。同一个文件 15 分钟内只探
    一次；文件被换掉（重装、改路径）mtime 一变立刻重探，不会拿着旧结论骗人。"""
    if not path:
        return engine_probe(path)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return engine_probe(path)
    hit = _ENGINE.get(path)
    if hit and hit["mtime"] == mt and time.time() - hit["at"] < ttl:
        return hit["ok"], hit["why"]
    with _RUNVER_LOCK:
        # 双重检查：排队等锁的工夫，别的线程（比如启动预热）可能已经探完写缓存了
        hit = _ENGINE.get(path)
        if hit and hit["mtime"] == mt and time.time() - hit["at"] < ttl:
            return hit["ok"], hit["why"]
        ok, why = engine_probe(path)
        _ENGINE[path] = {"mtime": mt, "ok": ok, "why": why, "at": time.time()}
        return ok, why

def probe_cached(path: str) -> tuple | None:
    """engine_probe_cached 的**只读**版：缓存热就返回结果，冷返回 None（绝不跑探测）。

    给点击链路用：冷缓存的一次探测要 3 秒，点「全文翻译」不该卡在它上面。
    返回 None 时调用方应当放行 start()（后台线程里有同款把关）并自己起个
    后台预热，让下一次点击拿到热缓存。
    """
    if not path:
        return False, "没找到"
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return False, "没找到"
    hit = _ENGINE.get(path)
    if hit and hit["mtime"] == mt and time.time() - hit["at"] < 900:
        return hit["ok"], hit["why"]
    return None

# ---------------- 起进程 ----------------

def _cmd(pdf_path, out_dir, service, extra, glossary_csv="", engine: str = "", pages: str = ""):
    """pdf2zh_next 的命令行协议与 1.9 完全不同：每家翻译服务一个独立旗标
    （`--bing`/`--openai`/…，不传会落到它默认的 SiliconFlowFree），key 一类敏感值
    一律走 PDF2ZH_* 环境变量（见 _pdf2zh_env），不进命令行、不进配置文件。

    页级批次固定带：
    --pages N-M                只译这一段（1 基闭区间）；
    --only-include-translated-page  产物只含所选页（不带的产物是"整本但只有这段被译了"，
                               两种形态组装逻辑都认，带上省得猜）；
    --no-dual                  盘上只落译文版，双语版要的时候由 原文+译文 派生（省一半盘）；
    --watermark-output-mode no_watermark  显式关水印，别吃默认值。
    """
    exe = engine_path(engine)
    if exe:
        base = [exe]
    elif getattr(sys, "frozen", False):
        raise FileNotFoundError("pdf2zh")
    else:
        base = [sys.executable, "-m", "pdf2zh_next"]
    cmd = base + [pdf_path, "--output", out_dir, f"--{service}"]
    if glossary_csv and service in LLM_SERVICES:
        # 术语注入走 prompt，只有 LLM 服务吃这一套；bing/google 物理上做不到（1.9 也一样）
        cmd += ["--glossaries", glossary_csv]
    cmd += ["--only-include-translated-page", "--no-dual",
            "--watermark-output-mode", "no_watermark"]
    if service in LLM_SERVICES:
        # LLM 通道提速（2026-10-09 真端点对照实测）：BabelDOC 默认 qps=4、段落翻译
        # 线程池=并发池=qps——每页几十段请求排成 4 路纵队。抬到 12 实测墙钟 ~1.35x
        # （215s→159s，两轮复现），同样的请求只是并发跑，质量不动；429 有 tenacity
        # 指数退避兜着。qps 再往上收益趋平（瓶颈转向端点每请求延迟），12 是实测拐点。
        # 用户在设置里自己写了 --qps 就尊重用户的。
        if "--qps" not in (extra or ""):
            cmd += ["--qps", "12"]
        # 注入了自家词表就必须关掉引擎的自动术语抽取：2.9.0 的
        # get_glossaries_for_translation 在"抽取开启且抽到了词"时**只用它自己抽的那份**，
        # 我们 --glossaries 注入的析读精选词表会被无视——术语锁定整个落空。
        # 关掉后翻译走 user_glossaries（就是我们的词表），顺带省掉抽取那趟
        # ~76k input tokens（对照实测墙钟无害：162s vs 159s）。
        # 没有词表可注入时（这篇还没析读过）留着它，引擎自己抽一份聊胜于无。
        if glossary_csv and "--no-auto-extract-glossary" not in (extra or ""):
            cmd += ["--no-auto-extract-glossary"]
    if extra:
        cmd += shlex.split(extra)
    if pages:
        cmd += ["--pages", pages]
    return cmd

def write_glossary_csv(path: str, rows: list) -> bool:
    """本篇术语表 → BabelDOC 的术语 CSV（表头 source,target）。空表返回 False（不传旗标）。

    **不写 tgt_lng 列**：写了会按 `--lang-out` 归一化后过滤（`zh-CN`→`zh_cn`），
    跟我们传的 `zh` 对不上的风险不值得冒；不写 = 不过滤 = 永远生效。
    编码用裸 utf-8：带 BOM 的话表头会变成 "\\ufeffsource"，列名对不上。
    """
    import csv
    items = []
    seen = set()
    for r in rows or []:
        en = (r.get("term_en") or "").strip()
        zh = (r.get("term_zh") or "").strip()
        if not en or not zh or en.lower() in seen:
            continue
        seen.add(en.lower())
        items.append((en, zh))
    if not items:
        return False
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["source", "target"])
            w.writerows(items)
        return True
    except OSError:
        return False

def _no_window():
    """(creationflags, startupinfo)：让子进程**不要**开出控制台窗口。

    CREATE_NO_WINDOW 是解药；startupinfo 那两行是给某些还看 SW_HIDE 的老路径上的
    双保险，两个一起给不冲突。
    """
    if os.name != "nt":
        return 0, None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return 0x08000000, si

def sweep_key_copies(page_root: str):
    """扫掉页级目录里 1.9 时代的 pdf2zh 配置副本（里面带着 key）。

    留着复用是**为了省时间**，不是为了存 key。1.9 会把环境变量里的 key 写进
    `--config` 副本，升级到 2.x 后这些副本不再生成，但存量目录里可能还有。
    """
    for dirpath, _dirs, files in os.walk(page_root):
        for fn in files:
            if fn.startswith(".pdf2zh-") and fn.endswith(".json"):
                fsutil.rm_quiet(os.path.join(dirpath, fn))

def _sweep_home_config():
    """pdf2zh_next 可能把这次运行收到的设置（含 key）自动落盘到 ~/.config/pdf2zh 的
    config.v3.toml。HOME 已指到我们数据目录的 home/，但那也是盘——任务收尾扫掉，
    默认配置（不含用户值）留着无妨。"""
    try:
        import appinfo
        cfg_dir = os.path.join(appinfo.data_dir(), "home", ".config", "pdf2zh")
        if not os.path.isdir(cfg_dir):
            return
        for fn in os.listdir(cfg_dir):
            low = fn.lower()
            if low.startswith("config.v") and (low.endswith(".toml") or low.endswith(".toml.temp")) \
                    and "default" not in low:
                fsutil.rm_quiet(os.path.join(cfg_dir, fn))
    except Exception:
        pass

def cancel(pid: str, out_dir: str = ""):
    """删论文/换版本/停止时用：把还在跑的 pdf2zh 掐掉。

    pdf2zh 是独立进程——不掐的话，"删掉这篇论文"之后它还会跑完、还会往
    已删掉的论文文件夹里写回 mono.pdf，用户以为删干净了、盘上却留下孤立的译文文件。

    两层够得着的手：本进程起的子进程直接 kill；跨重启的孤儿（eggpaper 被杀后
    pdf2zh 还在跑，_RUNNING 已空）凭它命令行里的本篇目录认出来再掐——
    启动清扫不动它们（那是在等孤儿译文认领/断点续译），只有用户明确对着这篇
    做删除/停止时才追杀。
    """
    j = job(pid)
    if j.get("status") in ("running", "queued"):
        # 只给在跑/在排队的一轮置旗：空闲时置旗会被下一轮 start 当成"取消前到达"误吞
        j["_abort"] = True
    procs = _RUNNING.get(pid) or []
    for proc in procs:
        _kill_tree(proc)     # 连树杀：pdf2zh 2.x 的孙进程命令行不带目录，目录追杀认不出
    orphaned = kill_orphan_procs(out_dir)
    return bool(procs) or orphaned > 0

def kill_orphan_procs(out_dir: str) -> int:
    """命令行里带着本篇输出目录的孤儿 pdf2zh 一律掐掉，返回掐掉几个。

    为什么不持久化 PID：Windows 的 PID 复用很激进，重启后照单全杀会误杀无辜进程；
    而每个 pdf2zh 的命令行里本来就有 `--output <本篇目录>`（_cmd 构造，cwd 也是它）
    ——进程自己就是自己的注册表，按目录路径匹配，PID 复用者命令行不可能撞上。
    PID 只从进程枚举来，永远不从 API 参数来（本机接口无鉴权，kill-by-pid 不许
    变成外部可指定的原语）。

    枚举用 PowerShell Get-CimInstance（wmic 在 Win11 24H2 已移除）；只在删论文/
    换版本/停止这类用户动作里调用，一次几百毫秒，不在热路径上。枚举进程自己的
    命令行里也带着 needle，解析时把自己和它排除。目录已不在（删完/清完）就没什么
    好保护的了，直接不扫。
    """
    if os.name != "nt":
        return _kill_orphan_posix(out_dir)
    if not out_dir or not os.path.isdir(out_dir):
        return 0
    needle = out_dir.rstrip("\\/").lower().replace("'", "''")
    # 双重判据：命令行含本篇目录 + 镜像名是引擎家族（pdf2zh/python）。单看命令行
    # 会误杀"用户在别的阅读器里开着这份 PDF"的进程；单看镜像名会误伤无关 python。
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine "
              "-and ($_.Name -eq 'pdf2zh.exe' -or $_.Name -eq 'python.exe' "
              "-or $_.Name -eq 'pythonw.exe') "
              "-and $_.CommandLine.ToLower().Contains('" + needle + "') } | "
              "ForEach-Object { $_.ProcessId }")
    flags, si = _no_window()
    enum = None
    try:
        enum = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=flags, startupinfo=si)
        out, _ = enum.communicate(timeout=15)
    except Exception as e:
        # 枚举超时/起不来也要把 powershell 收掉：communicate 超时**不杀**子进程，
        # 不收的话它一直挂着，用户每删一篇就再堆一个。返回值语义不变（0 = 这次
        # 没追杀到孤儿），但留一行日志——静默吞异常会让"追杀不生效"永远查不出来。
        if enum is not None:
            try:
                enum.kill()
                enum.wait(timeout=5)
            except Exception:
                pass
        llm.applog(f"[eggpaper] 孤儿 pdf2zh 枚举失败（跳过一次追杀）：{type(e).__name__}: {str(e)[:120]}")
        return 0
    me = os.getpid()
    killed = 0
    for line in out.split():
        if not line.strip().isdigit():
            continue
        target = int(line)
        if target in (me, enum.pid):
            continue
        try:
            # /T 连树一起带走：pdf2zh 2.x 的 multiprocessing 子进程命令行不带目录
            subprocess.run(["taskkill", "/PID", str(target), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            killed += 1
        except Exception:
            pass
    return killed

def _kill_orphan_posix(out_dir: str) -> int:
    """mac/linux 侧的同款（pgrep -f 全命令行匹配）。"""
    if not out_dir:
        return 0
    import re as _re
    needle = _re.escape(out_dir.rstrip("/"))
    try:
        out = subprocess.run(["pgrep", "-f", needle], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return 0
    me = os.getpid()
    killed = 0
    for line in out.split():
        if line.strip().isdigit() and int(line) != me:
            try:
                os.kill(int(line), 9)
                killed += 1
            except Exception:
                pass
    return killed

def _kill_tree(proc):
    """把一个引擎进程**连子孙一起**掐掉。

    为什么不能只 proc.kill()：pdf2zh 2.x 的 multiprocessing 子进程命令行里不带本篇
    目录（kill_orphan_procs 认不出它们），只杀父进程会留下孙进程继续跑。两条实害：
    ① 孙进程攥着 `.pages` 目录的句柄，收尾的 rmtree 静默失败，垃圾留在用户盘上；
    ② 它还继承着 stdout 管道写端——`for raw in proc.stdout` 永远等不到 EOF，
    _run_page 不返回、pool.map 不退出，任务在界面上"翻译中"挂到天荒地老，
    此后 start() 每次都把这条死任务原样退回（重启才解）。taskkill /T 按进程树收，
    拿不到就退回 proc.kill()——至少把父进程杀了。"""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        flags, si = _no_window()
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, creationflags=flags, startupinfo=si)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass

def adopt_existing(out_dir: str, mono=(), dual=()):
    """盘上已经有"看起来完整"的成品就认领，返回 {"dual","mono"}（缺的一方是空串）或 None。

    为什么要有：pdf2zh 是独立进程，eggpaper 关掉/装新版本时它还在跑，写完之后没人认领——
    启动时那次扫描早过了。用户看到界面上还是「全文翻译」，点一次就重译一遍（还覆盖成品）。
    判定"完整"看 %%EOF 收尾，免得把写到一半就被杀掉的半截文件当成成品。
    盘上**只落译文版（mono）**省盘，双语版按需派生。
    成品名两代并存：可读名（`<显示名>（译文）.pdf`，调用方从 DB 显示名派生传入）优先，
    老名 mono/dual.pdf 兜底——夹名对齐迁移过的库只有前者，没跑到的两样都认。"""
    mono_path = next((os.path.join(out_dir, n) for n in (*mono, "mono.pdf")
                      if _pdf_complete(os.path.join(out_dir, n))), "")
    dual_path = next((os.path.join(out_dir, n) for n in (*dual, "dual.pdf")
                      if _pdf_complete(os.path.join(out_dir, n))), "")
    if not mono_path and not dual_path:
        return None
    return {"dual": dual_path, "mono": mono_path}

def job(pid: str) -> dict:
    return JOBS.setdefault(pid, {"status": "none", "error": "", "dual": "", "mono": "",
                                 "service": "", "note": "", "pages": [0, 0],
                                 "current": [], "started": 0.0})

# ---------------- 全文翻译：按页流水线 ----------------

PAGE_WORKERS_FREE = 3
PAGE_WORKERS_LLM = 2
# 全库共享的引擎进程上限：每篇各有自己的线程池，两篇同时译就是两份池——
# 不设全局闸的话，6~8 个 pdf2zh 进程同时冷启动+跑 onnx，内存直接翻车
ENGINE_PROCS_CAP = 4
_SLOTS = threading.BoundedSemaphore(ENGINE_PROCS_CAP)
PAGE_TIMEOUT = 150
PAGE_TRIES = 2
BATCH_PAGES = 8          # 免费服务一次 pdf2zh 进程译几页。2.x 单进程冷启动 ~15-35s（1.9 约 3s），
                         # 批太小全是纯开销；批内坏页仍拆单页兜底，复用粒度退到"批"但产物按页认
BATCH_PAGES_LLM = 2      # LLM 服务（openai 等）一次译几页：每页都要真过一遍模型（实测 openai
                         # 通道 ~66s/页），8 页一批 = 第一拍进度要等 ~9 分钟，用户早当它死了
                         # 按停止（2026-10-08 真库日志：4 页 264s，第 3 分半被取消——上一次
                         # 的"小文档拆两半"只救了 ≤8 页的文档）。2 页一批，每 ~2 分半跳一格；
                         # 多付的冷启动全程摊下来 ~2 分钟，换来的是进度条真的在动

LLM_SERVICES = {"openai", "deepseek", "zhipu", "silicon", "modelscope",
                "gemini", "grok", "groq"}

# 两条通道的页级参数各一档："这个服务过不过模型"的判据只在这里出现一次——
# 原来三个单行函数各写一遍 `service in LLM_SERVICES`，改档位容易漏掉一处。
_PAGE_PROFILE = {
    True: {"workers": PAGE_WORKERS_LLM, "timeout": PAGE_TIMEOUT * 2, "batch": BATCH_PAGES_LLM},
    False: {"workers": PAGE_WORKERS_FREE, "timeout": PAGE_TIMEOUT, "batch": BATCH_PAGES},
}

def _page_profile(service: str) -> dict:
    """这次翻译走哪一档页级参数：LLM 家族（每页都真过模型）与非 LLM 免费通道。"""
    return _PAGE_PROFILE[service in LLM_SERVICES]

def _page_workers(service: str) -> int:
    return _page_profile(service)["workers"]

def _page_timeout(service: str) -> float:
    return _page_profile(service)["timeout"]

def page_sig(pdf_path: str, service: str, extra: str, engine: str,
             glossary_rows: list, flavor: str = "") -> str:
    """页级缓存的指纹：要覆盖一切"会改变产物"的输入——源文件（mtime+大小）、
    翻译服务、附加参数、引擎名、术语气息。

    只认源文件的话，换服务（bing→LLM）、改术语表或 options 后再点翻译，旧页会被
    静默复用：一篇译文里两种姿势混着，术语锁定只在新译的页上生效，用户拿着
    "术语已锁定"的期待看一份半锁定的文档（2026-10-11 审计实锤）。指纹一变，
    .pages 整目录作废重译，正是"改了输入就该重来"。
    """
    try:
        gsig = ""
        if service in LLM_SERVICES and glossary_rows:
            pairs = sorted((str((r or {}).get("term_en") or ""),
                            str((r or {}).get("term_zh") or "")) for r in glossary_rows)
            gsig = hashlib.sha256(
                json.dumps(pairs, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
        return json.dumps({"m": int(os.path.getmtime(pdf_path)),
                           "s": os.path.getsize(pdf_path),
                           "svc": service, "extra": extra or "",
                           "eng": engine or "", "g": gsig,
                           # f = 哪个模型 + 哪个上游端点。同一个服务名下换模型/换端点，
                           # 旧页会被整页复用 → 一份成品里两种模型风格、两套术语口径混排，
                           # 界面还照报"术语已锁定"（2026-10-13 审计）
                           "f": flavor or ""})
    except OSError:
        return ""

def _page_dir(out_dir: str, pno: int, t: int) -> str:
    return os.path.join(out_dir, ".pages", f"p{pno}-{t}")

def _batch_size(service: str) -> int:
    """一批译几页：LLM 服务 2（页数要中途真的动，见 BATCH_PAGES_LLM），免费服务 8。"""
    return _page_profile(service)["batch"]

def _plan_batches(pending: list, size: int = BATCH_PAGES) -> list:
    """把待译页号切成批次，size=一批几页（免费服务 8、LLM 服务 2，见 BATCH_PAGES_LLM）。
    待译页不超过一批的小文档（几页的 short letter 是常态）若整份一刀切，
    pdf2zh 要到全批结束才落产物——进度从 0 直接跳 n，中途几分钟没有任何可见
    的动静（实测用户等了 3 分半就当死了按停止，其实 1 分钟后就译完了）。
    拆成两半交两个引擎进程并行：前一半完成时进度立刻可见，墙钟也更短。"""
    if 1 < len(pending) <= size:
        mid = (len(pending) + 1) // 2
        return [pending[:mid], pending[mid:]]
    return [pending[i:i + size] for i in range(0, len(pending), size)]

def _product_mono(dirpath: str, stem: str) -> str:
    """目录里这份源的译文版产物；没有返回空串。

    **2.x 的产物命名带中缀**：`abc.no_watermark.zh.mono.pdf`（水印模式.语言），不是
    1.9 的 `abc-mono.pdf`——按词干前缀 glob，别钉死整名；上一次跑的旧名也顺带认得。
    多个命中取最新的（同目录反复重跑不会互相清干净时，认最新那份）。
    """
    import glob
    stem = glob.escape(stem)    # 旧库的源可能叫 paper[1].pdf：方括号是字符类，不转义产物永远找不到
    cands = (glob.glob(os.path.join(dirpath, f"{stem}*.mono.pdf")) +
             glob.glob(os.path.join(dirpath, f"{stem}-mono.pdf")))
    if not cands:
        return ""
    return max(cands, key=os.path.getmtime)

def _page_done(pdir: str, stem: str):
    """这一页的目录里有没有一份完整的译文版产物（%%EOF 收尾）。
    页级双语产物用完即删（省盘：组装成品由 原文+译文 派生，双语成品按需派生）。
    有 → 返回 {"mono": 路径}；没有 → None。重跑时靠它跳过已译好的页。"""
    mono = _product_mono(pdir, stem)
    return {"mono": mono} if mono and _pdf_complete(mono) else None

def derive_dual(pdf_path: str, mono_path: str, dual_path: str) -> str:
    """双语版 = 原文奇页 + 译文偶页交错。盘上只留译文版（省盘：双语是它的两倍大），
    用户第一次点「双语」时才合成并缓存。失败页（译文就是原文那页）交错出来天然是两页原文，
    与"奇原文偶译文"的页码关系一致。"""
    import pymupdf
    src = pymupdf.open(pdf_path)
    mono = pymupdf.open(mono_path)
    dual = pymupdf.open()
    try:
        for i in range(max(len(src), len(mono))):
            dual.insert_pdf(src, from_page=min(i, len(src) - 1), to_page=min(i, len(src) - 1))
            if i < len(mono):
                dual.insert_pdf(mono, from_page=i, to_page=i)
            else:
                dual.insert_pdf(src, from_page=min(i, len(src) - 1), to_page=min(i, len(src) - 1))
        # 半截产物一旦被当成品服务就固化了：先落临时名再原子换名
        dual.save(dual_path + ".part", garbage=4, deflate=True)
        os.replace(dual_path + ".part", dual_path)
    finally:
        src.close(); mono.close(); dual.close()
    return dual_path

def derive_mono(dual_path: str, mono_path: str) -> str:
    """旧版存量只有双语版时，抽偶数页（0 基 2i+1）合成译文版。"""
    import pymupdf
    dual = pymupdf.open(dual_path)
    mono = pymupdf.open()
    try:
        for i in range(1, len(dual), 2):
            mono.insert_pdf(dual, from_page=i, to_page=i)
        mono.save(mono_path + ".part", garbage=4, deflate=True)
        os.replace(mono_path + ".part", mono_path)
    finally:
        dual.close(); mono.close()
    return mono_path

def sweep_page_dirs(papers_root: str, max_age: float = 48 * 3600):
    """回收陈旧的 .pages/ 页级目录（页级产物各自内嵌全文档的字体，一份好几 MB）。

    翻译没跑完时成功的页**故意留着**（重跑直接复用，不再重译一遍），但用户也可能
    再也不回来——那就按年龄回收。启动时对整库调一次。
    """
    now = time.time()
    try:
        papers = os.listdir(papers_root)
    except OSError:
        return
    for pid in papers:
        path = os.path.join(papers_root, pid, ".pages")
        try:
            if os.path.isdir(path) and now - os.path.getmtime(path) > max_age:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

def _page_env(envs: dict) -> dict:
    """pdf2zh 子进程的环境。HOME/USERPROFILE 指到 eggpaper 数据目录下的 home/：
    pdf2zh_next 在 import 时就会创建 ~/.config/pdf2zh 并可能往里写自动保存的配置——
    不指过去就散落在 C 盘用户目录（用户要求：我们装的和写的都规范在 eggpaper 的
    文件夹里）。key 走 PDF2ZH_* 环境变量传入，不落命令行；home 里万一被自动落了盘，
    任务收尾由 _sweep_home_config 扫掉。"""
    env = {**os.environ, **(envs or {})}
    try:
        import appinfo
        home = os.path.join(appinfo.data_dir(), "home")
        os.makedirs(home, exist_ok=True)
        env["HOME"] = home
        env["USERPROFILE"] = home
    except Exception:
        pass
    return env

def _pages_count(pages: str) -> int:
    """--pages 的页数："5" → 1，"5-8" → 4。给超时预算用。"""
    m = re.match(r"^(\d+)(?:-(\d+))?$", (pages or "").strip())
    if not m:
        return 1
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    return max(1, b - a + 1)

def _run_page(pdf_path: str, pages: str, out_dir: str, service: str, extra: str,
              envs: dict, proc_reg: list, auth_out: list, engine: str = "",
              glossary_csv: str = "", asset_out: list = None) -> tuple:
    """翻译一页（或一个页区间）。返回 (产物 dict 或 None, 日志尾行 list)。超时/报错返回 (None, tail)。
    proc_reg：正在跑的进程都登记进来，删论文时 cancel() 能把它们掐掉。
    auth_out：一旦在输出里看到鉴权失败就**立刻**杀进程并记到这里——
    pdf2zh 对 401 是无限重试，等它自己结束要磨到天荒地老。
    asset_out：同理，看到"取字体/模型资源失败"也立刻收工（见 _ASSET_FAILS）：
    整台机器现在够不到引擎的资源站，这一页和下一页都卡在同一处，重试纯属把时间耗光。

    为什么收"页区间"：pdf2zh 每次冷启动要全套依赖 import（2.x 还要加载 onnx 模型），
    一页一进程时 30 页 = 30 次纯开销。按批（免费 8 页 / LLM 2 页，见 _batch_size）冷启动
    摊薄；批里单页坏了拆成单页再各试一遍，复用粒度从"页"退到"批"，
    断点续译（_page_done 逐页认）不变。
    """
    cmd = _cmd(pdf_path, out_dir, service, extra, glossary_csv, engine, pages)
    tail = collections.deque(maxlen=5)
    flags, si = _no_window()
    proc = None
    timeout_hit = []
    budget = _page_timeout(service) * _pages_count(pages)
    deadline = time.time() + budget

    def _kill_when_stale():
        while proc.poll() is None and time.time() < deadline:
            time.sleep(2)
        if proc.poll() is None:
            timeout_hit.append(True)     # 把超时如实写进 tail：失败原因别一律报"退出码 1"
            _kill_tree(proc)             # 必须连树杀，见 _kill_tree 的注释（卡死主因）

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                cwd=out_dir, env=_page_env(envs),
                                creationflags=flags, startupinfo=si)
        proc_reg.append(proc)
        threading.Thread(target=_kill_when_stale, daemon=True).start()
        for raw in proc.stdout:
            line = _ANSI.sub("", raw).strip()
            if not line or chr(13) in line or _PROG.search(line):
                continue
            low = line.lower()
            if any(k in low for k in AUTH_FAILS):
                auth_out.append(f"{service} 服务的 key 不对（pdf2zh 报鉴权失败）：{line[-160:]}")
                _kill_tree(proc)
                continue
            if asset_out is not None and _is_asset_net_fail(low):
                asset_out.append(_ASSET_HINT)
                _kill_tree(proc)
                continue
            tail.append(line)
        proc.wait()
    except Exception as e:
        tail.append(f"{type(e).__name__}: {str(e)[-160:]}")
        return None, list(tail)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    mono = _product_mono(out_dir, stem)
    if mono and _pdf_complete(mono):
        for stale in os.listdir(out_dir):
            if stale.endswith("-dual.pdf") or (
                    stale.endswith(".dual.pdf") and stale != os.path.basename(mono)):
                fsutil.rm_quiet(os.path.join(out_dir, stale))
        return {"mono": mono}, list(tail)
    if timeout_hit:
        tail.append(f"单页超时（{int(budget)}s 预算用尽）")
    elif not any("单页超时" in t for t in tail):
        tail.append(f"单页失败（退出码 {proc.poll()}）")
    return None, list(tail)

def _pdf_complete(path: str) -> bool:
    """文件存在且以 %%EOF 收尾（写到一半被杀的文件没有这个）。"""
    try:
        if os.path.getsize(path) < 10_000:
            return False
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 2048))
            return b"%%EOF" in f.read()
    except OSError:
        return False

def _prescan_reuse(out_dir: str, n: int, stem: str) -> dict:
    """预扫描续译：把上次已经译好的页直接复用，返回 {0 基页号: {"mono","at"}}。

    批目录名是批首页（1 基，worker 写 p{batch[0]+1}-…），一份产物覆盖整批。
    **必须按批映射**：老写法把 p9 目录里的 8 页产物当成"第 9 页"，组装时 at=None
    会取到产物最后一页——重启复用把第 16 页插到第 9 页的位置（实测续译复用 3/24
    就是它）。区间产物第 i 页对应原文第 a+i 页；整本产物（ml==n，老引擎形态）页号即位置。
    """
    results = {}
    for a in range(1, n + 1):
        for t in range(PAGE_TRIES):
            pdir = _page_dir(out_dir, a, t)
            got = _page_done(pdir, stem)
            if not got:
                continue
            try:
                ml = pdfparse.page_count(got["mono"])
            except Exception:
                break               # 产物读不动：当这页没有，重译
            if ml == n:
                for p in range(n):
                    results[p] = {"mono": got["mono"], "at": p}
            else:
                for p in range(a - 1, min(a - 1 + ml, n)):
                    results[p] = {"mono": got["mono"], "at": p - (a - 1)}
            break
    return results

def _assemble(out_dir: str, pdf_path: str, n: int, results: dict, mono_name: str,
              dual_name: str, j: dict, say, pid: str, service: str) -> tuple:
    """组装成品：盘上只落译文版（省盘：双语是它的两倍大，首次点开时由 原文+译文 派生）。

    返回 (成品路径, 失败页号列表)；中途放弃（全部页都没成/被取消）返回 ("", [])——
    放弃的原因已经写进 j（status=error/none），调用方看到空路径直接 return 即可。
    """
    import pymupdf
    mono_path = os.path.join(out_dir, mono_name or "mono.pdf")
    failed = []
    src = pymupdf.open(pdf_path)
    mono = pymupdf.open()
    prods: dict = {}     # 批产物路径 → 已打开的文档：8 页一批时同一文件要取 8 页，
    try:                 # 逐页重开一遍是纯浪费（doc.open 顺带解析 xref，几十毫秒/次）
        def _prod_of(p):
            d = prods.get(p)
            if d is None:
                d = pymupdf.open(p)
                prods[p] = d
            return d
        for pno in range(n):
            got = results.get(pno)
            ok = False
            if got:
                try:
                    m = _prod_of(got["mono"])
                    at = got.get("at")
                    at = min(pno, len(m) - 1) if at is None else max(0, min(int(at), len(m) - 1))
                    mono.insert_pdf(m, from_page=at, to_page=at)
                    ok = True
                except Exception:
                    prods.pop(got["mono"], None)   # 坏产物别留在缓存里反复炸
                    pass
            if not ok:
                failed.append(pno + 1)
                mono.insert_pdf(src, from_page=pno, to_page=pno)
        if n and not results:
            j.update(status="error",
                     error=f"所有页面都没译成（{service} 连不上或被限流）。"
                           "到「设置 → 全文翻译服务」换一个再试。")
            say(f"全文翻译失败 {pid}：全部页面失败")
            return "", []
        if j.get("_abort"):
            j.update(status="none", error="")   # 组装途中删了篇/点了停止：别把成品写进已删的目录
            return "", []
        # 原子落盘：半截的成品不该被任何人读到（认领/GET 都看 %%EOF，临时名不冒充成品）
        mono.save(mono_path + ".part", garbage=4, deflate=True)
        for _try in range(3):
            try:
                os.replace(mono_path + ".part", mono_path)
                break
            except OSError:
                if _try == 2:
                    raise       # 旧 mono 还被浏览器响应占着句柄：等它放手再换名，
                time.sleep(1.5) # 别把整场已经译完的活儿标成失败
        for stale in dict.fromkeys(("dual.pdf", dual_name)):
            if stale:
                fsutil.rm_quiet(os.path.join(out_dir, stale))
    finally:
        src.close(); mono.close()
        for d in prods.values():
            try:
                d.close()
            except Exception:
                pass
    return mono_path, failed

def start(pid: str, pdf_path: str, out_dir: str, service: str, extra: str = "",
          envs: dict = None, log=None, note: str = "", engine: str = "",
          glossary_rows: list = None, fresh: bool = False,
          mono_name: str = "", dual_name: str = "", flavor: str = "") -> dict:
    """按页流水线翻译全文：进度=完成页数，坏页回退原文，一页卡不住整份文档。

    glossary_rows：本篇术语表（[{term_en, term_zh}, …]）。非空且服务是 LLM 家族时
    写成 BabelDOC 术语 CSV，随每一批进程 `--glossaries` 注入 prompt——全文链的
    术语锁定就靠它（bing/google 这类非 LLM 服务吃不了，跳过不报错）。
    fresh=True：清掉全部页级缓存与成品再译——「重新全文翻译」按的是这个，不 fresh
    的话预扫描会把上次译好的页全复用，按钮等于没按（实测踩过）。
    mono_name/dual_name：成品文件名（`<显示名>（译文）.pdf`），调用方从 DB 显示名
    派生；空串退回老名 mono/dual.pdf。落名走调用方，这层不认名字只认路径。
    """
    j = job(pid)
    th = _WORKERS.get(pid)
    if th is not None:
        if j["status"] in ("running", "queued") and th.is_alive():
            # queued 也算在跑：start 同步置 queued、线程稍后才起来——这个窗口里第二次点击
            # 会再起一条流水线，两个 pdf2zh 写同一个 out_dir
            return j
        # 状态已落（done/error）但线程还在收尾：等它把 .pages 清理和进程收尸走完。
        # 不等的话，新任务写 .pages 的同时旧线程在 rmtree 它——缓存被自己人清掉，
        # 一页页重译（慢而不坏，但白烧时间）。
        th.join(timeout=20)
    if j["status"] in ("running", "queued"):
        if th is not None and th.is_alive():
            return j      # 20 秒还没收完（杀软攥着目录之类）：还当在跑，别叠第二条流水线
        # 登记簿说有活、进程里却没有对应的线程：上一轮的线程已经没了却没落终态
        # （卡死在 EOF/回调里的残骸）。不清理的话 start 每次都原样退回这条死任务，
        # 这篇论文到重启为止再也翻不了。当死任务清掉重开。
        llm.applog(f"[eggpaper] 全文翻译 {pid}: 状态是 {j['status']} 但没有在跑的线程，重置重开")
        j.update(status="none", error="", current=[])

    def _pipeline():
        say = log or (lambda _m: None)
        if j.pop("_abort", None):
            # start 返回后、线程还没调度到就被取消（或删论文）：安静收场，别照跑到底
            j.update(status="none", error="", pages=[0, 0])
            return
        j.update(status="running", error="", dual="", mono="", service=service,
                 note=note, pages=[0, 0], current=[], started=time.time())
        page_root = os.path.join(out_dir, ".pages")
        procs = []
        results = {}
        exe = engine_path(engine)
        ok, why = engine_probe_cached(exe)
        if not ok:
            msg = f"全文翻译引擎不可用（{why}）。到「设置 → 翻译引擎」安装或升级。"
            j.update(status="error", error=msg)
            say(f"全文翻译失败 {pid}：引擎不可用（{why}）")
            return
        say(f"全文翻译 {pid}: 用引擎 {exe}（{why}）")
        try:
            import engine_install
            # 先补后收：缓存被清过（home/ 丢了、数据目录搬过家）就把收起的资产包放回 zip 名，
            # 引擎启动时自己离线恢复——不然只能去它硬编码的资源站现取，国内网络下就是"取资源失败"
            if engine_install.restore_assets_zip():
                say(f"全文翻译 {pid}: 资产缓存不全，已把收起的 offline 资产包放回（引擎离线恢复）")
            if engine_install.settle_assets():
                say(f"全文翻译 {pid}: 收起引擎的 offline 资产包，后续每批省 10-20 秒重哈希")
        except Exception:
            pass
        try:
            os.makedirs(out_dir, exist_ok=True)
            if fresh:
                # 重译 = 连缓存一起清：页级产物、成品、双语缓存都不要了
                shutil.rmtree(page_root, ignore_errors=True)
                for stale in dict.fromkeys(("mono.pdf", "dual.pdf", mono_name, dual_name)):
                    if stale:
                        fsutil.rm_quiet(os.path.join(out_dir, stale))
            sig = page_sig(pdf_path, service, extra, engine, glossary_rows, flavor)
            mark = os.path.join(page_root, ".src.json")
            try:
                with open(mark, encoding="utf-8") as f:
                    old = f.read()
            except (OSError, ValueError):
                old = ""
            if old != sig:
                shutil.rmtree(page_root, ignore_errors=True)
            os.makedirs(page_root, exist_ok=True)
            try:
                with open(mark, "w", encoding="utf-8") as f:
                    f.write(sig)
            except OSError:
                pass
            glossary_csv = ""
            if service in LLM_SERVICES and glossary_rows:
                gp = os.path.join(page_root, "glossary.csv")
                if write_glossary_csv(gp, glossary_rows):
                    glossary_csv = gp
                    say(f"全文翻译 {pid}: 术语表 {len(glossary_rows)} 条注入全文翻译")
            n = pdfparse.page_count(pdf_path)
            if n == 0:
                j.update(status="error", error="PDF 没有可翻译的页面")
                say(f"全文翻译失败 {pid}：0 页")
                return
            stem = os.path.splitext(os.path.basename(pdf_path))[0]
            results = _prescan_reuse(out_dir, n, stem)
            if results:
                say(f"全文翻译 {pid}: 复用上次已译好的 {len(results)}/{n} 页，只译剩下的")
            j["pages"] = [len(results), n]
            _RUNNING[pid] = procs
            lock = threading.Lock()
            auth_error = []
            asset_error = []

            def worker(batch: list):
                try:
                    _worker(batch)
                except Exception as e:
                    say(f"全文翻译 {pid}: 第 {batch[0] + 1} 页起的一批异常（{type(e).__name__}），保留原文")

            def _worker(batch: list):
                """batch：连续的 0 基页号。整批两次都没成时拆成单页各再试一遍——
                别让一页坏页连坐同批的没坏页。"""
                a, b = batch[0] + 1, batch[-1] + 1
                label = str(a) if a == b else f"{a}-{b}"
                got, tail, auth_out, asset_out = None, [], [], []
                for t in range(PAGE_TRIES):
                    if auth_error or asset_error or j.get("_abort"):
                        return
                    # 全局闸：这里排队的是"引擎进程"而不是线程——同一时刻全库最多
                    # ENGINE_PROCS_CAP 个 pdf2zh 在跑，跨篇也不会超。
                    # 目录创建必须在拿到槽、过了取消复查**之后**：否则删篇+排队期间
                    # 会把 .pages 目录复活成幽灵（pdf2zh 的 cwd 还指着它时 rmtree 也删不掉）
                    with _SLOTS:
                        if auth_error or asset_error or j.get("_abort"):
                            return
                        pdir_t = _page_dir(out_dir, a, t)
                        os.makedirs(pdir_t, exist_ok=True)
                        # current：正在译的批区间，给前端的"正在译第 x-y 页"。
                        # pdf2zh 2.x 在管道下不吐实时进度（实测：进度行全是任务完成时
                        # 才一次性打出），批内的页级进度物理上拿不到——把"在译哪些页"
                        # 报出去，进度就不会在 0/20 上看起来像死了。
                        with lock:
                            j["current"] = [*(j.get("current") or []), label]
                        try:
                            got, tail = _run_page(pdf_path, label, pdir_t, service, extra,
                                                  envs, procs, auth_out, engine, glossary_csv,
                                                  asset_out)
                        finally:
                            with lock:
                                j["current"] = [x for x in (j.get("current") or []) if x != label]
                    if auth_out:
                        auth_error.append(auth_out[0])
                        return
                    if asset_out:
                        # 取资源失败：整篇同病，收工别再逐页重试
                        asset_error.append(asset_out[0])
                        return
                    if got:
                        if t > 0:
                            say(f"全文翻译 {pid}: 第 {label} 页第二次尝试成功")
                        break
                if got:
                    # pdf2zh 对 --pages 的产物有两种形态：全文（只有选中的页被译了）或只含选中页。
                    # 记下每页在产物里的真实位置，组装时按它取，别猜。
                    try:
                        ml = pdfparse.page_count(got["mono"])
                    except Exception:
                        ml = 0
                    unmapped = 0
                    with lock:
                        for p in batch:
                            if ml == n:
                                at = p                       # 全文产物：0 基页号即位置
                            elif ml == len(batch):
                                at = p - (a - 1)             # 只含区间：按批内顺序排
                            else:
                                # 页数既不是整本也不是批长：位置无从得知。原先是
                                # min(p, ml-1)——多页一起指到同一张产物页，装出来的成品里
                                # 出现**重复页**、一声不响（与 _prescan_reuse 修过的
                                # "第 16 页插到第 9 页位置"同源）。认得出位置的按批内顺序
                                # 先认下，认不出的如实判失败、这页留原文：宁可少一页译文，
                                # 也不给一页错的。
                                at = p - (a - 1) if 0 <= p - (a - 1) < ml else None
                            if at is None:
                                unmapped += 1
                                continue
                            results[p] = {"mono": got["mono"], "at": at}
                        j["pages"] = [len(results), n]
                    if unmapped:
                        say(f"全文翻译 {pid}: 第 {label} 页这批的产物页数 {ml} 与整本 {n} / 批长 "
                            f"{len(batch)} 都对不上，{unmapped} 页保留原文（不猜位置，免得成品里出现重复页）")
                    return
                if len(batch) > 1:
                    say(f"全文翻译 {pid}: 第 {label} 页整批两次都没成，拆成单页再各试一遍")
                    for p in batch:
                        _worker([p])
                    return
                say(f"全文翻译 {pid}: 第 {a} 页两次都没译成，这一页保留原文"
                     f"（{' / '.join(tail[-2:])}）")

            from concurrent.futures import ThreadPoolExecutor
            pending = [p for p in range(n) if p not in results]
            batches = _plan_batches(pending, _batch_size(service))
            with ThreadPoolExecutor(max_workers=_page_workers(service)) as pool:
                list(pool.map(worker, batches))

            if auth_error:
                j.update(status="error", error=auth_error[0])
                say(f"全文翻译失败 {pid}：{auth_error[0]}")
                return
            if not results and asset_error:
                # 一页都没译成且原因是"取资源失败"：整篇都会卡在同一处，如实报错收工
                # （已译出的页不浪费——断点续译照旧，下次点翻译从剩下的页继续）
                j.update(status="error", error=asset_error[0])
                say(f"全文翻译失败 {pid}：{asset_error[0]}")
                return
            if asset_error:
                say(f"全文翻译 {pid}：部分页没译成——{asset_error[0]}")
            if j.pop("_abort", None):
                j.update(status="none", error="")      # 用户主动取消，不算一次失败
                return

            # ---- 组装（见 _assemble）：盘上只落译文版（省盘：双语是它的两倍大）
            mono_path, failed = _assemble(out_dir, pdf_path, n, results, mono_name,
                                          dual_name, j, say, pid, service)
            if not mono_path:
                return

            done_note = (note or "") + (f"（第 {', '.join(map(str, failed))} 页没译成，保留原文）"
                                        if failed else "")
            j.update(status="done", dual="", mono=mono_path,
                     error="", pages=[n, n], note=done_note)
            say(f"全文翻译完成 {pid}：{int(time.time() - j['started'])}s · {service}"
                 f" · {len(results)}/{n} 页" + (f" · 失败页 {failed}" if failed else ""))
        except Exception as e:
            hint = llm.human_error(e)      # 界面直接显示这行：走统一口径，别把 "OSError: [Errno 28]…" 原文甩给用户
            j.update(status="error", error=hint[:400])
            say(f"全文翻译失败 {pid}：{type(e).__name__}: {str(e)[-300:]}")
        finally:
            _RUNNING.pop(pid, None)
            j.pop("_abort", None)   # 组装期点取消会再置旗：残留会让下一次 start 静默空转
            j["current"] = []
            if j["status"] == "done" or not results:
                shutil.rmtree(page_root, ignore_errors=True)
            else:
                sweep_key_copies(page_root)
            _sweep_home_config()
            for proc in procs:
                _kill_tree(proc)    # 收尾也要连树杀：孙进程漏一个，page_root 就 rmtree 不掉
            _WORKERS.pop(pid, None)  # 收尾全部走完才撤登记：start() 的 join 等的是这里

    def run():
        try:
            _pipeline()
        finally:
            _WORKERS.pop(pid, None)  # 早退出口（引擎不可用/取消先到）也必撤登记

    j.update(status="queued", error="", pages=[0, 0], current=[])   # 线程还没跑到的窗口期也让 cancel 有归属
    th = threading.Thread(target=run, daemon=True)
    _WORKERS[pid] = th          # 登记在 start 里、不在 run 里：线程被调度前的那一点窗口
    th.start()                  # 第二次点击也必须看到"有人在跑"，否则会再起一条流水线
    return j
