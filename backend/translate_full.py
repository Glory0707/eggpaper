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

JOBS = {}
_RUNNING = {}
_PROBE = {}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROG = re.compile(r"(\d+)%\|[^|]*\|\s*(\d+)/(\d+)")
AUTH_FAILS = ("authentication fails", "invalid api key", "incorrect api key",
              "401 - ", "unauthorized", "invalid_api_key", "api key is required")

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
    """能不能连上 host:port。返回 (是否通, 不通时的一句人话)。结果缓存 ttl 秒。"""
    if not host:
        return True, ""
    hit = _PROBE.get(host)
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
    _PROBE[host] = (ok, why, now)
    return ok, why

def choose_service(service: str, host: str = ""):
    """定下这次真正用哪个服务。返回 (服务名或 None, 给用户的一句话)。

    host 传了就以它为准（openai 服务用的是用户在设置里填的那个端点）。
    """
    service = (service or "bing").strip()
    target = host or SERVICE_HOST.get(service, "")
    if probe(target)[0]:
        return service, ""
    why = probe(target)[1]
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

    顺序：设置里指定的 → PATH → exe 同目录 → 数据目录 engines/ → 常见 Python Scripts。
    最后一处（数据目录 engines/）是给"手动放一个进来"留的稳位：那目录升级、卸载都不动。
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
    """
    if not path or not os.path.exists(path):
        return -1, ""
    flags, si = _no_window()
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           creationflags=flags, startupinfo=si, env=_page_env({}))
    except Exception:
        return -1, ""
    return r.returncode, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()

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
    ok, why = engine_probe(path)
    _ENGINE[path] = {"mtime": mt, "ok": ok, "why": why, "at": time.time()}
    return ok, why

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
                try:
                    os.remove(os.path.join(dirpath, fn))
                except OSError:
                    pass

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
                try:
                    os.remove(os.path.join(cfg_dir, fn))
                except OSError:
                    pass
    except Exception:
        pass

def cancel(pid: str):
    """删论文时用：把还在跑的 pdf2zh 掐掉。

    pdf2zh 是独立进程——不掐的话，"删掉这篇论文"之后它还会跑完、还会往
    已删掉的论文文件夹里写回 mono.pdf，用户以为删干净了、盘上却留下孤立的译文文件。
    """
    j = job(pid)
    if j.get("status") in ("running", "queued"):
        # 只给在跑/在排队的一轮置旗：空闲时置旗会被下一轮 start 当成"取消前到达"误吞
        j["_abort"] = True
    procs = _RUNNING.get(pid) or []
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass
    return bool(procs)

def adopt_existing(out_dir: str):
    """盘上已经有"看起来完整"的成品就认领，返回 {"dual","mono"}（缺的一方是空串）或 None。

    为什么要有：pdf2zh 是独立进程，eggpaper 关掉/装新版本时它还在跑，写完之后没人认领——
    启动时那次扫描早过了。用户看到界面上还是「全文翻译」，点一次就重译一遍（还覆盖成品）。
    判定"完整"看 %%EOF 收尾，免得把写到一半就被杀掉的半截文件当成成品。
    盘上**只落译文版（mono）**省盘，双语版按需派生。
    """
    mono = os.path.join(out_dir, "mono.pdf")
    dual = os.path.join(out_dir, "dual.pdf")
    mono_ok = _pdf_complete(mono)
    dual_ok = _pdf_complete(dual)
    if not mono_ok and not dual_ok:
        return None
    return {"dual": dual if dual_ok else "", "mono": mono if mono_ok else ""}

def job(pid: str) -> dict:
    return JOBS.setdefault(pid, {"status": "none", "error": "", "dual": "", "mono": "",
                                 "service": "", "note": "", "pages": [0, 0], "started": 0.0})

# ---------------- 全文翻译：按页流水线 ----------------

PAGE_WORKERS_FREE = 3
PAGE_WORKERS_LLM = 2
# 全库共享的引擎进程上限：每篇各有自己的线程池，两篇同时译就是两份池——
# 不设全局闸的话，6~8 个 pdf2zh 进程同时冷启动+跑 onnx，内存直接翻车
ENGINE_PROCS_CAP = 4
_SLOTS = threading.BoundedSemaphore(ENGINE_PROCS_CAP)
PAGE_TIMEOUT = 150
PAGE_TRIES = 2
BATCH_PAGES = 8          # 一次 pdf2zh 进程译几页。2.x 单进程冷启动 ~15-35s（1.9 约 3s），
                         # 批太小全是纯开销；批内坏页仍拆单页兜底，复用粒度退到"批"但产物按页认

LLM_SERVICES = {"openai", "deepseek", "zhipu", "silicon", "modelscope",
                "gemini", "grok", "groq"}

def _page_workers(service: str) -> int:
    return PAGE_WORKERS_LLM if service in LLM_SERVICES else PAGE_WORKERS_FREE

def _page_timeout(service: str) -> float:
    return PAGE_TIMEOUT * 2 if service in LLM_SERVICES else PAGE_TIMEOUT

def _page_dir(out_dir: str, pno: int, t: int) -> str:
    return os.path.join(out_dir, ".pages", f"p{pno}-{t}")

def _product_mono(dirpath: str, stem: str) -> str:
    """目录里这份源的译文版产物；没有返回空串。

    **2.x 的产物命名带中缀**：`abc.no_watermark.zh.mono.pdf`（水印模式.语言），不是
    1.9 的 `abc-mono.pdf`——按词干前缀 glob，别钉死整名；上一次跑的旧名也顺带认得。
    多个命中取最新的（同目录反复重跑不会互相清干净时，认最新那份）。
    """
    import glob
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
              glossary_csv: str = "") -> tuple:
    """翻译一页（或一个页区间）。返回 (产物 dict 或 None, 日志尾行 list)。超时/报错返回 (None, tail)。
    proc_reg：正在跑的进程都登记进来，删论文时 cancel() 能把它们掐掉。
    auth_out：一旦在输出里看到鉴权失败就**立刻**杀进程并记到这里——
    pdf2zh 对 401 是无限重试，等它自己结束要磨到天荒地老。

    为什么收"页区间"：pdf2zh 每次冷启动要全套依赖 import（2.x 还要加载 onnx 模型），
    一页一进程时 30 页 = 30 次纯开销。按 4 页一批冷启动摊薄；批里单页坏了拆成单页
    再各试一遍，复用粒度从"页"退到"批"，断点续译（_page_done 逐页认）不变。
    """
    cmd = _cmd(pdf_path, out_dir, service, extra, glossary_csv, engine, pages)
    tail = collections.deque(maxlen=5)
    flags, si = _no_window()
    proc = None
    deadline = time.time() + _page_timeout(service) * _pages_count(pages)

    def _kill_when_stale():
        while proc.poll() is None and time.time() < deadline:
            time.sleep(2)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

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
                try:
                    proc.kill()
                except Exception:
                    pass
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
                try:
                    os.remove(os.path.join(out_dir, stale))
                except OSError:
                    pass
        return {"mono": mono}, list(tail)
    if not any("单页超时" in t for t in tail):
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

def start(pid: str, pdf_path: str, out_dir: str, service: str, extra: str = "",
          envs: dict = None, log=None, note: str = "", engine: str = "",
          glossary_rows: list = None, fresh: bool = False) -> dict:
    """按页流水线翻译全文：进度=完成页数，坏页回退原文，一页卡不住整份文档。

    glossary_rows：本篇术语表（[{term_en, term_zh}, …]）。非空且服务是 LLM 家族时
    写成 BabelDOC 术语 CSV，随每一批进程 `--glossaries` 注入 prompt——全文链的
    术语锁定就靠它（bing/google 这类非 LLM 服务吃不了，跳过不报错）。
    fresh=True：清掉全部页级缓存与成品再译——「重新全文翻译」按的是这个，不 fresh
    的话预扫描会把上次译好的页全复用，按钮等于没按（实测踩过）。
    """
    j = job(pid)
    if j["status"] in ("running", "queued"):
        # queued 也算在跑：start 同步置 queued、线程稍后才起来——这个窗口里第二次点击
        # 会再起一条流水线，两个 pdf2zh 写同一个 out_dir
        return j

    def run():
        say = log or (lambda _m: None)
        if j.pop("_abort", None):
            # start 返回后、线程还没调度到就被取消（或删论文）：安静收场，别照跑到底
            j.update(status="none", error="", pages=[0, 0])
            return
        j.update(status="running", error="", dual="", mono="", service=service,
                 note=note, pages=[0, 0], started=time.time())
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
            import pymupdf
            os.makedirs(out_dir, exist_ok=True)
            if fresh:
                # 重译 = 连缓存一起清：页级产物、成品、双语缓存都不要了
                shutil.rmtree(page_root, ignore_errors=True)
                for stale in ("mono.pdf", "dual.pdf"):
                    try:
                        os.remove(os.path.join(out_dir, stale))
                    except OSError:
                        pass
            try:
                sig = json.dumps({"m": int(os.path.getmtime(pdf_path)),
                                  "s": os.path.getsize(pdf_path)})
            except OSError:
                sig = ""
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
            with pymupdf.open(pdf_path) as doc:
                n = len(doc)
            if n == 0:
                j.update(status="error", error="这份 PDF 没有可翻译的页面")
                say(f"全文翻译失败 {pid}：0 页")
                return
            stem = os.path.splitext(os.path.basename(pdf_path))[0]
            # 预扫描续译：批目录名是批首页（1 基，worker 写 p{batch[0]+1}-…），一份产物
            # 覆盖整批。**必须按批映射**：老写法把 p9 目录里的 8 页产物当成"第 9 页"，
            # 组装时 at=None 会取到产物最后一页——重启复用把第 16 页插到第 9 页的位置
            # （实测续译复用 3/24 就是它）。区间产物第 i 页对应原文第 a+i 页；整本产物
            # （ml==n，老引擎形态）页号即位置。
            for a in range(1, n + 1):
                for t in range(PAGE_TRIES):
                    pdir = _page_dir(out_dir, a, t)
                    got = _page_done(pdir, stem)
                    if not got:
                        continue
                    try:
                        with pymupdf.open(got["mono"]) as m:
                            ml = len(m)
                    except Exception:
                        break
                    if ml == n:
                        for p in range(n):
                            results[p] = {"mono": got["mono"], "at": p}
                    else:
                        for p in range(a - 1, min(a - 1 + ml, n)):
                            results[p] = {"mono": got["mono"], "at": p - (a - 1)}
                    break
            if results:
                say(f"全文翻译 {pid}: 复用上次已译好的 {len(results)}/{n} 页，只译剩下的")
            j["pages"] = [len(results), n]
            _RUNNING[pid] = procs
            lock = threading.Lock()
            auth_error = []

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
                got, tail, auth_out = None, [], []
                for t in range(PAGE_TRIES):
                    if auth_error or j.get("_abort"):
                        return
                    pdir_t = _page_dir(out_dir, a, t)
                    os.makedirs(pdir_t, exist_ok=True)
                    # 全局闸：这里排队的是"引擎进程"而不是线程——同一时刻全库最多
                    # ENGINE_PROCS_CAP 个 pdf2zh 在跑，跨篇也不会超
                    with _SLOTS:
                        if auth_error or j.get("_abort"):
                            return
                        got, tail = _run_page(pdf_path, label, pdir_t, service, extra,
                                              envs, procs, auth_out, engine, glossary_csv)
                    if auth_out:
                        auth_error.append(auth_out[0])
                        return
                    if got:
                        if t > 0:
                            say(f"全文翻译 {pid}: 第 {label} 页第二次尝试成功")
                        break
                if got:
                    # pdf2zh 对 --pages 的产物有两种形态：全文（只有选中的页被译了）或只含选中页。
                    # 记下每页在产物里的真实位置，组装时按它取，别猜。
                    try:
                        with pymupdf.open(got["mono"]) as m:
                            ml = len(m)
                    except Exception:
                        ml = 0
                    with lock:
                        for p in batch:
                            if ml == n:
                                at = p                       # 全文产物：0 基页号即位置
                            elif ml == len(batch):
                                at = p - (a - 1)             # 只含区间：按批内顺序排
                            else:
                                at = min(p, max(0, ml - 1))
                            results[p] = {"mono": got["mono"], "at": at}
                        j["pages"] = [len(results), n]
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
            batches = [pending[i:i + BATCH_PAGES] for i in range(0, len(pending), BATCH_PAGES)]
            with ThreadPoolExecutor(max_workers=_page_workers(service)) as pool:
                list(pool.map(worker, batches))

            if auth_error:
                j.update(status="error", error=auth_error[0])
                say(f"全文翻译失败 {pid}：{auth_error[0]}")
                return
            if j.pop("_abort", None):
                j.update(status="none", error="")      # 用户主动取消，不算一次失败
                return

            # ---- 组装：盘上只落译文版（省盘：双语是它的两倍大，首次点开时由 原文+译文
            mono_path = os.path.join(out_dir, "mono.pdf")
            failed = []
            src = pymupdf.open(pdf_path)
            mono = pymupdf.open()
            try:
                for pno in range(n):
                    got = results.get(pno)
                    ok = False
                    if got:
                        try:
                            with pymupdf.open(got["mono"]) as m:
                                at = got.get("at")
                                at = min(pno, len(m) - 1) if at is None else max(0, min(int(at), len(m) - 1))
                                mono.insert_pdf(m, from_page=at, to_page=at)
                            ok = True
                        except Exception:
                            pass
                    if not ok:
                        failed.append(pno + 1)
                        mono.insert_pdf(src, from_page=pno, to_page=pno)
                if n and not results:
                    j.update(status="error",
                             error=f"所有页面都没译成（{service} 连不上或被限流）。"
                                   "换一个翻译服务（设置 → 全文翻译）再试。")
                    say(f"全文翻译失败 {pid}：全部页面失败")
                    return
                mono.save(mono_path, garbage=4, deflate=True)
                try:
                    os.remove(os.path.join(out_dir, "dual.pdf"))
                except OSError:
                    pass
            finally:
                src.close(); mono.close()

            done_note = (note or "") + (f"（第 {', '.join(map(str, failed))} 页没译成，保留原文）"
                                        if failed else "")
            j.update(status="done", dual="", mono=mono_path,
                     error="", pages=[n, n], note=done_note)
            say(f"全文翻译完成 {pid}：{int(time.time() - j['started'])}s · {service}"
                 f" · {len(results)}/{n} 页" + (f" · 失败页 {failed}" if failed else ""))
        except Exception as e:
            j.update(status="error", error=f"{type(e).__name__}: {str(e)[-400:]}")
            say(f"全文翻译失败 {pid}：{type(e).__name__}: {str(e)[-300:]}")
        finally:
            _RUNNING.pop(pid, None)
            if j["status"] == "done" or not results:
                shutil.rmtree(page_root, ignore_errors=True)
            else:
                sweep_key_copies(page_root)
            _sweep_home_config()
            for proc in procs:
                try:
                    proc.kill()
                except Exception:
                    pass

    j.update(status="queued", error="", pages=[0, 0])   # 线程还没跑到的窗口期也让 cancel 有归属
    threading.Thread(target=run, daemon=True).start()
    return j
